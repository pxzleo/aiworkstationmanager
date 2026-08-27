from __future__ import annotations

import asyncio
import ctypes
import json
import math
import multiprocessing
import ntpath
import os
import re
import shlex
import subprocess
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, BinaryIO, Callable, Literal, Protocol
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request as UrlRequest, build_opener

import psutil

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .database import Database, DatabaseError, utc_now
from .discovery import redact_sensitive_text


ID_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
DISTRO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
UNIT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}\.service$"
SERVICE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
MAX_OUTPUT_CHARS = 8192
ADAPTER_STATUS_TIMEOUT_SECONDS = 5.0
COMFY_PORT_GPU_MAPPING = {8000: 0, 8001: 1, 8189: 1}


class ControlError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class ControlConfigError(ValueError):
    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class WslSystemdConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["wsl_systemd"]
    distro: str = Field(min_length=1, max_length=64, pattern=DISTRO_PATTERN)
    scope: Literal["system", "user"]
    service: str = Field(min_length=9, max_length=128, pattern=UNIT_PATTERN)
    timeout_seconds: float = Field(default=30, ge=1, le=660)


class WslSystemdRootConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["wsl_systemd_root"]
    distro: str = Field(min_length=1, max_length=64, pattern=DISTRO_PATTERN)
    service: str = Field(min_length=9, max_length=128, pattern=UNIT_PATTERN)
    timeout_seconds: float = Field(default=30, ge=1, le=120)


class WslDockerComposeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["wsl_docker_compose"]
    distro: str = Field(min_length=1, max_length=64, pattern=DISTRO_PATTERN)
    project_dir: str = Field(min_length=2, max_length=512)
    service: str = Field(min_length=1, max_length=64, pattern=SERVICE_PATTERN)
    project: str | None = Field(default=None, min_length=1, max_length=64, pattern=SERVICE_PATTERN)
    timeout_seconds: float = Field(default=60, ge=1, le=660)

    @field_validator("project_dir")
    @classmethod
    def validate_project_dir(cls, value: str) -> str:
        if "\x00" in value or "\\" in value or not value.startswith("/"):
            raise ValueError("project_dir 必须是严格绝对 WSL 路径")
        path = PurePosixPath(value)
        if ".." in path.parts or str(path) != value.rstrip("/"):
            raise ValueError("project_dir 必须是规范化绝对 WSL 路径")
        if not re.fullmatch(r"/[A-Za-z0-9._/@+:-]+(?:/[A-Za-z0-9._@+:-]+)*", value):
            raise ValueError("project_dir 包含不允许的字符")
        return value


WINDOWS_PATH_FIELDS = (
    "python_executable", "main_path", "working_directory", "base_directory",
    "user_directory", "database_path", "extra_model_paths_config",
    "input_directory", "output_directory",
)


def _strict_windows_path(value: str) -> str:
    if ("\x00" in value or len(value) > 512 or re.match(r"^[A-Za-z]:\\", value) is None
            or value.startswith(("\\\\", "\\??\\")) or "*" in value or "?" in value
            or any(part == ".." for part in value.replace("/", "\\").split("\\"))
            or any(part.endswith((" ", ".")) for part in value.split("\\") if part)
            or ntpath.normpath(value) != value):
        raise ValueError("路径必须是规范化的本地盘符绝对 Windows 路径")
    if ":" in value[2:]:
        raise ValueError("Windows 路径不得包含 alternate data stream")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    reserved.update(f"COM{index}" for index in range(1, 10))
    reserved.update(f"LPT{index}" for index in range(1, 10))
    for segment in value[3:].split("\\"):
        device_name = segment.split(".", 1)[0].upper()
        if device_name in reserved:
            raise ValueError("Windows 路径不得包含保留设备名")
    return value


def _windows_reparse_error(path: str,
                           get_attributes: Callable[[str], int] | None = None) -> str | None:
    """逐级拒绝 symlink/junction/其他 reparse；不能只检查最终目标。"""
    if get_attributes is None:
        if os.name != "nt":
            current = Path(ntpath.splitdrive(path)[0] + "\\")
            for segment in path[3:].split("\\"):
                current = current / segment
                if current.is_symlink():
                    return f"父级或目标为 reparse: {current.name}"
            return None
        get_file_attributes = ctypes.windll.kernel32.GetFileAttributesW
        get_file_attributes.argtypes = [ctypes.c_wchar_p]
        get_file_attributes.restype = ctypes.c_uint32
        get_attributes = lambda item: int(get_file_attributes(item))
    current = ntpath.splitdrive(path)[0] + "\\"
    for segment in path[3:].split("\\"):
        current = ntpath.join(current, segment)
        attributes = get_attributes(current)
        if attributes == 0xFFFFFFFF:
            return f"无法读取路径属性: {segment}"
        if attributes & 0x400:
            return f"父级或目标为 reparse: {segment}"
    return None


def _windows_probe_worker(probe: Callable[[str], tuple[bool, int, str | None]],
                          path: str, connection: Any) -> None:
    try:
        connection.send(("ok", probe(path)))
    except BaseException as exc:
        connection.send(("error", type(exc).__name__))
    finally:
        connection.close()


class WindowsComfyProcessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["windows_comfyui_process"]
    python_executable: str = Field(min_length=3, max_length=512)
    main_path: str = Field(min_length=3, max_length=512)
    working_directory: str = Field(min_length=3, max_length=512)
    base_directory: str = Field(min_length=3, max_length=512)
    user_directory: str = Field(min_length=3, max_length=512)
    database_path: str = Field(min_length=3, max_length=512)
    extra_model_paths_config: str = Field(min_length=3, max_length=512)
    input_directory: str = Field(min_length=3, max_length=512)
    output_directory: str = Field(min_length=3, max_length=512)
    host: Literal["127.0.0.1"]
    port: Literal[8000, 8001, 8189]
    cuda_device: Literal[0, 1]
    target_gpu_uuid: str = Field(pattern=r"^GPU-[A-Fa-f0-9-]{16,64}$", max_length=68)
    target_host_gpu_index: Literal[0, 1]
    expected_comfy_device_index: Literal[0]
    startup_timeout_seconds: float = Field(default=60, ge=1, le=120)
    stop_timeout_seconds: float = Field(default=30, ge=1, le=120)

    @field_validator(*WINDOWS_PATH_FIELDS)
    @classmethod
    def validate_windows_paths(cls, value: str) -> str:
        return _strict_windows_path(value)

    @model_validator(mode="after")
    def validate_contract(self) -> "WindowsComfyProcessConfig":
        if not self.python_executable.lower().endswith(".exe"):
            raise ValueError("python_executable 必须是 .exe")
        if ntpath.basename(self.python_executable).lower() != "python.exe":
            raise ValueError("只允许固定 Python 解释器，不允许 Comfy Desktop GUI")
        if ntpath.basename(self.main_path).lower() != "main.py":
            raise ValueError("main_path 必须指向 main.py")
        if not self.database_path.lower().endswith(".db"):
            raise ValueError("database_path 必须是 .db")
        if not self.extra_model_paths_config.lower().endswith((".yaml", ".yml")):
            raise ValueError("extra_model_paths_config 必须是 YAML")
        expected = COMFY_PORT_GPU_MAPPING[self.port]
        if self.cuda_device != expected or self.target_host_gpu_index != expected:
            raise ValueError("Comfy port 必须绑定登记的 host GPU 与 cuda-device")
        return self

    def command_args(self) -> list[str]:
        database_uri = self.database_path.replace("\\", "/")
        return [self.python_executable, "-s", self.main_path,
                "--base-directory", self.base_directory,
                "--user-directory", self.user_directory,
                "--database-url", f"sqlite:///{database_uri}",
                "--port", str(self.port), "--listen", self.host,
                "--enable-manager", "--cuda-device", str(self.cuda_device),
                "--extra-model-paths-config", self.extra_model_paths_config,
                "--input-directory", self.input_directory,
                "--output-directory", self.output_directory]


AdapterConfig = Annotated[
    WslSystemdConfig | WslSystemdRootConfig | WslDockerComposeConfig |
    WindowsComfyProcessConfig, Field(discriminator="type")
]


class HealthCheckConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["adapter_status"]


class LoopbackTcpHealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["loopback_tcp"]
    host: Literal["127.0.0.1", "::1", "localhost"] = "127.0.0.1"
    port: int = Field(ge=1, le=65535)
    timeout_seconds: float = Field(default=3, ge=.1, le=10)


JsonScalar = str | int | float | bool | None


class LoopbackHttpHealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["loopback_http"]
    url: str = Field(min_length=10, max_length=512)
    expected_status: int = Field(default=200, ge=100, le=599)
    json_equals: dict[str, JsonScalar] = Field(default_factory=dict, max_length=32)
    timeout_seconds: float = Field(default=5, ge=.1, le=15)

    @field_validator("url")
    @classmethod
    def validate_loopback_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.port is None or not parsed.path.startswith("/")):
            raise ValueError("url 必须是带固定端口和绝对路径的 loopback HTTP 地址")
        return value

    @field_validator("json_equals")
    @classmethod
    def validate_json_paths(cls, value: dict[str, JsonScalar]) -> dict[str, JsonScalar]:
        for path, expected in value.items():
            if re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){0,7}", path) is None:
                raise ValueError("json_equals 键必须是有限深度的严格字段路径")
            if isinstance(expected, str) and len(expected) > 512:
                raise ValueError("json_equals 字符串值过长")
        return value


class NvidiaGpuProcessHealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["nvidia_gpu_process"]
    gpu_uuid: str = Field(pattern=r"^GPU-[A-Fa-f0-9-]{16,64}$", max_length=68)
    process_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$", max_length=128)
    timeout_seconds: float = Field(default=5, ge=1, le=15)


class WslSystemdGpuBindingHealthCheck(BaseModel):
    """核对 host GPU UUID/index 映射与固定 user unit 的 CUDA 绑定。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["wsl_systemd_gpu_binding"]
    distro: str = Field(min_length=1, max_length=64, pattern=DISTRO_PATTERN)
    service: str = Field(min_length=9, max_length=128, pattern=UNIT_PATTERN)
    host_gpu_index: Literal[0, 1]
    gpu_uuid: str = Field(pattern=r"^GPU-[A-Fa-f0-9-]{16,64}$", max_length=68)
    cuda_visible_device: Literal[0, 1]
    timeout_seconds: float = Field(default=5, ge=1, le=15)

    @model_validator(mode="after")
    def validate_gpu_mapping(self) -> "WslSystemdGpuBindingHealthCheck":
        if self.host_gpu_index != self.cuda_visible_device:
            raise ValueError("当前 WSL GPU 绑定要求 host index 与 CUDA_VISIBLE_DEVICES 一致")
        return self


class WslDockerComposeGpuBindingHealthCheck(BaseModel):
    """交叉核对宿主映射、Compose 设备声明、容器环境与容器可见 GPU。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["wsl_docker_compose_gpu_binding"]
    distro: str = Field(min_length=1, max_length=64, pattern=DISTRO_PATTERN)
    project_dir: str = Field(min_length=2, max_length=512)
    service: str = Field(min_length=1, max_length=64, pattern=SERVICE_PATTERN)
    project: str | None = Field(default=None, min_length=1, max_length=64,
                                pattern=SERVICE_PATTERN)
    host_gpu_index: Literal[0, 1]
    gpu_uuid: str = Field(pattern=r"^GPU-[A-Fa-f0-9-]{16,64}$", max_length=68)
    cuda_visible_device: str = Field(pattern=r"^GPU-[A-Fa-f0-9-]{16,64}$", max_length=68)
    timeout_seconds: float = Field(default=8, ge=1, le=15)

    @field_validator("project_dir")
    @classmethod
    def validate_project_dir(cls, value: str) -> str:
        return WslDockerComposeConfig.validate_project_dir(value)

    @model_validator(mode="after")
    def validate_gpu_uuid(self) -> "WslDockerComposeGpuBindingHealthCheck":
        if self.cuda_visible_device != self.gpu_uuid:
            raise ValueError("CUDA_VISIBLE_DEVICES 必须使用同一个完整 GPU UUID")
        return self


class HttpJsonObjectHasKeysCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["http_json_object_has_keys"]
    url: str = Field(min_length=10, max_length=512)
    required_keys: tuple[str, ...] = Field(min_length=1, max_length=32)
    expected_status: int = Field(default=200, ge=100, le=599)
    timeout_seconds: float = Field(default=5, ge=.1, le=15)
    max_body_bytes: Literal[65536] = 65536

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return LoopbackHttpHealthCheck.validate_loopback_url(value)

    @field_validator("required_keys")
    @classmethod
    def validate_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("required_keys 不得重复")
        if any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key) is None
               for key in values):
            raise ValueError("required_keys 含无效 key")
        return values


class WindowsComfyCapabilityHealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["windows_comfy_capability_health"]
    system_stats_url: str = Field(min_length=10, max_length=512)
    queue_url: str = Field(min_length=10, max_length=512)
    object_info_url: str = Field(min_length=10, max_length=512)
    target_gpu_uuid: str = Field(pattern=r"^GPU-[A-Fa-f0-9-]{16,64}$", max_length=68)
    target_gpu_name: Literal["NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 3090"]
    target_host_gpu_index: Literal[0, 1]
    expected_comfy_device_index: Literal[0]
    required_node_classes: tuple[str, ...] = Field(min_length=1, max_length=32)
    timeout_seconds: float = Field(default=5, ge=.1, le=15)
    max_system_stats_bytes: Literal[65536] = 65536
    max_queue_bytes: Literal[65536] = 65536
    max_object_info_bytes: Literal[8388608] = 8388608

    @field_validator("required_node_classes")
    @classmethod
    def validate_node_classes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("required_node_classes 不得重复")
        if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", value) is None
               for value in values):
            raise ValueError("required_node_classes 含无效节点名")
        return values

    @model_validator(mode="after")
    def validate_urls(self) -> "WindowsComfyCapabilityHealthCheck":
        pairs = []
        for value, expected_path in ((self.system_stats_url, "/system_stats"),
                                     (self.queue_url, "/queue"),
                                     (self.object_info_url, "/object_info")):
            LoopbackHttpHealthCheck.validate_loopback_url(value)
            parsed = urlsplit(value)
            if parsed.hostname != "127.0.0.1" or parsed.path != expected_path:
                raise ValueError(f"Comfy URL path 必须精确为 {expected_path}")
            pairs.append((parsed.hostname, parsed.port))
        if len(set(pairs)) != 1:
            raise ValueError("Comfy 三个 URL 必须使用相同 loopback host/port")
        port = pairs[0][1]
        if (port not in COMFY_PORT_GPU_MAPPING
                or self.target_host_gpu_index != COMFY_PORT_GPU_MAPPING[port]):
            raise ValueError("Comfy port 与 host GPU index 映射不匹配")
        return self


HealthCheck = Annotated[
    HealthCheckConfig | LoopbackTcpHealthCheck | LoopbackHttpHealthCheck |
    NvidiaGpuProcessHealthCheck | WslSystemdGpuBindingHealthCheck |
    WslDockerComposeGpuBindingHealthCheck |
    HttpJsonObjectHasKeysCheck |
    WindowsComfyCapabilityHealthCheck,
    Field(discriminator="type"),
]


class DrainHttpJsonCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["drain_http_json"]
    purpose: Literal["active_requests", "comfy_queue"]
    url: str = Field(min_length=10, max_length=512)
    json_paths: tuple[str, ...] = Field(min_length=1, max_length=8)
    timeout_seconds: float = Field(default=3, ge=.1, le=10)
    wait_timeout_seconds: float = Field(default=60, ge=1, le=600)
    poll_interval_seconds: float = Field(default=1, ge=.1, le=10)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return LoopbackHttpHealthCheck.validate_loopback_url(value)

    @field_validator("json_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("json_paths 不得重复")
        for path in values:
            if re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){0,7}", path) is None:
                raise ValueError("json_paths 必须是有限深度严格字段路径")
        return values

    @model_validator(mode="after")
    def validate_purpose_paths(self) -> "DrainHttpJsonCheck":
        if self.purpose == "comfy_queue":
            leaf_names = {path.rsplit(".", 1)[-1] for path in self.json_paths}
            if leaf_names != {"pending", "running"} or len(self.json_paths) != 2:
                raise ValueError("comfy_queue 必须同时精确检查 pending 与 running")
        return self


class PrometheusSeries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    metric: str = Field(min_length=1, max_length=128,
                        pattern=r"^[A-Za-z_:][A-Za-z0-9_:]*$")
    labels: dict[str, str] = Field(default_factory=dict, max_length=8)

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, labels: dict[str, str]) -> dict[str, str]:
        for key, value in labels.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None or len(value) > 256:
                raise ValueError("Prometheus labels 必须是有限长度的精确键值")
        return labels


class DrainHttpPrometheusCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["drain_http_prometheus"]
    purpose: Literal["active_requests"]
    url: str = Field(min_length=10, max_length=512)
    series: tuple[PrometheusSeries, ...] = Field(min_length=1, max_length=8)
    timeout_seconds: float = Field(default=3, ge=.1, le=10)
    wait_timeout_seconds: float = Field(default=60, ge=1, le=600)
    poll_interval_seconds: float = Field(default=1, ge=.1, le=10)
    max_body_bytes: Literal[65536] = 65536

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return LoopbackHttpHealthCheck.validate_loopback_url(value)

    @field_validator("series")
    @classmethod
    def validate_series(cls, values: tuple[PrometheusSeries, ...]) -> tuple[PrometheusSeries, ...]:
        identities = [(item.metric, tuple(sorted(item.labels.items()))) for item in values]
        if len(set(identities)) != len(identities):
            raise ValueError("Prometheus series 不得重复")
        return values


class DrainHttpJsonArraysCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["drain_http_json_arrays"]
    purpose: Literal["comfy_queue"]
    url: str = Field(min_length=10, max_length=512)
    running_path: Literal["queue_running"] = "queue_running"
    pending_path: Literal["queue_pending"] = "queue_pending"
    timeout_seconds: float = Field(default=3, ge=.1, le=10)
    wait_timeout_seconds: float = Field(default=60, ge=1, le=600)
    poll_interval_seconds: float = Field(default=1, ge=.1, le=10)
    max_body_bytes: Literal[65536] = 65536

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return LoopbackHttpHealthCheck.validate_loopback_url(value)


class NvidiaGpuMemoryCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["nvidia_gpu_memory"]
    gpu_uuid: str = Field(pattern=r"^GPU-[A-Fa-f0-9-]{16,64}$", max_length=68)
    host_gpu_index: int | None = Field(default=None, ge=0, le=31)
    min_free_mib: int = Field(ge=256, le=262144)
    timeout_seconds: float = Field(default=5, ge=1, le=15)


class WslPathDiskCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["wsl_path_disk"]
    purpose: Literal["model", "lora"]
    distro: str = Field(min_length=1, max_length=64, pattern=DISTRO_PATTERN)
    path: str = Field(min_length=2, max_length=512)
    min_free_mib: int = Field(ge=256, le=16_777_216)
    timeout_seconds: float = Field(default=8, ge=1, le=30)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if ("\x00" in value or "\\" in value or not value.startswith("/")
                or ".." in PurePosixPath(value).parts
                or re.fullmatch(r"/[A-Za-z0-9._/@+:-]+(?:/[A-Za-z0-9._@+:-]+)*", value) is None):
            raise ValueError("path 必须是严格规范化绝对 WSL 路径")
        return value


class WindowsPathDiskCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["windows_path_disk"]
    purpose: Literal["model", "lora"]
    path: str = Field(min_length=3, max_length=512)
    min_free_gib: int = Field(ge=1, le=16384)
    timeout_seconds: float = Field(default=8, ge=1, le=30)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _strict_windows_path(value)


class RequiredDependencyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["required_dependency"]
    environment_id: str = Field(pattern=ID_PATTERN, max_length=64)


class LoopbackPortAvailableCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["loopback_port_available"]
    port: int = Field(ge=1, le=65535)
    owner_environment_id: str = Field(pattern=ID_PATTERN, max_length=64)
    timeout_seconds: float = Field(default=.5, ge=.1, le=3)


class WslPortAvailableCheck(BaseModel):
    """在指定 WSL 内部检查监听端口，避免 Windows portproxy 造成误判。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["wsl_port_available"]
    distro: str = Field(min_length=1, max_length=64, pattern=DISTRO_PATTERN)
    port: int = Field(ge=1, le=65535)
    owner_environment_id: str = Field(pattern=ID_PATTERN, max_length=64)
    timeout_seconds: float = Field(default=3, ge=1, le=10)


class H3VideoProfileCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["h3_video_profile"]
    steps: Literal[8]
    lora_name: str = Field(min_length=1, max_length=256,
                           pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,255}$")
    shift_video: Literal[12]
    shift_audio: Literal[3]

    @field_validator("lora_name")
    @classmethod
    def validate_lora(cls, value: str) -> str:
        if "8step" not in value.lower():
            raise ValueError("H3 LoRA 名称必须明确包含 8step 兼容标记")
        return value


PreflightCheck = Annotated[
    DrainHttpJsonCheck | DrainHttpPrometheusCheck | DrainHttpJsonArraysCheck |
    NvidiaGpuMemoryCheck | WslPathDiskCheck | WindowsPathDiskCheck |
    RequiredDependencyCheck | LoopbackPortAvailableCheck | WslPortAvailableCheck |
    H3VideoProfileCheck,
    Field(discriminator="type"),
]


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=ID_PATTERN, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    configured: bool = False
    missing_reason: str | None = Field(default=None, max_length=512)
    adapter: AdapterConfig | None = None
    gpu_ai: bool = False
    health_checks: tuple[HealthCheck, ...] = Field(default=(), max_length=16)
    safety_profile: Literal["standard", "gpu_ai", "h3_video"] = "standard"
    preflight_checks: tuple[PreflightCheck, ...] = Field(default=(), max_length=32)
    allowed_actions: tuple[Literal["start", "stop", "restart"], ...] = ()
    startup_health_timeout_seconds: float = Field(default=0, ge=0, le=660)
    startup_health_poll_interval_seconds: float = Field(default=1, ge=.1, le=10)

    @model_validator(mode="after")
    def validate_completeness(self) -> "EnvironmentConfig":
        if self.configured and self.adapter is None:
            raise ValueError("configured=true 时 adapter 必须完整")
        if not self.configured and self.allowed_actions:
            raise ValueError("未配置环境不得声明 allowed_actions")
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("allowed_actions 不得重复")
        if self.safety_profile in {"gpu_ai", "h3_video"} and not self.gpu_ai:
            raise ValueError("GPU/H3 safety_profile 必须同时声明 gpu_ai=true")
        return self


class SceneConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=ID_PATTERN, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    desired: tuple[str, ...] = Field(max_length=32)
    optional_desired: tuple[str, ...] = Field(default=(), max_length=32)
    conflicts: tuple[str, ...] = Field(max_length=32)

    @field_validator("desired", "optional_desired", "conflicts")
    @classmethod
    def validate_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("环境 ID 不得重复")
        if any(re.fullmatch(ID_PATTERN, value) is None for value in values):
            raise ValueError("环境 ID 格式无效")
        return values

    @model_validator(mode="after")
    def validate_disjoint(self) -> "SceneConfig":
        if (set(self.desired) & set(self.conflicts)
                or set(self.optional_desired) & (set(self.desired) | set(self.conflicts))):
            raise ValueError("desired、optional_desired 与 conflicts 不得重叠")
        return self


class ControlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    control_enabled: bool = False
    source: Literal["configured", "example_preview"] = Field(default="configured", exclude=True)
    environments: tuple[EnvironmentConfig, ...] = Field(default=(), max_length=256)
    scenes: tuple[SceneConfig, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_references(self) -> "ControlConfig":
        environment_ids = [item.id for item in self.environments]
        scene_ids = [item.id for item in self.scenes]
        if len(set(environment_ids)) != len(environment_ids):
            raise ValueError("环境 ID 不得重复")
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("场景 ID 不得重复")
        known = set(environment_ids)
        for scene in self.scenes:
            unknown = (set(scene.desired) | set(scene.optional_desired) | set(scene.conflicts)) - known
            if unknown:
                raise ValueError(f"场景 {scene.id} 引用了未知环境: {sorted(unknown)}")
        for environment in self.environments:
            for check in environment.preflight_checks:
                if isinstance(check, RequiredDependencyCheck):
                    if check.environment_id not in known:
                        raise ValueError(
                            f"环境 {environment.id} 引用了未知依赖: {check.environment_id}")
                    if check.environment_id == environment.id:
                        raise ValueError(f"环境 {environment.id} 不得依赖自身")
                if (isinstance(check, (LoopbackPortAvailableCheck, WslPortAvailableCheck))
                        and check.owner_environment_id not in known):
                    raise ValueError(
                        f"环境 {environment.id} 的端口 owner 未登记: {check.owner_environment_id}")
        return self


def load_control_config(path: Path) -> ControlConfig:
    try:
        exists = path.exists()
    except OSError as exc:
        raise ControlConfigError("control_config_unreadable", "无法检查控制配置", str(exc)) from exc
    if not exists:
        example_path = path.with_name("control.example.json")
        if not example_path.exists():
            return ControlConfig(source="example_preview")
        path = example_path
        preview = True
    else:
        preview = False
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ControlConfigError("control_config_unreadable", "无法读取控制配置", str(exc)) from exc
    if len(raw) > 256 * 1024:
        raise ControlConfigError("control_config_too_large", "控制配置超过 256 KiB")
    try:
        loaded = ControlConfig.model_validate_json(raw)
        return loaded.model_copy(update={
            "control_enabled": False if preview else loaded.control_enabled,
            "source": "example_preview" if preview else "configured",
        })
    except ValidationError as exc:
        raise ControlConfigError(
            "invalid_control_config", "控制配置校验失败", exc.errors(include_url=False)
        ) from exc


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class SubprocessRunner:
    """只执行适配器生成的固定参数数组；不接受调用方命令文本。"""

    def __call__(self, args: list[str], timeout: float) -> CommandResult:
        startupinfo = None
        creationflags = 0
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                args, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, check=False, startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise ControlError(504, "adapter_timeout", "适配器执行超时") from exc
        except OSError as exc:
            safe, _ = redact_sensitive_text(str(exc))
            raise ControlError(500, "adapter_spawn_failed", "无法启动固定适配器", safe) from exc
        stdout = redact_sensitive_text(completed.stdout[:MAX_OUTPUT_CHARS])[0]
        stderr = redact_sensitive_text(completed.stderr[:MAX_OUTPUT_CHARS])[0]
        return CommandResult(completed.returncode, stdout, stderr)


Runner = Callable[[list[str], float], CommandResult]


class OperationFileLock:
    """数据库旁的单字节独占锁；进程退出时由操作系统自动释放。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: BinaryIO | None = None

    def acquire(self) -> bool:
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0"); handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class Adapter:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def status(self) -> str:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def restart(self) -> None:
        raise NotImplementedError

    def _run_action(self, args: list[str], timeout: float) -> None:
        result = self.runner(args, timeout)
        if result.returncode != 0:
            raise ControlError(
                502, "adapter_action_failed", "固定适配器动作失败",
                {"returncode": result.returncode, "stderr": result.stderr},
            )


class WslSystemdAdapter(Adapter):
    def __init__(self, config: WslSystemdConfig, runner: Runner) -> None:
        super().__init__(runner)
        self.config = config

    def _args(self, verb: str) -> list[str]:
        args = ["wsl.exe", "-d", self.config.distro, "--", "systemctl"]
        if self.config.scope == "user":
            args.append("--user")
        return [*args, verb, self.config.service]

    def status(self) -> str:
        result = self.runner(
            self._args("is-active"),
            min(self.config.timeout_seconds, ADAPTER_STATUS_TIMEOUT_SECONDS),
        )
        state = result.stdout.strip().lower()
        if result.returncode == 0 and state == "active":
            return "running"
        if state == "inactive" or (result.returncode == 3 and state != "failed"):
            return "stopped"
        if state == "failed":
            return "failed"
        return "unknown"

    def start(self) -> None: self._run_action(self._args("start"), self.config.timeout_seconds)
    def stop(self) -> None: self._run_action(self._args("stop"), self.config.timeout_seconds)
    def restart(self) -> None: self._run_action(self._args("restart"), self.config.timeout_seconds)


class WslSystemdRootAdapter(Adapter):
    def __init__(self, config: WslSystemdRootConfig, runner: Runner) -> None:
        super().__init__(runner)
        self.config = config

    def _args(self, verb: Literal["is-active", "start", "stop", "restart"]) -> list[str]:
        return ["wsl.exe", "-d", self.config.distro, "-u", "root", "--",
                "systemctl", verb, self.config.service]

    def status(self) -> str:
        result = self.runner(
            self._args("is-active"),
            min(self.config.timeout_seconds, ADAPTER_STATUS_TIMEOUT_SECONDS),
        )
        state = result.stdout.strip().lower()
        if result.returncode == 0 and state == "active":
            return "running"
        if state == "inactive" or (result.returncode == 3 and state != "failed"):
            return "stopped"
        if state == "failed":
            return "failed"
        return "unknown"

    def start(self) -> None: self._run_action(self._args("start"), self.config.timeout_seconds)
    def stop(self) -> None: self._run_action(self._args("stop"), self.config.timeout_seconds)
    def restart(self) -> None: self._run_action(self._args("restart"), self.config.timeout_seconds)


class WslDockerComposeAdapter(Adapter):
    def __init__(self, config: WslDockerComposeConfig, runner: Runner) -> None:
        super().__init__(runner)
        self.config = config

    def _base(self) -> list[str]:
        args = ["wsl.exe", "-d", self.config.distro, "--", "docker", "compose"]
        if self.config.project:
            args += ["--project-name", self.config.project]
        return [*args, "--project-directory", self.config.project_dir]

    def status(self) -> str:
        args = [*self._base(), "ps", "--status", "running", "--services", self.config.service]
        result = self.runner(
            args, min(self.config.timeout_seconds, ADAPTER_STATUS_TIMEOUT_SECONDS)
        )
        if result.returncode != 0:
            return "unknown"
        return "running" if self.config.service in result.stdout.splitlines() else "stopped"

    def start(self) -> None:
        self._run_action([*self._base(), "up", "-d", self.config.service], self.config.timeout_seconds)

    def stop(self) -> None:
        self._run_action([*self._base(), "stop", self.config.service], self.config.timeout_seconds)

    def restart(self) -> None:
        self._run_action([*self._base(), "restart", self.config.service], self.config.timeout_seconds)


class WindowsProcessApi(Protocol):
    def status(self, config: WindowsComfyProcessConfig,
               command: list[str]) -> tuple[str, int | None]: ...
    def start(self, config: WindowsComfyProcessConfig, command: list[str]) -> int: ...
    def terminate(self, pid: int, timeout: float) -> None: ...
    def owns(self, pid: int, command: list[str]) -> bool: ...


class DefaultWindowsProcessApi:
    """通过 PID、完整 exe/cmdline 指纹和监听端口 owner 交叉确认，不按名称接管。"""

    def __init__(self) -> None:
        self._owned: dict[tuple[str, ...], tuple[int, float]] = {}

    @staticmethod
    def _same_command(actual: list[str], expected: list[str]) -> bool:
        if len(actual) != len(expected):
            return False
        return all(ntpath.normcase(left) == ntpath.normcase(right)
                   for left, right in zip(actual, expected))

    def status(self, config: WindowsComfyProcessConfig,
               command: list[str]) -> tuple[str, int | None]:
        matching: list[int] = []
        try:
            for process in psutil.process_iter(["pid", "exe", "cmdline"]):
                info = process.info
                exe = info.get("exe")
                cmdline = info.get("cmdline") or []
                if (isinstance(exe, str)
                        and ntpath.normcase(ntpath.normpath(exe)) ==
                        ntpath.normcase(ntpath.normpath(config.python_executable))
                        and self._same_command(list(cmdline), command)):
                    pid = int(info["pid"])
                    matching.append(pid)
            owners = {int(conn.pid) for conn in psutil.net_connections(kind="tcp")
                      if conn.pid is not None and conn.status == psutil.CONN_LISTEN
                      and conn.laddr and int(conn.laddr.port) == config.port
                      and str(conn.laddr.ip) == config.host}
        except (OSError, psutil.Error, ValueError, AttributeError):
            return "unknown", None
        fingerprint = tuple(command)
        if len(matching) == 1 and owners == {matching[0]}:
            return "running", matching[0]
        if not matching and not owners:
            self._owned.pop(fingerprint, None)
            return "stopped", None
        return "unknown", None

    def owns(self, pid: int, command: list[str]) -> bool:
        owned = self._owned.get(tuple(command))
        if owned is None or owned[0] != pid:
            return False
        try:
            return float(psutil.Process(pid).create_time()) == owned[1]
        except (psutil.Error, OSError, ValueError):
            return False

    def start(self, config: WindowsComfyProcessConfig, command: list[str]) -> int:
        startupinfo = None
        creationflags = 0
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                command, cwd=config.working_directory, shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                startupinfo=startupinfo, creationflags=creationflags, close_fds=True,
            )
        except OSError as exc:
            safe, _ = redact_sensitive_text(str(exc))
            raise ControlError(500, "adapter_spawn_failed", "无法启动固定 ComfyUI 进程", safe) from exc
        pid = int(process.pid)
        try:
            create_time = float(psutil.Process(pid).create_time())
        except (psutil.Error, OSError, ValueError) as exc:
            process.terminate()
            try:
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            raise ControlError(500, "adapter_spawn_failed",
                               "ComfyUI 已启动但无法记录 PID 创建时间，已撤销启动") from exc
        self._owned[tuple(command)] = (pid, create_time)
        return pid

    def terminate(self, pid: int, timeout: float) -> None:
        owned = next(((fingerprint, value) for fingerprint, value in self._owned.items()
                      if value[0] == pid), None)
        if owned is None:
            raise ControlError(409, "adapter_ownership_unknown", "PID 不属于当前管理器启动记录")
        try:
            process = psutil.Process(pid)
            if float(process.create_time()) != owned[1][1]:
                raise ControlError(409, "adapter_ownership_unknown", "PID 创建时间已变化，拒绝终止")
            process.terminate()
            process.wait(timeout=timeout)
        except psutil.TimeoutExpired as exc:
            raise ControlError(504, "adapter_timeout", "等待 ComfyUI 精确 PID 退出超时") from exc
        except psutil.NoSuchProcess:
            return
        except (psutil.AccessDenied, OSError) as exc:
            raise ControlError(502, "adapter_action_failed", "无法终止 ComfyUI 精确 PID") from exc
        finally:
            if not psutil.pid_exists(pid):
                self._owned.pop(owned[0], None)


class WindowsComfyProcessAdapter(Adapter):
    def __init__(self, config: WindowsComfyProcessConfig,
                 process_api: WindowsProcessApi | None = None) -> None:
        self.config = config
        self.process_api = process_api or DefaultWindowsProcessApi()
        self.command = config.command_args()

    def status(self) -> str:
        return self.process_api.status(self.config, self.command)[0]

    def start(self) -> None:
        state, _ = self.process_api.status(self.config, self.command)
        if state != "stopped":
            if state == "running":
                return
            raise ControlError(409, "adapter_ownership_unknown", "ComfyUI PID/端口归属不完整，拒绝接管")
        self.process_api.start(self.config, self.command)
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() < deadline:
            state, _ = self.process_api.status(self.config, self.command)
            if state == "running":
                return
            if state == "unknown":
                raise ControlError(409, "adapter_ownership_unknown", "ComfyUI 启动后 PID/端口归属不完整")
            time.sleep(.1)
        raise ControlError(504, "adapter_timeout", "等待 ComfyUI 固定端口收敛超时")

    def stop(self) -> None:
        state, pid = self.process_api.status(self.config, self.command)
        if state == "stopped":
            return
        if state != "running" or pid is None:
            raise ControlError(409, "adapter_ownership_unknown", "ComfyUI PID/端口归属不完整，拒绝终止")
        owns = getattr(self.process_api, "owns", None)
        if callable(owns) and not owns(pid, self.command):
            raise ControlError(409, "adapter_ownership_unknown",
                               "ComfyUI 不是当前管理器记录的 PID/创建时间，拒绝终止")
        self.process_api.terminate(pid, self.config.stop_timeout_seconds)
        after, _ = self.process_api.status(self.config, self.command)
        if after != "stopped":
            raise ControlError(502, "adapter_action_failed", "ComfyUI 精确 PID 终止后状态未收敛")

    def restart(self) -> None:
        self.stop()
        self.start()


def make_adapter(config: AdapterConfig, runner: Runner,
                 process_api: WindowsProcessApi | None = None) -> Adapter:
    if isinstance(config, WslSystemdConfig):
        return WslSystemdAdapter(config, runner)
    if isinstance(config, WslSystemdRootConfig):
        return WslSystemdRootAdapter(config, runner)
    if isinstance(config, WslDockerComposeConfig):
        return WslDockerComposeAdapter(config, runner)
    if isinstance(config, WindowsComfyProcessConfig):
        return WindowsComfyProcessAdapter(config, process_api)
    raise ControlError(500, "adapter_type_unsupported", "不支持的适配器类型")


@dataclass(frozen=True)
class HealthResult:
    healthy: bool
    message: str
    status: Literal["healthy", "failed", "unknown"] | None = None

    def __post_init__(self) -> None:
        if self.status is None:
            object.__setattr__(self, "status", "healthy" if self.healthy else "failed")


class HealthProbe(Protocol):
    def check(self, check: HealthCheck) -> HealthResult: ...


class SafetyProbe(Protocol):
    def check(self, check: PreflightCheck) -> HealthResult: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class DefaultHealthProbe:
    def __init__(self, runner: Runner,
                 json_fetcher: Callable[[str, int, float], Any] | None = None) -> None:
        self.runner = runner
        self.json_fetcher = json_fetcher

    def check(self, check: HealthCheck) -> HealthResult:
        if isinstance(check, LoopbackTcpHealthCheck):
            try:
                with socket.create_connection((check.host, check.port), check.timeout_seconds):
                    return HealthResult(True, "loopback TCP 可连接")
            except OSError as exc:
                return HealthResult(False, f"loopback TCP 不可连接: {type(exc).__name__}")
        if isinstance(check, LoopbackHttpHealthCheck):
            return self._http(check)
        if isinstance(check, NvidiaGpuProcessHealthCheck):
            return self._gpu_process(check)
        if isinstance(check, WslSystemdGpuBindingHealthCheck):
            return self._wsl_systemd_gpu_binding(check)
        if isinstance(check, WslDockerComposeGpuBindingHealthCheck):
            return self._wsl_docker_compose_gpu_binding(check)
        if isinstance(check, HttpJsonObjectHasKeysCheck):
            fetched = self._fetch_json_bytes(check.url, check.max_body_bytes,
                                             check.timeout_seconds, check.expected_status)
            if isinstance(fetched, HealthResult):
                return fetched
            return self._object_has_keys(check, fetched)
        if isinstance(check, WindowsComfyCapabilityHealthCheck):
            return self._comfy_capability(check)
        return HealthResult(False, "adapter_status 必须由控制面核验")

    def _fetch_json_bytes(self, url: str, limit: int, timeout: float,
                          expected_status: int = 200) -> bytes | HealthResult:
        if self.json_fetcher is not None:
            try:
                value = self.json_fetcher(url, limit, timeout)
                encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
                if len(encoded) > limit:
                    return HealthResult(False, f"HTTP JSON 响应超过 {limit} bytes", "unknown")
                return encoded
            except Exception as exc:
                return HealthResult(False, f"HTTP JSON 获取失败: {type(exc).__name__}", "unknown")
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        request = UrlRequest(url, method="GET", headers={"Accept": "application/json"})
        try:
            with opener.open(request, timeout=timeout) as response:
                status = int(response.status)
                body = response.read(limit + 1)
        except HTTPError as exc:
            status = int(exc.code)
            body = exc.read(limit + 1)
        except Exception as exc:
            return HealthResult(False, f"HTTP JSON 获取失败: {type(exc).__name__}", "unknown")
        if status != expected_status:
            return HealthResult(False, f"HTTP 状态不匹配: {status}")
        if len(body) > limit:
            return HealthResult(False, f"HTTP JSON 响应超过 {limit} bytes", "unknown")
        return body

    @staticmethod
    def _json_path(payload: Any, path: str) -> tuple[bool, Any]:
        current = payload
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                return False, None
        return True, current

    def _http(self, check: LoopbackHttpHealthCheck) -> HealthResult:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        request = UrlRequest(check.url, method="GET", headers={"Accept": "application/json"})
        try:
            with opener.open(request, timeout=check.timeout_seconds) as response:
                status = int(response.status)
                body = response.read(65537)
        except HTTPError as exc:
            status = int(exc.code)
            body = exc.read(65537)
        except Exception as exc:
            return HealthResult(False, f"loopback HTTP 失败: {type(exc).__name__}")
        if status != check.expected_status:
            return HealthResult(False, f"HTTP 状态不匹配: {status}")
        if not check.json_equals:
            return HealthResult(True, f"HTTP {status}")
        if len(body) > 65536:
            return HealthResult(False, "HTTP JSON 响应超过 64 KiB")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            return HealthResult(False, "HTTP 响应不是有效 UTF-8 JSON")
        for path, expected in check.json_equals.items():
            found, actual = self._json_path(payload, path)
            if not found or type(actual) is not type(expected) or actual != expected:
                return HealthResult(False, f"JSON 字段 {path} 不匹配")
        return HealthResult(True, f"HTTP {status} 且模型字段匹配")

    def _gpu_process(self, check: NvidiaGpuProcessHealthCheck) -> HealthResult:
        result = self.runner([
            "nvidia-smi", "--query-compute-apps=gpu_uuid,process_name",
            "--format=csv,noheader,nounits",
        ], check.timeout_seconds)
        if result.returncode != 0:
            return HealthResult(False, f"nvidia-smi 返回 {result.returncode}")
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 1)]
            if len(parts) != 2:
                continue
            process_name = parts[1].replace("\\", "/").rsplit("/", 1)[-1]
            if parts[0] == check.gpu_uuid and process_name == check.process_name:
                return HealthResult(True, "GPU UUID 与进程名匹配")
        return HealthResult(False, "指定 GPU UUID 上未发现登记进程")

    def _wsl_systemd_gpu_binding(
            self, check: WslSystemdGpuBindingHealthCheck) -> HealthResult:
        gpu = self.runner([
            "nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader,nounits",
        ], check.timeout_seconds)
        if gpu.returncode != 0:
            return HealthResult(False, f"GPU 映射检查返回 {gpu.returncode}", "unknown")
        mapped = False
        for line in gpu.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 1)]
            if len(parts) != 2:
                continue
            try:
                index = int(parts[0])
            except ValueError:
                continue
            if index == check.host_gpu_index and parts[1] == check.gpu_uuid:
                mapped = True
                break
        if not mapped:
            return HealthResult(False, "host GPU index 与 UUID 映射不匹配")
        unit = self.runner([
            "wsl.exe", "-d", check.distro, "--", "systemctl", "--user", "show",
            check.service, "--property=Environment", "--value",
        ], check.timeout_seconds)
        if unit.returncode != 0:
            return HealthResult(False, f"WSL unit 环境检查返回 {unit.returncode}", "unknown")
        try:
            environment = shlex.split(unit.stdout, posix=True)
        except ValueError:
            return HealthResult(False, "WSL unit Environment 输出无法解析", "unknown")
        expected = f"CUDA_VISIBLE_DEVICES={check.cuda_visible_device}"
        cuda_assignments = [item for item in environment
                            if item.startswith("CUDA_VISIBLE_DEVICES=")]
        if cuda_assignments != [expected]:
            return HealthResult(False, f"WSL unit 未唯一固定 {expected}")
        return HealthResult(True, "WSL user unit 与指定 GPU UUID 绑定一致")

    def _wsl_docker_compose_gpu_binding(
            self, check: WslDockerComposeGpuBindingHealthCheck) -> HealthResult:
        gpu = self.runner([
            "nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader,nounits",
        ], check.timeout_seconds)
        if gpu.returncode != 0:
            return HealthResult(False, f"GPU 映射检查返回 {gpu.returncode}", "unknown")
        mappings = {}
        for line in gpu.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 1)]
            if len(parts) != 2:
                continue
            try:
                mappings[int(parts[0])] = parts[1]
            except ValueError:
                continue
        if mappings.get(check.host_gpu_index) != check.gpu_uuid:
            return HealthResult(False, "host GPU index 与 UUID 映射不匹配")

        compose = ["wsl.exe", "-d", check.distro, "--", "docker", "compose"]
        if check.project:
            compose += ["--project-name", check.project]
        compose += ["--project-directory", check.project_dir]
        found = self.runner([*compose, "ps", "-q", check.service], check.timeout_seconds)
        ids = [line.strip() for line in found.stdout.splitlines() if line.strip()]
        if found.returncode != 0:
            return HealthResult(False, f"Compose 容器查询返回 {found.returncode}", "unknown")
        if len(ids) != 1 or re.fullmatch(r"[a-f0-9]{12,64}", ids[0]) is None:
            return HealthResult(False, "Compose 未唯一返回合法容器 ID", "unknown")
        container_id = ids[0]
        docker = ["wsl.exe", "-d", check.distro, "--", "docker"]

        devices = self.runner([
            *docker, "inspect", "--format", "{{json .HostConfig.DeviceRequests}}", container_id,
        ], check.timeout_seconds)
        if devices.returncode != 0:
            return HealthResult(False, f"Docker DeviceRequests 检查返回 {devices.returncode}",
                                "unknown")
        try:
            requests = json.loads(devices.stdout)
        except ValueError:
            return HealthResult(False, "Docker DeviceRequests 不是有效 JSON", "unknown")
        capabilities = requests[0].get("Capabilities") if (
            isinstance(requests, list) and len(requests) == 1
            and isinstance(requests[0], dict)) else None
        valid_request = (isinstance(requests, list) and len(requests) == 1
                         and isinstance(requests[0], dict)
                         and requests[0].get("Driver") == "nvidia"
                         and requests[0].get("Count") == 0
                         and requests[0].get("DeviceIDs") == [check.gpu_uuid]
                         and isinstance(capabilities, list)
                         and capabilities == [["gpu"]])
        if not valid_request:
            return HealthResult(False, "Docker DeviceRequests 未唯一固定目标 GPU UUID")

        environment = self.runner([
            *docker, "inspect", "--format", "{{json .Config.Env}}", container_id,
        ], check.timeout_seconds)
        if environment.returncode != 0:
            return HealthResult(False, f"Docker 环境检查返回 {environment.returncode}", "unknown")
        try:
            variables = json.loads(environment.stdout)
        except ValueError:
            return HealthResult(False, "Docker Env 不是有效 JSON", "unknown")
        expected = {
            "NVIDIA_VISIBLE_DEVICES": check.gpu_uuid,
            "CUDA_VISIBLE_DEVICES": check.cuda_visible_device,
        }
        assignments: dict[str, list[str]] = {key: [] for key in expected}
        if isinstance(variables, list):
            for item in variables:
                if not isinstance(item, str) or "=" not in item:
                    continue
                key, value = item.split("=", 1)
                if key in assignments:
                    assignments[key].append(value)
        if any(assignments[key] != [value] for key, value in expected.items()):
            return HealthResult(False, "容器环境未唯一固定 NVIDIA/CUDA GPU UUID")

        visible = self.runner([
            *docker, "exec", container_id, "/usr/local/bin/cuda-visible-probe",
        ], check.timeout_seconds)
        visible_lines = [line.strip() for line in visible.stdout.splitlines() if line.strip()]
        if visible.returncode != 0:
            return HealthResult(False, f"容器内 CUDA 可见性检查返回 {visible.returncode}", "unknown")
        if visible_lines != ["count=1", check.gpu_uuid]:
            return HealthResult(False, "CUDA Driver 没有且仅有指定 GPU UUID")
        return HealthResult(True, "Compose、容器环境与 CUDA Driver 均唯一绑定目标 UUID")

    @staticmethod
    def _object_has_keys(check: HttpJsonObjectHasKeysCheck, body: bytes) -> HealthResult:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            return HealthResult(False, "voices 响应不是有效 UTF-8 JSON", "unknown")
        if not isinstance(payload, dict):
            return HealthResult(False, "voices 响应顶层必须是 object")
        missing = [key for key in check.required_keys if key not in payload or payload[key] is None]
        if missing:
            return HealthResult(False, f"voices 缺少必需 key: {', '.join(missing)}")
        return HealthResult(True, "voices object 包含全部必需 key")

    def _comfy_capability(self, check: WindowsComfyCapabilityHealthCheck) -> HealthResult:
        gpu = self.runner(["nvidia-smi", "--query-gpu=index,uuid,name",
                           "--format=csv,noheader,nounits"], check.timeout_seconds)
        if gpu.returncode != 0:
            return HealthResult(False, f"Comfy GPU 交叉检查命令返回 {gpu.returncode}", "unknown")
        matches = []
        for line in gpu.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 2)]
            if len(parts) == 3:
                try:
                    index = int(parts[0])
                except ValueError:
                    continue
                if (index == check.target_host_gpu_index and parts[1] == check.target_gpu_uuid
                        and parts[2] == check.target_gpu_name):
                    matches.append(parts)
        if len(matches) != 1:
            return HealthResult(False, "Comfy host GPU index/UUID/name 未唯一精确匹配")
        payloads: dict[str, Any] = {}
        for stage, url, limit in (
            ("system_stats", check.system_stats_url, check.max_system_stats_bytes),
            ("queue", check.queue_url, check.max_queue_bytes),
            ("object_info", check.object_info_url, check.max_object_info_bytes),
        ):
            fetched = self._fetch_json_bytes(url, limit, check.timeout_seconds)
            if isinstance(fetched, HealthResult):
                return HealthResult(False, f"Comfy {stage}: {fetched.message}", fetched.status)
            try:
                payloads[stage] = json.loads(fetched.decode("utf-8"))
            except (UnicodeError, ValueError):
                return HealthResult(False, f"Comfy {stage} 不是有效 UTF-8 JSON", "unknown")
        stats = payloads["system_stats"]
        devices = stats.get("devices") if isinstance(stats, dict) else None
        if not isinstance(devices, list) or not devices or not isinstance(devices[0], dict):
            return HealthResult(False, "Comfy system_stats.devices[0] 结构无效", "unknown")
        device = devices[0]
        if (device.get("type") != "cuda" or type(device.get("index")) is not int
                or device.get("index") != check.expected_comfy_device_index):
            return HealthResult(False, "Comfy 内部 cuda device index 不匹配")
        name = device.get("name")
        if not isinstance(name, str) or check.target_gpu_name not in name:
            return HealthResult(False, "Comfy 内部 GPU 型号不匹配")
        queue = payloads["queue"]
        if (not isinstance(queue, dict) or not isinstance(queue.get("queue_running"), list)
                or not isinstance(queue.get("queue_pending"), list)):
            return HealthResult(False, "Comfy queue 两个数组结构无效", "unknown")
        object_info = payloads["object_info"]
        if not isinstance(object_info, dict):
            return HealthResult(False, "Comfy object_info 顶层必须是 object", "unknown")
        missing = [node for node in check.required_node_classes if node not in object_info]
        if missing:
            return HealthResult(False, f"Comfy 缺少必需节点: {', '.join(missing)}")
        return HealthResult(True, "Comfy GPU、队列结构与节点能力严格匹配")


class DefaultSafetyProbe:
    """仅实现有限类型、固定参数的安全预检；不接受命令或任意主机。"""

    def __init__(self, runner: Runner,
                 windows_path_probe: Callable[[str], tuple[bool, int, str | None]] | None = None) -> None:
        self.runner = runner
        self.windows_path_probe = windows_path_probe or self._default_windows_path_probe

    def check(self, check: PreflightCheck) -> HealthResult:
        if isinstance(check, DrainHttpJsonCheck):
            return self._drain(check)
        if isinstance(check, DrainHttpPrometheusCheck):
            body = self._fetch_drain_body(check.url, check.timeout_seconds, check.max_body_bytes)
            return body if isinstance(body, HealthResult) else self._drain_prometheus(check, body)
        if isinstance(check, DrainHttpJsonArraysCheck):
            body = self._fetch_drain_body(check.url, check.timeout_seconds, check.max_body_bytes)
            return body if isinstance(body, HealthResult) else self._drain_json_arrays(check, body)
        if isinstance(check, NvidiaGpuMemoryCheck):
            query = ("index,uuid,memory.free" if check.host_gpu_index is not None
                     else "uuid,memory.free")
            result = self.runner([
                "nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits",
            ], check.timeout_seconds)
            if result.returncode != 0:
                return HealthResult(False, f"nvidia-smi 返回 {result.returncode}")
            for line in result.stdout.splitlines():
                parts = [part.strip() for part in line.split(",")]
                if check.host_gpu_index is None:
                    if len(parts) != 2 or parts[0] != check.gpu_uuid:
                        continue
                    free_value = parts[1]
                else:
                    if len(parts) != 3 or parts[1] != check.gpu_uuid:
                        continue
                    try:
                        actual_index = int(parts[0])
                    except ValueError:
                        return HealthResult(False, "GPU host index 不是整数", "unknown")
                    if actual_index != check.host_gpu_index:
                        return HealthResult(
                            False,
                            f"目标 GPU UUID 当前位于 host index {actual_index}，要求 {check.host_gpu_index}",
                        )
                    free_value = parts[2]
                if parts:
                    try:
                        free_mib = int(free_value)
                    except ValueError:
                        return HealthResult(False, "GPU free MiB 不是整数")
                    return HealthResult(
                        free_mib >= check.min_free_mib,
                        f"指定 GPU 空闲 {free_mib} MiB，要求至少 {check.min_free_mib} MiB",
                    )
            return HealthResult(False, "未找到指定 GPU UUID")
        if isinstance(check, WslPathDiskCheck):
            exists = self.runner([
                "wsl.exe", "-d", check.distro, "--", "test", "-e", check.path,
            ], check.timeout_seconds)
            if exists.returncode != 0:
                return HealthResult(False, f"WSL {check.purpose} 路径不存在")
            disk = self.runner([
                "wsl.exe", "-d", check.distro, "--", "df", "--output=avail", "-k", check.path,
            ], check.timeout_seconds)
            if disk.returncode != 0:
                return HealthResult(False, "无法取得 WSL 路径磁盘余量")
            numbers = [line.strip() for line in disk.stdout.splitlines()
                       if line.strip().isdigit()]
            if not numbers:
                return HealthResult(False, "WSL 磁盘余量输出无效")
            free_mib = int(numbers[-1]) // 1024
            return HealthResult(
                free_mib >= check.min_free_mib,
                f"WSL 路径空闲 {free_mib} MiB，要求至少 {check.min_free_mib} MiB",
            )
        if isinstance(check, WindowsPathDiskCheck):
            outcome = self._run_windows_path_probe(check)
            if isinstance(outcome, HealthResult):
                return outcome
            exists, free_bytes, error = outcome
            if error:
                return HealthResult(False, f"Windows 路径/卷无法可信检查: {error}", "unknown")
            if not exists:
                return HealthResult(False, f"Windows {check.purpose} 路径不存在")
            required = check.min_free_gib * 1024 ** 3
            free_gib = free_bytes / 1024 ** 3
            return HealthResult(free_bytes >= required,
                                f"Windows 路径所在卷空闲 {free_gib:.1f} GiB，要求至少 {check.min_free_gib} GiB")
        if isinstance(check, LoopbackPortAvailableCheck):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(check.timeout_seconds)
            try:
                occupied = probe.connect_ex(("127.0.0.1", check.port)) == 0
            finally:
                probe.close()
            return HealthResult(not occupied,
                                "目标 loopback 端口可用" if not occupied else "目标 loopback 端口已占用")
        if isinstance(check, WslPortAvailableCheck):
            result = self.runner([
                "wsl.exe", "-d", check.distro, "--", "ss", "-ltnH",
            ], check.timeout_seconds)
            if result.returncode != 0:
                return HealthResult(False, f"WSL 端口检查返回 {result.returncode}", "unknown")
            occupied = False
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) < 4:
                    continue
                local = fields[3]
                if ":" not in local:
                    continue
                try:
                    port = int(local.rsplit(":", 1)[1])
                except ValueError:
                    continue
                if port == check.port:
                    occupied = True
                    break
            return HealthResult(not occupied,
                                "目标 WSL 端口可用" if not occupied else "目标 WSL 端口已占用")
        if isinstance(check, H3VideoProfileCheck):
            return HealthResult(True, "H3 profile 为 8 steps / shift 12,3 / 8step LoRA")
        return HealthResult(False, "required_dependency 必须由控制面核验")

    def _run_windows_path_probe(
            self, check: WindowsPathDiskCheck) -> tuple[bool, int, str | None] | HealthResult:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_windows_probe_worker,
            args=(self.windows_path_probe, check.path, child),
            daemon=True,
        )
        started = False
        try:
            process.start()
            started = True
            child.close()
            if not parent.poll(check.timeout_seconds):
                return HealthResult(False, "Windows 路径探针超时", "unknown")
            kind, payload = parent.recv()
            if kind != "ok":
                return HealthResult(False, f"Windows 路径探针失败: {payload}", "unknown")
            return payload
        except Exception as exc:
            return HealthResult(False, f"Windows 路径探针失败: {type(exc).__name__}", "unknown")
        finally:
            for connection in (parent, child):
                try:
                    connection.close()
                except (OSError, ValueError):
                    pass
            self._cleanup_windows_path_probe_process(process, started)

    @staticmethod
    def _cleanup_windows_path_probe_process(process: Any, started: bool) -> None:
        """Best-effort cleanup that must not replace the probe's original result."""
        if not started:
            return
        try:
            if process.is_alive():
                process.terminate()
        except (AssertionError, OSError, ValueError):
            pass
        try:
            process.join(1)
        except (AssertionError, OSError, ValueError):
            pass
        try:
            if process.is_alive():
                process.kill()
        except (AssertionError, OSError, ValueError):
            pass
        try:
            process.join(1)
        except (AssertionError, OSError, ValueError):
            pass
        try:
            if not process.is_alive():
                process.close()
        except (AssertionError, OSError, ValueError):
            pass

    @staticmethod
    def _default_windows_path_probe(path: str) -> tuple[bool, int, str | None]:
        target = Path(path)
        try:
            if not target.exists():
                return False, 0, None
            reparse_error = _windows_reparse_error(path)
            if reparse_error:
                return True, 0, reparse_error
            root = ntpath.splitdrive(path)[0] + "\\"
            free = ctypes.c_ulonglong()
            total = ctypes.c_ulonglong()
            total_free = ctypes.c_ulonglong()
            kernel32 = ctypes.windll.kernel32
            ok = kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(root), ctypes.byref(free),
                ctypes.byref(total), ctypes.byref(total_free))
            if not ok:
                return True, 0, f"Win32 error {ctypes.get_last_error()}"
            return True, int(free.value), None
        except (OSError, ValueError, AttributeError) as exc:
            return False, 0, type(exc).__name__

    @staticmethod
    def _fetch_drain_body(url: str, timeout: float, limit: int) -> bytes | HealthResult:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        request = UrlRequest(url, method="GET", headers={"Accept": "application/json,text/plain"})
        try:
            with opener.open(request, timeout=timeout) as response:
                if int(response.status) != 200:
                    return HealthResult(False, f"drain HTTP 状态 {response.status}", "unknown")
                body = response.read(limit + 1)
        except Exception as exc:
            return HealthResult(False, f"drain HTTP 失败: {type(exc).__name__}", "unknown")
        if len(body) > limit:
            return HealthResult(False, f"drain 响应超过 {limit} bytes", "unknown")
        return body

    @staticmethod
    def _parse_prometheus_labels(raw: str) -> dict[str, str] | None:
        labels: dict[str, str] = {}
        position = 0
        pattern = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*")(?:,|$)')
        while position < len(raw):
            match = pattern.match(raw, position)
            if match is None or match.group(1) in labels:
                return None
            try:
                value = json.loads(match.group(2))
            except ValueError:
                return None
            labels[match.group(1)] = value
            position = match.end()
        return labels

    @classmethod
    def _drain_prometheus(cls, check: DrainHttpPrometheusCheck, body: bytes) -> HealthResult:
        try:
            text = body.decode("utf-8")
        except UnicodeError:
            return HealthResult(False, "Prometheus drain 不是有效 UTF-8", "unknown")
        samples: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        line_pattern = re.compile(
            r'^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{([^}]*)\})?\s+([^\s]+)(?:\s+\d+)?$')
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = line_pattern.fullmatch(line)
            if match is None:
                return HealthResult(False, "Prometheus exposition 行格式无效", "unknown")
            labels = cls._parse_prometheus_labels(match.group(2) or "")
            if labels is None:
                return HealthResult(False, "Prometheus labels 解析失败", "unknown")
            try:
                value = float(match.group(3))
            except ValueError:
                return HealthResult(False, "Prometheus 值不是数值", "unknown")
            if not math.isfinite(value) or value < 0:
                return HealthResult(False, "Prometheus 值必须是有限非负数", "unknown")
            identity = (match.group(1), tuple(sorted(labels.items())))
            if identity in samples:
                return HealthResult(False, "Prometheus 出现重复完全相同 series", "unknown")
            samples[identity] = value
        for series in check.series:
            identity = (series.metric, tuple(sorted(series.labels.items())))
            if identity not in samples:
                return HealthResult(False, f"Prometheus 缺少 series {series.metric}", "unknown")
            if samples[identity] != 0:
                return HealthResult(False, f"Prometheus {series.metric} 尚为 {samples[identity]}")
        return HealthResult(True, "Prometheus 登记 series 均为 0")

    @staticmethod
    def _drain_json_arrays(check: DrainHttpJsonArraysCheck, body: bytes) -> HealthResult:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            return HealthResult(False, "Comfy queue drain 不是有效 UTF-8 JSON", "unknown")
        if not isinstance(payload, dict):
            return HealthResult(False, "Comfy queue 顶层必须是 object", "unknown")
        running = payload.get(check.running_path)
        pending = payload.get(check.pending_path)
        if not isinstance(running, list) or not isinstance(pending, list):
            return HealthResult(False, "Comfy queue_running/queue_pending 必须都是 array", "unknown")
        if running or pending:
            return HealthResult(False, f"Comfy queue 尚有 running={len(running)}, pending={len(pending)}")
        return HealthResult(True, "Comfy queue 两个数组均为空")

    @staticmethod
    def _drain(check: DrainHttpJsonCheck) -> HealthResult:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        request = UrlRequest(check.url, method="GET", headers={"Accept": "application/json"})
        try:
            with opener.open(request, timeout=check.timeout_seconds) as response:
                if int(response.status) != 200:
                    return HealthResult(False, f"drain HTTP 状态 {response.status}")
                body = response.read(65537)
        except Exception as exc:
            return HealthResult(False, f"drain HTTP 失败: {type(exc).__name__}")
        if len(body) > 65536:
            return HealthResult(False, "drain JSON 响应超过 64 KiB")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            return HealthResult(False, "drain 响应不是有效 UTF-8 JSON")
        for path in check.json_paths:
            found, value = DefaultHealthProbe._json_path(payload, path)
            if not found or isinstance(value, bool) or not isinstance(value, (int, float)):
                return HealthResult(False, f"drain 字段 {path} 必须是数值")
            if value != 0:
                return HealthResult(False, f"drain 字段 {path} 尚为 {value}")
        return HealthResult(True, "drain 数值字段均为 0")


class ControlPlane:
    def __init__(self, config: ControlConfig, database: Database, runner: Runner | None = None,
                 health_probe: HealthProbe | None = None,
                 safety_probe: SafetyProbe | None = None,
                 process_api: WindowsProcessApi | None = None) -> None:
        self.config = config
        self.database = database
        self.runner = runner or SubprocessRunner()
        self.health_probe = health_probe or DefaultHealthProbe(self.runner)
        self.safety_probe = safety_probe or DefaultSafetyProbe(self.runner)
        self.process_api = process_api or DefaultWindowsProcessApi()
        self.environments = {item.id: item for item in config.environments}
        self.scenes = {item.id: item for item in config.scenes}
        self._active_task: asyncio.Task[None] | None = None
        self._operation_lock: OperationFileLock | None = None
        self._poisoned_reason: str | None = None
        self._lease_owner_id = uuid.uuid4().hex
        recovery_lock = OperationFileLock(database.path.with_suffix(database.path.suffix + ".control.lock"))
        if recovery_lock.acquire():
            try:
                self.database.interrupt_incomplete_operations()
                self.database.clear_stale_control_lease()
            finally:
                recovery_lock.release()

    def _acquire_operation_lock(self) -> None:
        lock = OperationFileLock(
            self.database.path.with_suffix(self.database.path.suffix + ".control.lock")
        )
        if not lock.acquire():
            raise ControlError(409, "operation_in_progress", "另一管理器进程正在执行控制操作")
        try:
            acquired = self.database.acquire_control_lease(self._lease_owner_id)
        except Exception:
            lock.release()
            raise
        if not acquired:
            lock.release()
            raise ControlError(409, "operation_in_progress", "SQLite 控制租约已被占用")
        self._operation_lock = lock

    def _release_operation_lock(self) -> None:
        lock = self._operation_lock
        if lock is not None:
            self.database.release_control_lease(self._lease_owner_id)
            lock.release()
            self._operation_lock = None

    def _operation_in_progress(self) -> bool:
        if self._active_task is not None and not self._active_task.done():
            return True
        owner = self.database.control_lease_owner()
        return owner is not None and owner != self._lease_owner_id

    def _operation_safely_finalized(self, operation_id: str) -> bool:
        try:
            item = self.database.get_operation(operation_id)
            if item is None or item["status"] not in {"succeeded", "failed", "interrupted"}:
                return False
            if item["result"] in {"recovery_required", "rollback_failed"}:
                recovery = self.recovery_lock()
                return (recovery is not None and recovery["operation_id"] == operation_id
                        and bool(recovery.get("items")))
            return True
        except DatabaseError:
            return False

    async def _run_with_operation_lock(self, operation_id: str, operation: Any) -> None:
        cancelled = False
        try:
            await operation
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exc:
            self._poisoned_reason = self._error_summary(exc)
        if self._operation_safely_finalized(operation_id):
            try:
                self._release_operation_lock()
            except Exception as exc:
                self._poisoned_reason = self._error_summary(exc)
        else:
            self._poisoned_reason = self._poisoned_reason or "操作终态或恢复锁未可靠持久化"
        if cancelled:
            raise asyncio.CancelledError

    def _environment(self, environment_id: str) -> EnvironmentConfig:
        item = self.environments.get(environment_id)
        if item is None:
            raise ControlError(404, "environment_not_found", "环境不存在")
        return item

    def _scene(self, scene_id: str) -> SceneConfig:
        item = self.scenes.get(scene_id)
        if item is None:
            raise ControlError(404, "scene_not_found", "场景不存在")
        return item

    def _adapter(self, item: EnvironmentConfig) -> Adapter:
        if not item.configured or item.adapter is None:
            raise ControlError(409, "environment_not_configured", "环境尚未配置", item.missing_reason)
        return make_adapter(item.adapter, self.runner, self.process_api)

    async def status(self, item: EnvironmentConfig) -> str:
        # 只读状态发现与写控制授权必须分离：已经登记固定适配器但尚未通过
        # 安全验收的环境仍应显示真实状态；configured/allowed_actions 继续阻断写动作。
        if item.adapter is None:
            return "unconfigured"
        try:
            adapter = make_adapter(item.adapter, self.runner, self.process_api)
            return await self._adapter_call(adapter.status)
        except ControlError:
            return "unknown"

    def configuration_blockers(self, item: EnvironmentConfig,
                               action: str | None = None) -> list[str]:
        """返回按动作计算的静态配置缺口。"""
        result: list[str] = []
        if not item.configured:
            result.append(item.missing_reason or "缺少已核对的生命周期入口")
        if item.adapter is None:
            result.append("缺少适配器配置")
        preflight_types = {check.type for check in item.preflight_checks}
        is_ai = item.gpu_ai or item.safety_profile in {"gpu_ai", "h3_video"}
        if action in {"stop", "restart"}:
            if is_ai and isinstance(item.adapter, WindowsComfyProcessConfig):
                if "drain_http_json_arrays" not in preflight_types:
                    result.append(f"ComfyUI {action} 缺少 drain_http_json_arrays 队列清空检查")
            elif is_ai and not ({"drain_http_prometheus", "drain_http_json"} & preflight_types):
                result.append(f"可控 AI 环境 {action} 缺少 active_requests drain 检查")
            if action == "stop":
                return list(dict.fromkeys(result))
        check_types = {check.type for check in item.health_checks}
        if "adapter_status" not in check_types:
            result.append("缺少 adapter_status 健康检查")
        if item.gpu_ai:
            if isinstance(item.adapter, WindowsComfyProcessConfig):
                capability = [check for check in item.health_checks
                              if isinstance(check, WindowsComfyCapabilityHealthCheck)]
                if not capability:
                    result.append("Windows ComfyUI 缺少严格 GPU/队列/节点能力健康检查")
                elif not any(check.target_gpu_uuid == item.adapter.target_gpu_uuid
                             and check.target_host_gpu_index == item.adapter.target_host_gpu_index
                             and urlsplit(check.system_stats_url).port == item.adapter.port
                             for check in capability):
                    result.append("Windows ComfyUI adapter 与 capability GPU/端口不一致")
            else:
                http_checks = [check for check in item.health_checks
                               if isinstance(check, LoopbackHttpHealthCheck)]
                if not http_checks:
                    result.append("GPU AI 环境缺少 loopback HTTP endpoint 健康检查")
                elif not any(check.json_equals for check in http_checks):
                    result.append("GPU AI 环境缺少 HTTP JSON 模型字段精确匹配")
                gpu_bindings = [check for check in item.health_checks
                                if isinstance(check, WslSystemdGpuBindingHealthCheck)]
                docker_bindings = [check for check in item.health_checks
                                   if isinstance(check, WslDockerComposeGpuBindingHealthCheck)]
                if not ({"nvidia_gpu_process", "wsl_systemd_gpu_binding",
                         "wsl_docker_compose_gpu_binding"} & check_types):
                    result.append("GPU AI 环境缺少 GPU UUID 进程或 WSL unit 绑定健康检查")
                if gpu_bindings:
                    if not isinstance(item.adapter, WslSystemdConfig):
                        result.append("WSL GPU unit 绑定检查只能用于 wsl_systemd 适配器")
                    elif not any(check.distro == item.adapter.distro
                                 and check.service == item.adapter.service
                                 for check in gpu_bindings):
                        result.append("WSL GPU unit 绑定检查与 adapter 不一致")
                    wsl_ports = [check for check in item.preflight_checks
                                 if isinstance(check, WslPortAvailableCheck)]
                    http_ports = {urlsplit(check.url).port for check in http_checks}
                    if not isinstance(item.adapter, WslSystemdConfig) or not any(
                            check.distro == item.adapter.distro
                            and check.owner_environment_id == item.id
                            and check.port in http_ports for check in wsl_ports):
                        result.append("WSL 端口检查与当前 adapter、环境或健康端口不一致")
                if docker_bindings:
                    if not isinstance(item.adapter, WslDockerComposeConfig):
                        result.append("WSL Docker GPU 绑定检查只能用于 wsl_docker_compose 适配器")
                    elif not any(check.distro == item.adapter.distro
                                 and check.project_dir == item.adapter.project_dir
                                 and check.project == item.adapter.project
                                 and check.service == item.adapter.service
                                 for check in docker_bindings):
                        result.append("WSL Docker GPU 绑定检查与 adapter 不一致")
                if isinstance(item.adapter, WslDockerComposeConfig) and not docker_bindings:
                    result.append("WSL Docker GPU 环境缺少 Compose/CUDA Driver 绑定检查")
                memory_checks = [check for check in item.preflight_checks
                                 if isinstance(check, NvidiaGpuMemoryCheck)]
                if docker_bindings and memory_checks and not any(
                        memory.gpu_uuid == binding.gpu_uuid
                        for memory in memory_checks for binding in docker_bindings):
                    result.append("WSL Docker 显存预算与 GPU 绑定 UUID 不一致")
                if isinstance(item.adapter, WslDockerComposeConfig):
                    wsl_paths = [check for check in item.preflight_checks
                                 if isinstance(check, WslPathDiskCheck)]
                    if wsl_paths and any(check.distro != item.adapter.distro
                                         for check in wsl_paths):
                        result.append("WSL Docker 模型路径检查与 adapter 发行版不一致")
                    ports = [check for check in item.preflight_checks
                             if isinstance(check, LoopbackPortAvailableCheck)]
                    http_ports = {urlsplit(check.url).port for check in http_checks}
                    if ports and not any(check.owner_environment_id == item.id
                                         and check.port in http_ports for check in ports):
                        result.append("WSL Docker 端口检查与当前环境或健康端口不一致")
        if is_ai:
            if "nvidia_gpu_memory" not in preflight_types:
                result.append("GPU AI 环境缺少指定 GPU UUID 显存预算检查")
            expected_path_type = WindowsPathDiskCheck if isinstance(
                item.adapter, WindowsComfyProcessConfig) else WslPathDiskCheck
            path_checks = [check for check in item.preflight_checks
                           if isinstance(check, expected_path_type)]
            if not any(check.purpose == "model" for check in path_checks):
                result.append("GPU AI 环境缺少模型路径与磁盘余量检查")
            if not ({"loopback_port_available", "wsl_port_available"} & preflight_types):
                result.append("GPU AI 环境缺少目标端口冲突检查")
        if item.safety_profile == "h3_video":
            if not any(isinstance(check, (WslPathDiskCheck, WindowsPathDiskCheck))
                       and check.purpose == "lora"
                       for check in item.preflight_checks):
                result.append("H3 视频环境缺少 LoRA 路径与磁盘余量检查")
            if "h3_video_profile" not in preflight_types:
                result.append("H3 视频环境缺少固定 8-step profile 检查")
            if "required_dependency" not in preflight_types:
                result.append("H3 视频环境缺少依赖环境健康检查")
        return list(dict.fromkeys(result))

    def blockers(self, item: EnvironmentConfig) -> list[str]:
        result: list[str] = []
        if not self.config.control_enabled:
            result.append("控制功能未启用")
        result.extend(self.configuration_blockers(item))
        if not item.allowed_actions:
            result.append("未允许任何写动作")
        return list(dict.fromkeys(result))

    def rollback_blockers(self, item: EnvironmentConfig, action: str) -> list[str]:
        """回滚同样 fail-closed；AI stop 绝不绕过对应强类型 drain。"""
        return self.configuration_blockers(item, action)

    def recovery_lock(self) -> dict[str, Any] | None:
        return self.database.control_recovery_lock()

    async def _action_capabilities(self, item: EnvironmentConfig, state: str,
                                   health: dict[str, Any] | None = None,
                                   include_operation_lock: bool = True) -> dict[str, Any]:
        recovery = self.recovery_lock()
        operation_in_progress = include_operation_lock and self._operation_in_progress()
        result: dict[str, Any] = {}
        for action in ("start", "stop", "restart"):
            blockers: list[str] = []
            if not self.config.control_enabled:
                blockers.append("控制功能未启用")
            if self._poisoned_reason is not None:
                blockers.append("控制面最终化失败，必须重启并人工恢复")
            if recovery is not None:
                blockers.append(f"控制恢复锁未解除：{recovery['environment_id']} 需人工处理")
            if operation_in_progress:
                blockers.append("已有控制操作正在执行")
            if action not in item.allowed_actions:
                blockers.append(f"未允许 {action} 动作")
            blockers.extend(self.configuration_blockers(item, action))
            if action != "stop" and state in {"unknown", "unconfigured"}:
                blockers.append("无法确认环境当前状态")
            if action in {"start", "restart"} and health is not None and health["healthy"] is False:
                blockers.extend(check["message"] for check in health["checks"] if not check["healthy"])
            result[action] = {
                "allowed": action in item.allowed_actions,
                "ready": not blockers,
                "blockers": list(dict.fromkeys(blockers)),
                "confirmation": f"{action}:{item.id}" if action in item.allowed_actions else None,
            }
        return result

    async def check_health(self, item: EnvironmentConfig, lifecycle_state: str) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for check in item.health_checks:
            if isinstance(check, HealthCheckConfig):
                result = HealthResult(lifecycle_state == "running",
                                      f"适配器状态 {lifecycle_state}")
            else:
                result = await self._adapter_call(lambda check=check: self.health_probe.check(check))
            results.append({"type": check.type, "healthy": result.healthy,
                            "status": result.status,
                            "message": redact_sensitive_text(result.message)[0]})
        return {"healthy": bool(results) and all(item["healthy"] for item in results),
                "checks": results}

    async def wait_for_startup_health(self, item: EnvironmentConfig,
                                      lifecycle_state: str) -> dict[str, Any]:
        deadline = time.monotonic() + item.startup_health_timeout_seconds
        while True:
            health = await self.check_health(item, lifecycle_state)
            if health["healthy"] or item.startup_health_timeout_seconds == 0:
                return health
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return health
            try:
                await asyncio.sleep(min(item.startup_health_poll_interval_seconds, remaining))
            except asyncio.CancelledError:
                # adapter 已可能把 Type=simple unit 启动为 running；关闭管理器时不能
                # 把 readiness 等待伪装成已取消，否则会跳过既有协调/回滚/恢复锁。
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()

    async def check_safety(self, item: EnvironmentConfig, check: PreflightCheck,
                           statuses: dict[str, str] | None = None) -> dict[str, Any]:
        if isinstance(check, RequiredDependencyCheck):
            dependency = self.environments.get(check.environment_id)
            if dependency is None:
                result = HealthResult(False, "依赖环境不存在")
            else:
                state = (statuses or {}).get(dependency.id)
                if state is None:
                    state = await self.status(dependency)
                if state != "running":
                    result = HealthResult(False, f"依赖环境 {dependency.id} 状态为 {state}")
                else:
                    health = await self.check_health(dependency, state)
                    result = HealthResult(health["healthy"],
                                          f"依赖环境 {dependency.id} 严格健康验证"
                                          if health["healthy"] else
                                          f"依赖环境 {dependency.id} 健康验证失败")
        elif isinstance(check, (LoopbackPortAvailableCheck, WslPortAvailableCheck)):
            owner_state = (statuses or {}).get(check.owner_environment_id)
            if owner_state == "running" and check.owner_environment_id == item.id:
                result = HealthResult(True, "端口由当前已运行环境占用")
            else:
                result = await self._adapter_call(lambda: self.safety_probe.check(check))
        else:
            result = await self._adapter_call(lambda: self.safety_probe.check(check))
        return {"type": check.type, "healthy": result.healthy,
                "status": result.status,
                "message": redact_sensitive_text(result.message)[0]}

    async def safety_preflight(self, item: EnvironmentConfig, action: str,
                               statuses: dict[str, str] | None = None) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for check in item.preflight_checks:
            if action == "stop" and not isinstance(
                    check, (DrainHttpJsonCheck, DrainHttpPrometheusCheck, DrainHttpJsonArraysCheck)):
                continue
            if action == "start" and isinstance(
                    check, (DrainHttpJsonCheck, DrainHttpPrometheusCheck, DrainHttpJsonArraysCheck)):
                continue
            checks.append(await self.check_safety(item, check, statuses))
        return checks

    async def wait_for_drain(self, item: EnvironmentConfig) -> list[dict[str, Any]]:
        drain_checks = [check for check in item.preflight_checks
                        if isinstance(check, (DrainHttpJsonCheck, DrainHttpPrometheusCheck,
                                              DrainHttpJsonArraysCheck))]
        results: list[dict[str, Any]] = []
        for check in drain_checks:
            deadline = time.monotonic() + check.wait_timeout_seconds
            while True:
                result = await self.check_safety(item, check)
                if result["healthy"]:
                    results.append(result)
                    break
                if time.monotonic() >= deadline:
                    raise ControlError(409, "drain_timeout",
                                       f"{item.name} 等待活动请求清零超时", result)
                await asyncio.sleep(check.poll_interval_seconds)
        return results

    @staticmethod
    async def _adapter_call(function: Callable[[], Any]) -> Any:
        """适配器子进程不可被 asyncio 伪取消；必须等固定超时或完成后再收尾。"""
        worker = asyncio.create_task(asyncio.to_thread(function))
        while True:
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()

    async def list_environments(self) -> dict[str, Any]:
        items = []
        for env in self.environments.values():
            state = await self.status(env)
            blockers = self.blockers(env)
            health = await self.check_health(env, state) if env.configured and state == "running" else {
                "healthy": None, "checks": []
            }
            if health["healthy"] is False:
                blockers.extend(check["message"] for check in health["checks"] if not check["healthy"])
            operation_in_progress = self._operation_in_progress()
            if operation_in_progress: blockers.append("已有控制操作正在执行")
            capabilities = await self._action_capabilities(env, state, health)
            items.append({
                "id": env.id, "name": env.name, "configured": env.configured,
                "adapter_configured": env.adapter is not None,
                "adapter_type": env.adapter.type if env.adapter is not None else None,
                "status": state, "ready": any(item["ready"] for item in capabilities.values()),
                "allowed_actions": list(env.allowed_actions), "blockers": blockers,
                "operation_in_progress": operation_in_progress,
                "action_capabilities": capabilities,
                "health": health,
            })
        return {"control_enabled": self.config.control_enabled, "source": self.config.source,
                "recovery_required": self.recovery_lock(),
                "process_poisoned": self._poisoned_reason is not None,
                "environments": items}

    async def environment_preflight(self, environment_id: str,
                                    action: str | None = None) -> dict[str, Any]:
        env = self._environment(environment_id)
        if action is not None and action not in {"start", "stop", "restart"}:
            raise ControlError(422, "invalid_action", "环境动作无效")
        state = await self.status(env)
        blockers = self.blockers(env)
        health = await self.check_health(env, state) if env.configured and state == "running" else {
            "healthy": None, "checks": []
        }
        if health["healthy"] is False:
            blockers.extend(check["message"] for check in health["checks"] if not check["healthy"])
        if self._operation_in_progress():
            blockers.append("已有控制操作正在执行")
        if state == "unknown": blockers.append("无法确认环境当前状态")
        capabilities = await self._action_capabilities(env, state, health)
        safety_checks: list[dict[str, Any]] = []
        if action is not None and not self.configuration_blockers(env, action):
            safety_checks = await self.safety_preflight(env, action, {env.id: state})
            capabilities[action]["blockers"].extend(
                check["message"] for check in safety_checks if not check["healthy"])
            capabilities[action]["blockers"] = list(dict.fromkeys(
                capabilities[action]["blockers"]))
            capabilities[action]["ready"] = not capabilities[action]["blockers"]
        selected_blockers = capabilities[action]["blockers"] if action is not None else blockers
        selected_ready = capabilities[action]["ready"] if action is not None else not blockers
        return {"environment_id": env.id, "status": state, "ready": selected_ready,
                "allowed_actions": list(env.allowed_actions), "blockers": selected_blockers,
                "action": action, "action_blockers": selected_blockers,
                "action_capabilities": capabilities,
                "health": health,
                "preflight_checks": safety_checks,
                "confirmation": {action: f"{action}:{env.id}" for action in env.allowed_actions}}

    async def scene_view(self, scene: SceneConfig, include_operation_lock: bool = True) -> dict[str, Any]:
        statuses = {env_id: await self.status(self.environments[env_id])
                    for env_id in (*scene.desired, *scene.conflicts)}
        health: dict[str, Any] = {}
        blockers: list[str] = []
        if not self.config.control_enabled:
            blockers.append("控制功能未启用")
        if self._poisoned_reason is not None:
            blockers.append("控制面最终化失败，必须重启并人工恢复")
        recovery = self.recovery_lock()
        if recovery is not None:
            blockers.append(f"控制恢复锁未解除：{recovery['environment_id']} 需人工处理")
        operation_in_progress = self._operation_in_progress()
        if include_operation_lock and operation_in_progress:
            blockers.append("已有控制操作正在执行")
        for env_id in scene.desired:
            env = self.environments[env_id]
            blockers.extend(f"{env.name}：{reason}" for reason in self.blockers(env)
                            if reason != "控制功能未启用")
            if statuses[env_id] == "unknown": blockers.append(f"{env.name}：状态未知")
            if statuses[env_id] == "running":
                health[env_id] = await self.check_health(env, statuses[env_id])
                if not health[env_id]["healthy"]:
                    blockers.extend(f"{env.name}：{check['message']}" for check in health[env_id]["checks"] if not check["healthy"])
                active_safety = []
                for check in env.preflight_checks:
                    if isinstance(check, (WslPathDiskCheck, WindowsPathDiskCheck,
                                          RequiredDependencyCheck,
                                          LoopbackPortAvailableCheck, WslPortAvailableCheck,
                                          H3VideoProfileCheck)):
                        active_safety.append(await self.check_safety(env, check, statuses))
                health[env_id]["preflight_checks"] = active_safety
                blockers.extend(f"{env.name}：{check['message']}" for check in active_safety
                                if not check["healthy"])
            if statuses[env_id] != "running" and "start" not in env.allowed_actions:
                blockers.append(f"{env.name}：未允许 start 动作")
            if statuses[env_id] != "running" and "stop" not in env.allowed_actions:
                blockers.append(f"{env.name}：缺少失败回滚所需的 stop 权限")
        for env_id in scene.conflicts:
            env = self.environments[env_id]
            if statuses[env_id] in {"unknown", "unconfigured"}:
                blockers.append(f"{env.name}：无法确认冲突环境状态")
            if statuses[env_id] == "failed":
                blockers.append(f"{env.name}：冲突环境状态异常")
            if statuses[env_id] == "running":
                blockers.extend(f"{env.name}：{reason}"
                                for reason in self.configuration_blockers(env, "stop"))
                if "stop" not in env.allowed_actions:
                    blockers.append(f"{env.name}：未允许 stop 动作")
                if "start" not in env.allowed_actions:
                    blockers.append(f"{env.name}：缺少失败回滚所需的 start 权限")
                blockers.extend(f"{env.name}：回滚配置：{reason}"
                                for reason in self.configuration_blockers(env, "start"))
        desired_running = all(statuses[item] == "running" for item in scene.desired)
        conflicts_stopped = all(statuses[item] != "running" for item in scene.conflicts)
        uncertain = any(value in {"unknown", "unconfigured"} for value in statuses.values())
        degraded = any(value == "failed" for value in statuses.values())
        desired_healthy = all(
            health.get(item, {}).get("healthy") is True
            and all(check["healthy"] for check in health.get(item, {}).get("preflight_checks", []))
            for item in scene.desired)
        current = "active" if desired_running and desired_healthy and conflicts_stopped and not uncertain and not degraded else (
            "partial" if any(statuses[item] == "running" for item in scene.desired)
            else "degraded" if degraded
            else "unknown" if uncertain else "inactive"
        )
        return {"id": scene.id, "name": scene.name, "desired": list(scene.desired),
                "optional_desired": list(scene.optional_desired),
                "conflicts": list(scene.conflicts), "ready": not blockers,
                "blockers": list(dict.fromkeys(blockers)), "statuses": statuses,
                "health": health,
                "current": current, "inference": "仅依据已登记适配器状态；未配置项不作确定推断"}

    async def list_scenes(self) -> dict[str, Any]:
        return {"control_enabled": self.config.control_enabled, "source": self.config.source,
                "scenes": [await self.scene_view(scene) for scene in self.scenes.values()]}

    async def scene_preflight(self, scene_id: str, include_operation_lock: bool = True) -> dict[str, Any]:
        scene = self._scene(scene_id)
        view = await self.scene_view(scene, include_operation_lock=include_operation_lock)
        plan = []
        sequence = 1
        for env_id in scene.conflicts:
            env = self.environments[env_id]
            if (view["statuses"][env_id] == "running"
                    and any(isinstance(check, (DrainHttpJsonCheck, DrainHttpPrometheusCheck,
                                                DrainHttpJsonArraysCheck))
                            for check in env.preflight_checks)):
                plan.append({"sequence": sequence, "phase": "drain", "target_id": env_id,
                             "action": "drain", "checks": [
                                 check.model_dump() for check in env.preflight_checks
                                 if isinstance(check, (DrainHttpJsonCheck, DrainHttpPrometheusCheck,
                                                       DrainHttpJsonArraysCheck))
                             ]}); sequence += 1
        for env_id in scene.conflicts:
            if view["statuses"][env_id] == "running":
                plan.append({"sequence": sequence, "phase": "stop_conflicts", "target_id": env_id, "action": "stop"}); sequence += 1
        for env_id in scene.conflicts:
            if view["statuses"][env_id] == "running":
                env = self.environments[env_id]
                for index, check in enumerate(env.preflight_checks):
                    if isinstance(check, (NvidiaGpuMemoryCheck, LoopbackPortAvailableCheck,
                                          WslPortAvailableCheck)):
                        plan.append({"sequence": sequence, "phase": "verify_release_ports",
                                     "target_id": env_id, "action": "safety_check",
                                     "check_index": index,
                                     "check": check.model_dump()}); sequence += 1
        for env_id in scene.desired:
            env = self.environments[env_id]
            for index, check in enumerate(env.preflight_checks):
                if not isinstance(check, (DrainHttpJsonCheck, DrainHttpPrometheusCheck,
                                          DrainHttpJsonArraysCheck)):
                    plan.append({"sequence": sequence, "phase": "validate_safety",
                                 "target_id": env_id, "action": "safety_check",
                                 "check_index": index,
                                 "check": check.model_dump()}); sequence += 1
        for env_id in scene.desired:
            if view["statuses"][env_id] != "running":
                plan.append({"sequence": sequence, "phase": "start_desired", "target_id": env_id, "action": "start"}); sequence += 1
        for env_id in scene.desired:
            plan.append({"sequence": sequence, "phase": "verify", "target_id": env_id, "action": "status", "expected": "running"}); sequence += 1
        for env_id in scene.conflicts:
            plan.append({"sequence": sequence, "phase": "verify_conflicts", "target_id": env_id, "action": "status", "expected": "stopped"}); sequence += 1
        return {**view, "plan": plan, "confirmation": f"activate:{scene.id}"}

    def _ensure_submit_allowed(self) -> None:
        if not self.config.control_enabled:
            raise ControlError(403, "control_disabled", "控制功能未启用")
        if self._poisoned_reason is not None:
            raise ControlError(503, "control_poisoned", "控制面最终化失败，必须重启并人工恢复")
        recovery = self.recovery_lock()
        if recovery is not None:
            raise ControlError(409, "recovery_required", "存在需人工处理的控制恢复锁", recovery)
        if self._active_task is not None and not self._active_task.done():
            raise ControlError(409, "operation_in_progress", "已有控制操作正在执行")

    async def recovery_preflight(self) -> dict[str, Any]:
        recovery = self.recovery_lock()
        if recovery is None:
            raise ControlError(404, "recovery_not_required", "当前不存在控制恢复锁")
        blockers: list[str] = []
        item_results: list[dict[str, Any]] = []
        for locked in recovery.get("items", []):
            env = self._environment(locked["environment_id"])
            expected = locked["expected_state"]
            state = await self.status(env)
            valid_expected = expected in {"running", "stopped"}
            if expected == "running":
                item_blockers = self.configuration_blockers(env, "start")
            else:
                item_blockers = []
                if not env.configured:
                    item_blockers.append(env.missing_reason or "缺少已核对的生命周期入口")
                if env.adapter is None:
                    item_blockers.append("缺少适配器配置")
            if not valid_expected:
                item_blockers.append("恢复锁期望状态无效，不能自动解除")
            elif state != expected:
                item_blockers.append(f"人工恢复状态尚未满足：期望 {expected}，实际 {state}")
            health = {"healthy": None, "checks": []}
            if valid_expected and state == "running":
                health = await self.check_health(env, state)
                if not health["healthy"]:
                    item_blockers.extend(
                        check["message"] for check in health["checks"] if not check["healthy"])
            item_blockers = list(dict.fromkeys(item_blockers))
            blockers.extend(f"{env.name}：{reason}" for reason in item_blockers)
            item_results.append({"environment_id": env.id, "expected_state": expected,
                                 "status": state, "ready": not item_blockers,
                                 "blockers": item_blockers, "health": health})
        if self._operation_in_progress():
            blockers.append("已有控制操作正在执行")
        confirmation_target = (recovery["environment_id"] if len(item_results) == 1
                               else recovery["operation_id"])
        return {
            "recovery_required": recovery,
            "environment_id": recovery["environment_id"],
            "expected_state": recovery["expected_state"],
            "status": item_results[0]["status"] if item_results else "unknown",
            "items": item_results,
            "ready": not blockers, "blockers": list(dict.fromkeys(blockers)),
            "health": item_results[0]["health"] if item_results else {"healthy": None, "checks": []},
            "confirmation": f"resolve-recovery:{confirmation_target}",
        }

    async def resolve_recovery(self, confirmation: str, username: str,
                               source_ip: str) -> None:
        if not self.config.control_enabled:
            raise ControlError(403, "control_disabled", "控制功能未启用")
        preflight = await self.recovery_preflight()
        if preflight["blockers"]:
            raise ControlError(409, "recovery_not_ready", "人工恢复状态预检未通过",
                               preflight["blockers"])
        if confirmation != preflight["confirmation"]:
            raise ControlError(422, "confirmation_mismatch", "精确确认文本不匹配")
        self._acquire_operation_lock()
        try:
            second = await self.recovery_preflight()
            # 当前调用持有自身租约，二次预检只忽略这一项并重新核验状态与健康。
            remaining = [item for item in second["blockers"] if item != "已有控制操作正在执行"]
            if remaining:
                raise ControlError(409, "recovery_not_ready", "解除前再次预检未通过", remaining)
            if not self.database.resolve_control_recovery(
                    second["recovery_required"]["operation_id"], username, source_ip):
                raise ControlError(409, "recovery_changed", "控制恢复锁已变化")
        finally:
            self._release_operation_lock()

    async def submit_environment(self, environment_id: str, action: str, confirmation: str,
                                 username: str, source_ip: str) -> str:
        self._ensure_submit_allowed()
        env = self._environment(environment_id)
        if not env.configured:
            raise ControlError(409, "environment_not_configured", "环境尚未配置", env.missing_reason)
        if action not in env.allowed_actions:
            raise ControlError(403, "action_not_allowed", "该环境未允许此动作")
        if confirmation != f"{action}:{env.id}":
            raise ControlError(422, "confirmation_mismatch", "精确确认文本不匹配")
        static_blockers = self.configuration_blockers(env, action)
        if static_blockers:
            raise ControlError(409, "environment_blocked", "环境静态配置完整性校验未通过",
                               static_blockers)
        self._acquire_operation_lock()
        operation_id = uuid.uuid4().hex
        try:
            self.database.create_operation(operation_id, "environment", env.id, action, username, source_ip)
            self._active_task = asyncio.create_task(
                self._run_with_operation_lock(
                    operation_id, self._run_environment(operation_id, env, action))
            )
        except Exception:
            self._release_operation_lock()
            raise
        return operation_id

    async def submit_scene(self, scene_id: str, confirmation: str,
                           username: str, source_ip: str) -> str:
        self._ensure_submit_allowed()
        preflight = await self.scene_preflight(scene_id)
        self._ensure_submit_allowed()
        if preflight["blockers"]:
            raise ControlError(409, "scene_blocked", "场景预检未通过", preflight["blockers"])
        if confirmation != f"activate:{scene_id}":
            raise ControlError(422, "confirmation_mismatch", "精确确认文本不匹配")
        self._acquire_operation_lock()
        operation_id = uuid.uuid4().hex
        try:
            self.database.create_operation(operation_id, "scene", scene_id, "activate", username, source_ip)
            self._active_task = asyncio.create_task(
                self._run_with_operation_lock(
                    operation_id,
                    self._run_scene(operation_id, self._scene(scene_id), preflight)
                )
            )
        except Exception:
            self._release_operation_lock()
            raise
        return operation_id

    async def _run_environment(self, operation_id: str, env: EnvironmentConfig, action: str) -> None:
        before = None
        step_created = False
        action_attempted = False
        adapter: Adapter | None = None
        action_sequence = 1
        rollback_sequence = 2
        restart_stop_confirmed = False
        restart_start_attempted = False
        try:
            self.database.update_operation(operation_id, status="running", started_at=utc_now())
            static_blockers = self.configuration_blockers(env, action)
            if static_blockers:
                self.database.create_operation_step(
                    operation_id, 1, "preflight", env.id, action)
                step_created = True
                raise ControlError(
                    409, "environment_blocked", "执行前静态配置完整性校验未通过",
                    static_blockers,
                )
            adapter = self._adapter(env)
            before = await self._adapter_call(adapter.status)
            desired = "running" if action in {"start", "restart"} else "stopped"
            has_runtime_safety = (action in {"start", "restart"} and bool(env.preflight_checks)) or (
                action == "stop" and any(isinstance(
                    check, (DrainHttpJsonCheck, DrainHttpPrometheusCheck, DrainHttpJsonArraysCheck))
                                         for check in env.preflight_checks))
            if has_runtime_safety and not (action == "stop" and before == "stopped"):
                self.database.create_operation_step(
                    operation_id, 1,
                    "drain" if action == "stop" else
                    "drain_then_safety_preflight" if action == "restart" else
                    "safety_preflight",
                    env.id, "wait_zero" if action == "stop" else
                    "wait_zero_then_validate" if action == "restart" else "validate",
                    before_state=before)
                step_created = True
                if action == "stop":
                    results = await self.wait_for_drain(env)
                elif action == "restart":
                    if before == "running":
                        results = await self.wait_for_drain(env)
                    else:
                        results = await self.safety_preflight(env, "start", {env.id: before})
                        failed = [result for result in results if not result["healthy"]]
                        if failed:
                            raise ControlError(409, "safety_preflight_failed",
                                               "restart 冷启动安全预检未通过", failed)
                else:
                    results = await self.safety_preflight(env, action, {env.id: before})
                    failed = [result for result in results if not result["healthy"]]
                    if failed:
                        raise ControlError(409, "safety_preflight_failed",
                                           "执行前安全预检未通过", failed)
                self.database.finish_operation_step(
                    operation_id, 1, "succeeded", before,
                    json.dumps(results, ensure_ascii=False))
                step_created = False
                action_sequence = 2
                rollback_sequence = 3
            self.database.create_operation_step(operation_id, action_sequence, "action", env.id, action, before_state=before)
            step_created = True
            if (action == "start" and before == "running") or (action == "stop" and before == "stopped"):
                if action == "start":
                    health = await self.wait_for_startup_health(env, before)
                    if not health["healthy"]:
                        raise ControlError(502, "health_verification_failed", "环境已运行但健康验证失败", health["checks"])
                self.database.finish_operation_step(operation_id, action_sequence, "succeeded", before, "noop")
                step_created = False
                self.database.finish_operation_with_audit(operation_id, "succeeded", "noop", before, before)
                return
            action_attempted = True
            if action == "restart":
                if before == "running":
                    await self._adapter_call(adapter.stop)
                    stopped = await self._adapter_call(adapter.status)
                    if stopped != "stopped":
                        raise ControlError(502, "verification_failed",
                                           "restart 停止阶段状态验证失败", {"state": stopped})
                    restart_stop_confirmed = True
                    startup_results = await self.safety_preflight(
                        env, "start", {env.id: stopped})
                    failed = [result for result in startup_results if not result["healthy"]]
                    if failed:
                        raise ControlError(409, "safety_preflight_failed",
                                           "restart 停止后启动型安全预检未通过", failed)
                else:
                    restart_stop_confirmed = True
                restart_start_attempted = True
                await self._adapter_call(adapter.start)
            else:
                await self._adapter_call(getattr(adapter, action))
            after = await self._adapter_call(adapter.status)
            if after != desired:
                raise ControlError(502, "verification_failed", "动作后状态验证失败", {"state": after})
            if desired == "running":
                health = await self.wait_for_startup_health(env, after)
                if not health["healthy"]:
                    raise ControlError(502, "health_verification_failed", "动作后健康验证失败", health["checks"])
            self.database.finish_operation_step(operation_id, action_sequence, "succeeded", after, "changed")
            step_created = False
            self.database.finish_operation_with_audit(operation_id, "succeeded", "changed", before, after)
        except asyncio.CancelledError:
            if step_created:
                self.database.finish_operation_step(
                    operation_id, action_sequence, "interrupted", None,
                    error_summary="管理器关闭，步骤已取消",
                )
            self.database.finish_operation_with_audit(operation_id, "interrupted", "interrupted", before, None, "管理器关闭，操作已取消")
            raise
        except Exception as exc:
            safe = self._error_summary(exc)
            if not action_attempted or adapter is None:
                if step_created:
                    self.database.finish_operation_step(
                        operation_id, action_sequence, "failed", None, error_summary=safe)
                self.database.finish_operation_with_audit(
                    operation_id, "failed", "failed", before, None, safe)
                return

            desired = "running" if action in {"start", "restart"} else "stopped"
            after_state: str | None = None
            try:
                after_state = await self._adapter_call(adapter.status)
            except Exception as reconcile_exc:
                safe = f"{safe}; reconcile: {self._error_summary(reconcile_exc)}"[:MAX_OUTPUT_CHARS]

            reconciled = after_state == desired and (
                action != "restart" or (restart_stop_confirmed and restart_start_attempted))
            if reconciled and desired == "running":
                try:
                    health = await self.wait_for_startup_health(env, after_state)
                    reconciled = health["healthy"]
                    if not reconciled:
                        safe = f"{safe}; health: {health['checks']}"[:MAX_OUTPUT_CHARS]
                except Exception as health_exc:
                    reconciled = False
                    safe = f"{safe}; health: {self._error_summary(health_exc)}"[:MAX_OUTPUT_CHARS]
            if reconciled:
                if step_created:
                    self.database.finish_operation_step(
                        operation_id, action_sequence, "succeeded", after_state, "reconciled")
                self.database.finish_operation_with_audit(
                    operation_id, "succeeded", "reconciled", before, after_state)
                return

            if step_created:
                self.database.finish_operation_step(
                    operation_id, action_sequence, "failed", after_state,
                    result="reconcile_failed", error_summary=safe)
                step_created = False

            known_states = {"running", "stopped"}
            changed = before in known_states and after_state in known_states and after_state != before
            ambiguous = after_state not in known_states
            recovery_expected = str(before) if before in known_states else "stopped"
            inverse = "stop" if before == "stopped" and after_state == "running" else (
                "start" if before == "running" and after_state == "stopped" else None)
            result = "failed"
            recovery_lock: dict[str, str] | None = None
            if changed and inverse is not None:
                inverse_ready = inverse in env.allowed_actions and not self.rollback_blockers(env, inverse)
                if inverse_ready:
                    self.database.create_operation_step(
                        operation_id, rollback_sequence, "rollback", env.id, inverse, before_state=after_state)
                    try:
                        if inverse == "stop":
                            await self.wait_for_drain(env)
                        else:
                            safety = await self.safety_preflight(env, "start", {env.id: after_state})
                            if any(not item["healthy"] for item in safety):
                                raise ControlError(409, "rollback_safety_failed",
                                                   "回滚启动前安全检查失败", safety)
                        await self._adapter_call(getattr(adapter, inverse))
                        restored = await self._adapter_call(adapter.status)
                        if restored != before:
                            raise ControlError(
                                502, "rollback_verification_failed", "环境回滚后状态验证失败",
                                {"expected": before, "actual": restored})
                        if restored == "running":
                            restored_health = await self.wait_for_startup_health(env, restored)
                            if not restored_health["healthy"]:
                                raise ControlError(
                                    502, "rollback_health_failed", "环境回滚后健康验证失败",
                                    restored_health["checks"])
                        after_state = restored
                        self.database.finish_operation_step(
                            operation_id, rollback_sequence, "succeeded", restored, "rollback_changed")
                        result = "rolled_back"
                    except Exception as rollback_exc:
                        rollback_safe = self._error_summary(rollback_exc)
                        restored = None
                        try:
                            restored = await self._adapter_call(adapter.status)
                        except Exception:
                            restored = None
                        rollback_reconciled = restored == before
                        if rollback_reconciled and restored == "running":
                            try:
                                restored_health = await self.wait_for_startup_health(env, restored)
                                rollback_reconciled = restored_health["healthy"]
                            except Exception:
                                rollback_reconciled = False
                        after_state = restored
                        if rollback_reconciled:
                            self.database.finish_operation_step(
                                operation_id, rollback_sequence, "succeeded", restored,
                                "rollback_reconciled")
                            result = "rolled_back"
                        else:
                            self.database.finish_operation_step(
                                operation_id, rollback_sequence, "failed", after_state,
                                error_summary=rollback_safe)
                            result = "rollback_failed"
                            recovery_lock = {"environment_id": env.id,
                                             "expected_state": recovery_expected,
                                             "reason": rollback_safe}
                else:
                    result = "recovery_required"
                    recovery_lock = {"environment_id": env.id,
                                     "expected_state": recovery_expected, "reason": safe}
            elif before not in known_states or ambiguous or (
                    action == "restart" and
                    (restart_stop_confirmed or restart_start_attempted)):
                result = "recovery_required"
                recovery_lock = {"environment_id": env.id,
                                 "expected_state": recovery_expected, "reason": safe}
            self.database.finish_operation_with_audit(
                operation_id, "failed", result, before, after_state, safe,
                recovery_lock=recovery_lock)

    async def _run_scene(self, operation_id: str, scene: SceneConfig, preflight: dict[str, Any]) -> None:
        changed: list[tuple[EnvironmentConfig, str]] = []
        attempted: set[str] = set()
        sequence = 0
        active_step_sequence: int | None = None
        baseline_statuses = dict(preflight["statuses"])
        health_baseline: dict[str, Any] = {}
        try:
            for env_id, baseline_state in baseline_statuses.items():
                if baseline_state == "running":
                    baseline_env = self.environments[env_id]
                    health_result = await self.check_health(baseline_env, baseline_state)
                    health_baseline[env_id] = {
                        "result": health_result,
                        "configuration": [check.model_dump()
                                          for check in baseline_env.health_checks],
                    }
                    if not health_result["healthy"]:
                        raise ControlError(
                            409, "scene_health_baseline_failed",
                            f"{baseline_env.name} 无法建立严格健康基线",
                            health_result["checks"])
            before_states = json.dumps(
                {"statuses": baseline_statuses, "health": health_baseline},
                ensure_ascii=False, sort_keys=True)
            self.database.update_operation(operation_id, status="running", started_at=utc_now(), before_state=before_states)
            execution_preflight = await self.scene_preflight(scene.id, include_operation_lock=False)
            if execution_preflight["blockers"]:
                raise ControlError(409, "scene_blocked", "执行前再次预检未通过", execution_preflight["blockers"])
            for planned in execution_preflight["plan"]:
                sequence += 1
                env = self.environments[planned["target_id"]]
                if planned["action"] == "drain":
                    before = await self.status(env)
                    self.database.create_operation_step(
                        operation_id, sequence, planned["phase"], env.id, "wait_zero",
                        before_state=before)
                    active_step_sequence = sequence
                    results = await self.wait_for_drain(env)
                    self.database.finish_operation_step(
                        operation_id, sequence, "succeeded", before,
                        json.dumps(results, ensure_ascii=False))
                    active_step_sequence = None
                    continue
                if planned["action"] == "safety_check":
                    before = await self.status(env)
                    check = env.preflight_checks[planned["check_index"]]
                    self.database.create_operation_step(
                        operation_id, sequence, planned["phase"], env.id, check.type,
                        before_state=before)
                    active_step_sequence = sequence
                    current_statuses = await self._scene_states(scene)
                    result = await self.check_safety(env, check, current_statuses)
                    if not result["healthy"]:
                        raise ControlError(409, "scene_safety_check_failed",
                                           f"{env.name} 场景安全检查失败", result)
                    self.database.finish_operation_step(
                        operation_id, sequence, "succeeded", before,
                        json.dumps(result, ensure_ascii=False))
                    active_step_sequence = None
                    continue
                adapter = None
                if planned["action"] == "status":
                    before = await self.status(env)
                else:
                    adapter = self._adapter(env)
                    before = await self._adapter_call(adapter.status)
                self.database.create_operation_step(operation_id, sequence, planned["phase"], env.id, planned["action"], before_state=before)
                active_step_sequence = sequence
                if planned["action"] == "status":
                    expected = planned["expected"]
                    if before != expected:
                        raise ControlError(
                            502, "verification_failed", f"{env.name} 最终状态不符合场景",
                            {"expected": expected, "actual": before},
                        )
                    if expected == "running":
                        health = await self.wait_for_startup_health(env, before)
                        if not health["healthy"]:
                            raise ControlError(502, "health_verification_failed", f"{env.name} 健康验证失败", health["checks"])
                    self.database.finish_operation_step(operation_id, sequence, "succeeded", before, "verified")
                    active_step_sequence = None
                    continue
                desired = "stopped" if planned["action"] == "stop" else "running"
                if before == desired:
                    self.database.finish_operation_step(operation_id, sequence, "succeeded", before, "noop")
                    active_step_sequence = None
                    continue
                try:
                    attempted.add(env.id)
                    if adapter is None:
                        raise ControlError(500, "adapter_missing", "执行动作缺少固定适配器")
                    await self._adapter_call(getattr(adapter, planned["action"]))
                except Exception as action_exc:
                    try:
                        changed_state = await self._adapter_call(adapter.status)
                        if changed_state != before and changed_state == desired:
                            changed.append((env, "start" if planned["action"] == "stop" else "stop"))
                    except Exception as probe_exc:
                        raise ControlError(
                            502, "action_and_status_failed", "动作失败且无法确认是否改变状态",
                            {"action_error": self._error_summary(action_exc),
                             "status_error": self._error_summary(probe_exc)},
                        ) from action_exc
                    raise action_exc
                changed.append((env, "start" if planned["action"] == "stop" else "stop"))
                after = await self._adapter_call(adapter.status)
                if after != desired:
                    raise ControlError(502, "verification_failed", f"{env.name} 动作后验证失败")
                self.database.finish_operation_step(operation_id, sequence, "succeeded", after, "changed")
                active_step_sequence = None
            final = await self._scene_states(scene)
            if (any(final[env_id] != "running" for env_id in scene.desired)
                    or any(final[env_id] != "stopped" for env_id in scene.conflicts)):
                raise ControlError(502, "scene_final_verification_failed", "场景最终复核未通过", final)
            final_health = {
                env_id: await self.wait_for_startup_health(
                    self.environments[env_id], final[env_id])
                for env_id in scene.desired
            }
            if any(not result["healthy"] for result in final_health.values()):
                raise ControlError(502, "scene_health_verification_failed", "场景最终健康复核未通过", final_health)
            self.database.finish_operation_with_audit(
                operation_id, "succeeded", "changed" if changed else "noop", before_states,
                json.dumps(final, ensure_ascii=False, sort_keys=True),
            )
        except asyncio.CancelledError:
            if active_step_sequence is not None:
                self.database.finish_operation_step(
                    operation_id, active_step_sequence, "interrupted", None,
                    error_summary="管理器关闭，场景步骤已取消",
                )
            self.database.finish_operation_with_audit(operation_id, "interrupted", "interrupted", json.dumps(preflight["statuses"], ensure_ascii=False, sort_keys=True), None, "管理器关闭，场景操作已取消")
            raise
        except Exception as exc:
            safe = self._error_summary(exc)
            if active_step_sequence is not None:
                self.database.finish_operation_step(
                    operation_id, active_step_sequence, "failed", None, error_summary=safe
                )
            rollback_failed = False
            recovery_items: list[dict[str, str]] = []
            for env_id in (*scene.desired, *scene.conflicts):
                env = self.environments[env_id]
                expected = baseline_statuses.get(env_id)
                safe_expected = expected if expected in {"running", "stopped"} else "stopped"
                try:
                    current = await self._adapter_call(self._adapter(env).status)
                except Exception as status_exc:
                    recovery_items.append({"environment_id": env.id,
                                           "expected_state": safe_expected,
                                           "reason": self._error_summary(status_exc)})
                    continue
                if current == safe_expected:
                    if current == "running":
                        restored_health = await self.check_health(env, current)
                        if not restored_health["healthy"]:
                            recovery_items.append({"environment_id": env.id,
                                                   "expected_state": "running",
                                                   "reason": "回滚后严格健康检查未恢复"})
                    continue
                if current not in {"running", "stopped"} or env.id not in attempted:
                    recovery_items.append({"environment_id": env.id,
                                           "expected_state": safe_expected,
                                           "reason": f"场景失败后状态无法安全恢复：{current}"})
                    continue
                rollback_action = "start" if safe_expected == "running" else "stop"
                sequence += 1
                self.database.create_operation_step(
                    operation_id, sequence, "rollback", env.id, rollback_action,
                    before_state=current)
                try:
                    if rollback_action not in env.allowed_actions:
                        raise ControlError(403, "rollback_not_allowed",
                                           f"{env.name} 未授权回滚动作 {rollback_action}")
                    if self.rollback_blockers(env, rollback_action):
                        raise ControlError(409, "rollback_configuration_blocked",
                                           f"{env.name} 回滚配置不完整",
                                           self.rollback_blockers(env, rollback_action))
                    if rollback_action == "stop":
                        await self.wait_for_drain(env)
                    else:
                        rollback_safety = await self.safety_preflight(
                            env, "start", await self._scene_states(scene))
                        if any(not item["healthy"] for item in rollback_safety):
                            raise ControlError(409, "rollback_safety_failed",
                                               f"{env.name} 回滚启动前安全检查失败",
                                               rollback_safety)
                    await self._adapter_call(getattr(self._adapter(env), rollback_action))
                    after = await self._adapter_call(self._adapter(env).status)
                    if after != safe_expected:
                        raise ControlError(502, "rollback_verification_failed",
                                           f"{env.name} 回滚后状态验证失败",
                                           {"expected": safe_expected, "actual": after})
                    if after == "running":
                        restored_health = await self.wait_for_startup_health(env, after)
                        if not restored_health["healthy"]:
                            raise ControlError(502, "rollback_health_failed",
                                               f"{env.name} 回滚后严格健康验证失败",
                                               restored_health["checks"])
                    self.database.finish_operation_step(
                        operation_id, sequence, "succeeded", after, "rollback_changed")
                except Exception as rollback_exc:
                    rollback_failed = True
                    rollback_safe = self._error_summary(rollback_exc)
                    self.database.finish_operation_step(
                        operation_id, sequence, "failed", None,
                        error_summary=rollback_safe)
                    recovery_items.append({"environment_id": env.id,
                                           "expected_state": safe_expected,
                                           "reason": rollback_safe})
            recovery_items = list({item["environment_id"]: item for item in recovery_items}.values())
            result = ("rollback_failed" if rollback_failed else
                      "recovery_required" if recovery_items else
                      "rolled_back" if attempted else "failed")
            final = await self._scene_states(scene)
            self.database.finish_operation_with_audit(
                operation_id, "failed", result,
                json.dumps({"statuses": baseline_statuses, "health": health_baseline},
                           ensure_ascii=False, sort_keys=True),
                json.dumps(final, ensure_ascii=False, sort_keys=True), safe,
                recovery_items=recovery_items or None,
            )

    async def _scene_states(self, scene: SceneConfig) -> dict[str, str]:
        return {
            env_id: await self.status(self.environments[env_id])
            for env_id in (*scene.desired, *scene.conflicts)
        }

    @staticmethod
    def _error_summary(exc: Exception) -> str:
        if isinstance(exc, ControlError) and exc.details is not None:
            raw = f"{exc.code}: {exc.message}; {exc.details}"
        else:
            raw = f"{type(exc).__name__}: {exc}"
        return redact_sensitive_text(raw[:MAX_OUTPUT_CHARS])[0]

    async def shutdown(self, timeout: float = 10.0) -> None:
        """等待活跃操作；阈值后请求取消，但适配器仍完成有限超时与安全收尾。"""
        task = self._active_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return
