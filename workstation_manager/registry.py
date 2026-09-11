from __future__ import annotations

import asyncio
import errno
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlsplit

from .database import Database, DatabaseError, OperationBusyError, utc_now
from .portproxy import (
    PortProxySyncError,
    WslPortProxySynchronizer,
    mapping_from_service,
)
from .redaction import redact_value


SERVICE_STATES = {"running", "stopped", "unhealthy", "unknown"}
SERVICE_ACTIONS = {"start", "stop", "restart"}
SCRIPT_SUFFIXES = {".cmd", ".bat", ".ps1"}
ID_RE = re.compile(r"[0-9a-f]{32}")
HEALTH_INTERVAL_SECONDS = 5.0
HEALTH_TIMEOUT_SECONDS = 1.0
HEALTH_CONCURRENCY = 2
HEALTH_FAILURE_THRESHOLD = 2
HEALTH_BODY_LIMIT = 256 * 1024


def _windows_open_error_code(path: Path) -> int | None:
    """Return the native CreateFile error without collapsing it to errno."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path), 0xC0000000, 0x00000007, None, 3, 0x00000080, None
    )
    if handle == wintypes.HANDLE(-1).value:
        return ctypes.get_last_error()
    close_handle(handle)
    return 0


class RegistryError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ManagerInstanceLock:
    """Hold an exclusive cross-process lock associated with the database."""

    def __init__(
        self, database_path: Path, *, suffix: str | None = None,
        busy_code: str = "manager_already_running",
        busy_message: str = "同一数据库已有管理器实例正在运行",
        lock_error_code: str = "manager_lock_failed",
        lock_error_message: str = "无法取得管理器实例锁",
        unlock_error_code: str = "manager_unlock_failed",
        unlock_message: str = "无法释放管理器实例锁",
    ) -> None:
        self.path = database_path.with_suffix(suffix or f"{database_path.suffix}.lock")
        self.busy_code = busy_code
        self.busy_message = busy_message
        self.lock_error_code = lock_error_code
        self.lock_error_message = lock_error_message
        self.unlock_error_code = unlock_error_code
        self.unlock_message = unlock_message
        self._handle: Any | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        try:
            handle = self.path.open("a+b")
        except OSError as exc:
            native_error = getattr(exc, "winerror", None)
            if native_error is None and exc.errno == errno.EACCES:
                native_error = _windows_open_error_code(self.path)
            if native_error == 32:
                raise RegistryError(409, self.busy_code, self.busy_message) from exc
            raise RegistryError(
                500, self.lock_error_code, f"{self.lock_error_message}: {exc}"
            ) from exc
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            busy_numbers = {errno.EACCES, errno.EAGAIN}
            if exc.errno in busy_numbers or getattr(exc, "winerror", None) in {33, 36}:
                raise RegistryError(409, self.busy_code, self.busy_message) from exc
            raise RegistryError(
                500, self.lock_error_code, f"{self.lock_error_message}: {exc}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise RegistryError(500, self.unlock_error_code, self.unlock_message) from exc
        finally:
            handle.close()


@dataclass(frozen=True)
class ScriptResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class HealthProbeResult:
    state: str
    error: str | None = None
    reachable: bool = False


class HttpHealthProbe:
    def __init__(self, timeout_seconds: float = HEALTH_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds

    def probe(self, url: str, expected_text: str) -> HealthProbeResult:
        request = Request(url, headers={"User-Agent": "AXIS-Service-Monitor/1"})
        try:
            response = build_opener(ProxyHandler({})).open(
                request, timeout=self.timeout_seconds
            )
            with response:
                status = int(response.status)
                body = response.read(HEALTH_BODY_LIMIT + 1)
        except HTTPError as exc:
            return HealthProbeResult(
                "unhealthy", f"健康接口返回 HTTP {exc.code}", True
            )
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, (ConnectionRefusedError, ConnectionResetError)):
                return HealthProbeResult("stopped", None, False)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return HealthProbeResult("unknown", "健康接口响应超时", False)
            return HealthProbeResult("unknown", f"健康检查失败: {reason}", False)
        except (OSError, ValueError) as exc:
            if isinstance(exc, (ConnectionRefusedError, ConnectionResetError)):
                return HealthProbeResult("stopped", None, False)
            if isinstance(exc, (TimeoutError, socket.timeout)):
                return HealthProbeResult("unknown", "健康接口响应超时", False)
            return HealthProbeResult("unknown", f"健康检查失败: {exc}", False)
        if not 200 <= status < 300:
            return HealthProbeResult("unhealthy", f"健康接口返回 HTTP {status}", True)
        if len(body) > HEALTH_BODY_LIMIT:
            return HealthProbeResult("unknown", "健康接口响应超过 256 KiB", True)
        text = body.decode("utf-8", errors="replace")
        if expected_text and expected_text not in text:
            return HealthProbeResult("unhealthy", "健康接口响应与服务身份不匹配", True)
        return HealthProbeResult("running", None, True)


class ScriptRunner:
    OUTPUT_LIMIT = 4096
    OUTPUT_READ_BYTES = OUTPUT_LIMIT * 4 + 3

    def __init__(self, action_timeout_seconds: float = 600.0,
                 status_timeout_seconds: float = 3.0) -> None:
        self.action_timeout_seconds = action_timeout_seconds
        self.status_timeout_seconds = status_timeout_seconds

    @staticmethod
    def validate_path(value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise RegistryError(422, "invalid_script", "管理脚本必须使用绝对路径")
        if path.suffix.lower() not in SCRIPT_SUFFIXES:
            raise RegistryError(422, "invalid_script", "管理脚本只支持 .cmd、.bat、.ps1")
        if not path.is_file():
            raise RegistryError(422, "script_not_found", "管理脚本不存在")
        return path.resolve()

    @staticmethod
    def _command(path: Path, action: str) -> list[str]:
        if path.suffix.lower() == ".ps1":
            executable = shutil.which("powershell.exe") or shutil.which("powershell")
            if executable is None:
                raise RegistryError(500, "powershell_not_found", "未找到 PowerShell")
            return [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(path), action]
        executable = os.environ.get("COMSPEC") or shutil.which("cmd.exe") or shutil.which("cmd")
        if executable is None:
            raise RegistryError(500, "cmd_not_found", "未找到 Windows 命令解释器")
        invocation = f'call "{path}" {action}'
        return [executable, "/d", "/s", "/c", invocation]

    @staticmethod
    def _environment(path: Path) -> dict[str, str]:
        environment = dict(os.environ)
        if os.name == "nt" and path.suffix.lower() == ".ps1":
            environment = {
                key: value
                for key, value in environment.items()
                if key.lower() != "psmodulepath"
            }
        return environment

    @classmethod
    def _read_output_tail(cls, stream: Any) -> str:
        stream.flush()
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - cls.OUTPUT_READ_BYTES))
        return stream.read(cls.OUTPUT_READ_BYTES).decode(
            "utf-8", errors="replace"
        )[-cls.OUTPUT_LIMIT:].strip()

    def run(self, script_path: str, action: str) -> ScriptResult:
        if action not in SERVICE_ACTIONS | {"status"}:
            raise RegistryError(422, "invalid_action", "脚本动作无效")
        path = self.validate_path(script_path)
        timeout = self.status_timeout_seconds if action == "status" else self.action_timeout_seconds
        try:
            with tempfile.TemporaryFile(mode="w+b") as stdout_file, \
                    tempfile.TemporaryFile(mode="w+b") as stderr_file:
                completed = subprocess.run(
                    self._command(path, action), cwd=path.parent, shell=False,
                    stdout=stdout_file, stderr=stderr_file,
                    timeout=timeout, check=False,
                    env=self._environment(path),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                stdout = self._read_output_tail(stdout_file)
                stderr = self._read_output_tail(stderr_file)
        except subprocess.TimeoutExpired as exc:
            raise RegistryError(504, "script_timeout", f"脚本动作 {action} 执行超时") from exc
        except OSError as exc:
            raise RegistryError(500, "script_launch_failed", f"无法启动管理脚本: {exc}") from exc
        return ScriptResult(
            completed.returncode,
            stdout,
            stderr,
        )


def validate_service_input(payload: dict[str, Any], runner: ScriptRunner) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not 1 <= len(name) <= 100:
        raise RegistryError(422, "invalid_name", "服务名称长度必须为 1..100")
    description = str(payload.get("description") or "").strip()
    if len(description) > 1000:
        raise RegistryError(422, "invalid_description", "服务说明不能超过 1000 个字符")
    script_path = str(runner.validate_path(str(payload.get("script_path") or "")))
    gpu_label = str(payload.get("gpu_label") or "").strip()
    if len(gpu_label) > 100:
        raise RegistryError(422, "invalid_gpu", "GPU 标签不能超过 100 个字符")
    raw_port = payload.get("port")
    try:
        port = None if raw_port in {None, ""} else int(raw_port)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RegistryError(422, "invalid_port", "服务端口必须是整数") from exc
    if port is not None and not 1 <= port <= 65535:
        raise RegistryError(422, "invalid_port", "服务端口必须在 1..65535")
    ui_url = str(payload.get("ui_url") or "").strip()
    if ui_url:
        parsed = urlsplit(ui_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RegistryError(422, "invalid_ui_url", "UI 地址必须是完整的 HTTP/HTTPS 地址")
    health_url = str(payload.get("health_url") or "").strip()
    if health_url:
        try:
            parsed = urlsplit(health_url)
            port_value = parsed.port
        except ValueError as exc:
            raise RegistryError(422, "invalid_health_url", "健康检查地址格式无效") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            raise RegistryError(
                422, "invalid_health_url", "健康检查地址必须是完整的 HTTP/HTTPS 地址"
            )
        if parsed.username is not None or parsed.password is not None:
            raise RegistryError(422, "invalid_health_url", "健康检查地址不能包含用户名或密码")
        if parsed.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
            raise RegistryError(422, "invalid_health_url", "健康检查地址只允许本机 loopback")
        if port_value is not None and not 1 <= port_value <= 65535:
            raise RegistryError(422, "invalid_health_url", "健康检查端口必须在 1..65535")
    health_expect = str(payload.get("health_expect") or "").strip()
    if len(health_expect) > 512:
        raise RegistryError(422, "invalid_health_expect", "健康检查匹配内容不能超过 512 个字符")
    if health_expect and not health_url:
        raise RegistryError(422, "invalid_health_expect", "填写匹配内容前必须填写健康检查地址")
    wsl_portproxy_enabled = bool(payload.get("wsl_portproxy_enabled", False))
    wsl_distro = str(payload.get("wsl_distro") or "Ubuntu-22.04").strip()
    if not 1 <= len(wsl_distro) <= 100 or any(ord(character) < 32 for character in wsl_distro):
        raise RegistryError(422, "invalid_wsl_distro", "WSL 发行版名称长度必须为 1..100")
    wsl_listen_address = str(payload.get("wsl_listen_address") or "0.0.0.0").strip()
    try:
        parsed_listen_address = ipaddress.ip_address(wsl_listen_address)
    except ValueError as exc:
        raise RegistryError(422, "invalid_wsl_listen_address", "局域网监听地址必须是 IPv4 地址") from exc
    if parsed_listen_address.version != 4 or not (
        parsed_listen_address.is_unspecified
        or parsed_listen_address.is_private
        or parsed_listen_address.is_loopback
    ):
        raise RegistryError(
            422, "invalid_wsl_listen_address", "局域网监听地址只允许 0.0.0.0、私网或 loopback IPv4"
        )

    def optional_proxy_port(key: str, label: str) -> int | None:
        raw_value = payload.get(key)
        try:
            value = None if raw_value in {None, ""} else int(raw_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RegistryError(422, "invalid_wsl_portproxy_port", f"{label}必须是整数") from exc
        if value is not None and not 1 <= value <= 65535:
            raise RegistryError(422, "invalid_wsl_portproxy_port", f"{label}必须在 1..65535")
        return value

    wsl_listen_port = optional_proxy_port("wsl_listen_port", "Windows 监听端口")
    wsl_connect_port = optional_proxy_port("wsl_connect_port", "WSL 目标端口")
    if wsl_portproxy_enabled:
        wsl_listen_port = wsl_listen_port or port
        wsl_connect_port = wsl_connect_port or wsl_listen_port
        if wsl_listen_port is None:
            raise RegistryError(
                422, "invalid_wsl_portproxy_port", "启用 WSL 局域网映射时必须填写服务端口或监听端口"
            )
    return {"name": name, "description": description, "script_path": script_path,
            "gpu_label": gpu_label, "port": port, "ui_url": ui_url,
            "health_url": health_url, "health_expect": health_expect,
            "wsl_portproxy_enabled": wsl_portproxy_enabled,
            "wsl_distro": wsl_distro,
            "wsl_listen_address": wsl_listen_address,
            "wsl_listen_port": wsl_listen_port,
            "wsl_connect_port": wsl_connect_port}


def validate_scene_input(payload: dict[str, Any], database: Database) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not 1 <= len(name) <= 100:
        raise RegistryError(422, "invalid_name", "场景名称长度必须为 1..100")
    description = str(payload.get("description") or "").strip()
    if len(description) > 1000:
        raise RegistryError(422, "invalid_description", "场景说明不能超过 1000 个字符")
    raw_ids = payload.get("service_ids")
    if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
        raise RegistryError(422, "invalid_services", "场景服务必须是有序 ID 数组")
    service_ids = list(dict.fromkeys(raw_ids))
    known = {item["id"] for item in database.list_registered_services()}
    missing = [item for item in service_ids if item not in known]
    if missing:
        raise RegistryError(422, "service_not_found", "场景包含不存在的已登记服务")
    result = {
        "name": name,
        "description": description,
        "service_ids": service_ids,
    }
    if "is_default_generation" in payload:
        result["is_default_generation"] = bool(payload["is_default_generation"])
    if "detailed_description" in payload:
        detailed_description = str(payload.get("detailed_description") or "").strip()
        if len(detailed_description) > 8000:
            raise RegistryError(
                422, "invalid_detailed_description", "场景详细说明不能超过 8000 个字符"
            )
        result["detailed_description"] = detailed_description
    return result


class RegisteredServiceManager:
    def __init__(
        self, database: Database, runner: ScriptRunner | None = None,
        health_probe: HttpHealthProbe | None = None,
        health_interval_seconds: float = HEALTH_INTERVAL_SECONDS,
        health_failure_threshold: int = HEALTH_FAILURE_THRESHOLD,
        portproxy_synchronizer: WslPortProxySynchronizer | None = None,
    ) -> None:
        if health_interval_seconds <= 0:
            raise ValueError("健康检查间隔必须大于 0")
        if health_failure_threshold < 1:
            raise ValueError("健康检查失败阈值必须至少为 1")
        self.database = database
        self.runner = runner or ScriptRunner()
        self.health_probe = health_probe or HttpHealthProbe()
        self.health_interval_seconds = health_interval_seconds
        self.health_failure_threshold = health_failure_threshold
        self.portproxy_synchronizer = portproxy_synchronizer or WslPortProxySynchronizer()
        initial_services = self.database.list_registered_services()
        self.statuses = {item["id"]: self._stored_status(item) for item in initial_services}
        self._health_authorities = {
            item["id"]: self._health_authority(item) for item in initial_services
        }
        self._registered_ports = {
            item["id"]: item.get("port") for item in initial_services
        }
        self._health_failures: dict[str, int] = {}
        self._health_task: asyncio.Task[None] | None = None
        self._health_semaphore = asyncio.Semaphore(HEALTH_CONCURRENCY)
        self._operation_tasks: set[asyncio.Task[None]] = set()
        self._cancel_requests: dict[str, asyncio.Event] = {}
        self._operation_pending = False
        self._busy_services: set[str] = set()
        self._service_locks: dict[str, asyncio.Lock] = {}
        self._instance_lock = ManagerInstanceLock(self.database.path)
        self._docker_handoff_lock = ManagerInstanceLock(
            self.database.path,
            suffix=".docker-handoff.lock",
            busy_code="docker_handoff_busy",
            busy_message="Docker 正在执行开机或登录会话交接，请稍后重试",
            lock_error_code="docker_handoff_lock_failed",
            lock_error_message="无法取得 Docker 会话交接互斥锁",
            unlock_error_code="docker_handoff_unlock_failed",
            unlock_message="无法释放 Docker 会话交接互斥锁",
        )
        self.last_operation_error: str | None = None
        self.last_health_error: str | None = None
        self.portproxy_errors: dict[str, str] = {}

    @property
    def last_portproxy_error(self) -> str | None:
        if not self.portproxy_errors:
            return None
        return "; ".join(dict.fromkeys(self.portproxy_errors.values()))[:1024]

    async def _sync_all_portproxies(self) -> None:
        services = self.database.list_registered_services()
        errors, synced_addresses = await asyncio.to_thread(
            self.portproxy_synchronizer.sync_services, services
        )
        for service_id, address in synced_addresses.items():
            self.database.update_registered_service_portproxy_address(service_id, address)
        self.portproxy_errors = {
            service_id: redact_value(message) for service_id, message in errors.items()
        }

    async def _remove_owned_portproxy(self, service: dict[str, Any]) -> bool:
        mapping = mapping_from_service(service)
        if mapping is None:
            return False
        try:
            return await asyncio.to_thread(self.portproxy_synchronizer.remove_owned, mapping)
        except PortProxySyncError as exc:
            message = redact_value(str(exc))
            self.portproxy_errors[service["id"]] = message
            raise RegistryError(503, "portproxy_sync_failed", message) from exc

    async def _restore_owned_portproxy(self, service: dict[str, Any]) -> str | None:
        mapping = mapping_from_service(service)
        if mapping is None:
            return None
        try:
            await asyncio.to_thread(self.portproxy_synchronizer.restore_owned, mapping)
        except PortProxySyncError as exc:
            return redact_value(str(exc))
        return None

    @staticmethod
    def _portproxy_identity(service: dict[str, Any]) -> tuple[Any, ...]:
        return (
            bool(service.get("wsl_portproxy_enabled")),
            service.get("wsl_distro"),
            service.get("wsl_listen_address"),
            service.get("wsl_listen_port"),
            service.get("wsl_connect_port"),
        )

    @staticmethod
    def _stored_status(service: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": service.get("observed_state", service.get("recorded_state", "unknown")),
            "checked_at": service.get("observed_at", service.get("state_updated_at")),
            "error": service.get("observed_error", service.get("state_error")),
            "source": "stored",
        }

    def _set_status(
        self, service_id: str, state: str, error: str | None = None,
        source: str = "script",
    ) -> dict[str, Any]:
        if state not in SERVICE_STATES:
            raise RegistryError(500, "invalid_stored_state", "无法保存无效的服务状态")
        safe_error = redact_value(error) if error is not None else None
        previous = self.statuses.get(service_id, {})
        checked_at = utc_now()
        if previous.get("state") != state or previous.get("error") != safe_error:
            if not self.database.update_registered_service_status(service_id, state, safe_error):
                raise RegistryError(404, "service_not_found", "已登记服务不存在")
        status = {
            "state": state, "checked_at": checked_at, "error": safe_error,
            "source": source,
        }
        self.statuses[service_id] = status
        return status

    def _set_desired_state(self, service_id: str, state: str) -> None:
        if state not in {"running", "stopped", "unknown"}:
            raise RegistryError(500, "invalid_desired_state", "无法保存无效的服务期望状态")
        if not self.database.update_registered_service_desired_state(service_id, state):
            raise RegistryError(404, "service_not_found", "已登记服务不存在")

    async def _reconcile_service_status(
        self, service: dict[str, Any], source: str,
    ) -> dict[str, Any]:
        status = await self._probe_status(service)
        state = status["state"]
        self._set_desired_state(
            service["id"], state if state in {"running", "stopped"} else "unknown"
        )
        self._health_failures.pop(service["id"], None)
        return self._set_status(service["id"], state, status["error"], source)

    async def _reconcile_failed_action_status(
        self, service: dict[str, Any], action_error: str,
    ) -> tuple[dict[str, Any], str | None]:
        try:
            return await self._reconcile_service_status(service, "script"), None
        except (DatabaseError, RegistryError) as exc:
            detail = exc.message if isinstance(exc, RegistryError) else str(exc)
            reconciliation_error = (
                f"失败后状态校准失败 ({type(exc).__name__}): {detail}"
            )
            try:
                self._set_desired_state(service["id"], "unknown")
                status = self._set_status(
                    service["id"], "unknown",
                    f"{action_error}; {reconciliation_error}", "action",
                )
                return status, reconciliation_error
            except (DatabaseError, RegistryError) as fallback_exc:
                fallback_detail = (
                    fallback_exc.message
                    if isinstance(fallback_exc, RegistryError) else str(fallback_exc)
                )
                fallback_error = (
                    f"{reconciliation_error}; 无法持久化 unknown 回退状态 "
                    f"({type(fallback_exc).__name__}): {fallback_detail}"
                )
                status = {
                    "state": "unknown", "checked_at": utc_now(),
                    "error": redact_value(f"{action_error}; {fallback_error}"),
                    "source": "action",
                }
                self.statuses[service["id"]] = status
                return status, fallback_error

    async def start(self) -> None:
        if self._health_task is not None:
            raise RuntimeError("服务健康监控已经启动")
        self._instance_lock.acquire()
        try:
            self.database.interrupt_simple_operations()
            self.statuses = {
                item["id"]: self._stored_status(item)
                for item in self.database.list_registered_services()
            }
            self._reload_health_authorities()
            await self._sync_all_portproxies()
            if self.database.get_default_scene() is None:
                unknown_services = []
                for service in self.database.list_registered_services():
                    status = await self._reconcile_service_status(service, "startup")
                    if status["state"] == "unknown":
                        unknown_services.append(service)
                for service in unknown_services:
                    await self._reconcile_service_status(service, "startup")
            self._health_task = asyncio.create_task(self._health_loop())
        except Exception:
            self._instance_lock.release()
            raise

    async def shutdown(self) -> None:
        health_task = self._health_task
        self._health_task = None
        if health_task is not None:
            health_task.cancel()
            try:
                await health_task
            except asyncio.CancelledError:
                pass
        if self._operation_tasks:
            await asyncio.gather(*tuple(self._operation_tasks), return_exceptions=True)
        self._docker_handoff_lock.release()
        self._instance_lock.release()

    @staticmethod
    def _health_authority(service: dict[str, Any]) -> tuple[str, str, int | None] | None:
        health_url = str(service.get("health_url") or "")
        if not health_url:
            return None
        parsed = urlsplit(health_url)
        scheme = parsed.scheme.lower()
        port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
        return scheme, (parsed.hostname or "").lower(), port

    def _peer_is_running(self, service: dict[str, Any]) -> bool:
        authority = self._health_authority(service)
        if authority is None:
            return False
        registered_port = service.get("port")
        for peer_id, peer_authority in self._health_authorities.items():
            if peer_id == service["id"]:
                continue
            shares_health_authority = peer_authority == authority
            shares_registered_port = registered_port is not None \
                and self._registered_ports.get(peer_id) == registered_port
            if not shares_health_authority and not shares_registered_port:
                continue
            if self.statuses.get(peer_id, {}).get("state") == "running":
                return True
        return False

    def _reload_health_authorities(self) -> None:
        services = self.database.list_registered_services()
        self._health_authorities = {
            item["id"]: self._health_authority(item) for item in services
        }
        self._registered_ports = {item["id"]: item.get("port") for item in services}

    async def _probe_health(self, service: dict[str, Any]) -> HealthProbeResult:
        async with self._health_semaphore:
            return await asyncio.to_thread(
                self.health_probe.probe,
                service["health_url"], service.get("health_expect", ""),
            )

    def _record_health_result(
        self, service: dict[str, Any], result: HealthProbeResult,
        *, immediate: bool = False,
    ) -> dict[str, Any]:
        service_id = service["id"]
        state = result.state
        error = result.error
        desired_state = service.get("desired_state", "unknown")
        previous = self.statuses.get(
            service_id, {"state": "unknown", "checked_at": None, "error": None}
        )
        if state == "unknown" and not result.reachable:
            if desired_state == "stopped":
                state, error = "stopped", None
            elif desired_state == "running":
                state = "unhealthy"
            elif previous.get("state") == "unhealthy":
                self._health_failures.pop(service_id, None)
                preserved = dict(previous)
                preserved["checked_at"] = utc_now()
                self.statuses[service_id] = preserved
                return preserved
        if state == "unhealthy" and result.reachable and self._peer_is_running(service):
            state, error = "stopped", None
        if state == "running" or immediate:
            self._health_failures.pop(service_id, None)
            return self._set_status(service_id, state, error, "health")
        failures = self._health_failures.get(service_id, 0) + 1
        self._health_failures[service_id] = failures
        if failures >= self.health_failure_threshold:
            return self._set_status(service_id, state, error, "health")
        pending = dict(previous)
        pending["checked_at"] = utc_now()
        pending["source"] = "health"
        self.statuses[service_id] = pending
        return pending

    async def refresh_service_health(
        self, service: dict[str, Any], *, immediate: bool = False,
    ) -> dict[str, Any]:
        if not service.get("health_url"):
            return self.statuses.get(service["id"], self._stored_status(service))
        async with self._service_lock(service["id"]):
            result = await self._probe_health(service)
            return self._record_health_result(service, result, immediate=immediate)

    async def refresh_all_health(self, *, immediate: bool = False) -> None:
        services = [
            item for item in self.database.list_registered_services()
            if item.get("health_url") and item["id"] not in self._busy_services
        ]
        if not services:
            self.last_health_error = None
            return
        results = await asyncio.gather(
            *(self.refresh_service_health(service, immediate=immediate) for service in services),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            self.last_health_error = "; ".join(
                f"{type(error).__name__}: {error}" for error in errors
            )[:1024]
        else:
            self.last_health_error = None

    async def _health_loop(self) -> None:
        while True:
            try:
                await self.refresh_all_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_health_error = f"{type(exc).__name__}: {exc}"[:1024]
            await asyncio.sleep(self.health_interval_seconds)

    async def _probe_status(self, service: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(self.runner.run, service["script_path"], "status")
            if result.returncode != 0:
                state = "unknown"
                error = result.stderr or result.stdout or f"退出码 {result.returncode}"
            else:
                state = result.stdout.strip()
                if state not in SERVICE_STATES:
                    state = "unknown"
                    error = "status 必须输出 running/stopped/unhealthy/unknown"
                else:
                    error = None
        except RegistryError as exc:
            state, error = "unknown", exc.message
        return {"state": state, "checked_at": utc_now(), "error": error}

    def _service_lock(self, service_id: str) -> asyncio.Lock:
        return self._service_locks.setdefault(service_id, asyncio.Lock())

    async def refresh_status(self, service: dict[str, Any]) -> dict[str, Any]:
        service_id = service["id"]
        async with self._service_lock(service_id):
            status = await self._probe_status(service)
            self._health_failures.pop(service_id, None)
            return self._set_status(
                service_id, status["state"], status["error"], "script"
            )

    async def check_service_status(self, service_id: str) -> dict[str, Any]:
        self._require_idle()
        service = self._require_service(service_id)
        self._operation_pending = True
        self._busy_services.add(service_id)
        try:
            return await self.refresh_status(service)
        finally:
            self._busy_services.discard(service_id)
            self._operation_pending = False

    def list_services(self) -> list[dict[str, Any]]:
        result = []
        for item in self.database.list_registered_services():
            enriched = dict(item)
            enriched["status"] = self.statuses.get(
                item["id"], {
                    "state": "unknown", "checked_at": None, "error": None,
                    "source": "stored",
                }
            )
            enriched["desired_state"] = item.get("desired_state", "unknown")
            enriched["wsl_portproxy_error"] = self.portproxy_errors.get(item["id"])
            enriched["busy"] = item["id"] in self._busy_services
            enriched["operation_pending"] = self._operation_pending
            result.append(enriched)
        return result

    async def create_service(self, payload: dict[str, Any], username: str, source_ip: str) -> dict[str, Any]:
        self._require_idle()
        item = {"id": uuid.uuid4().hex, **validate_service_input(payload, self.runner)}
        try:
            self.database.create_registered_service(item)
        except DatabaseError as exc:
            raise RegistryError(409, "service_conflict", str(exc)) from exc
        self.database.append_audit(source_ip, "management.service.create", "success",
                                   {"service_id": item["id"], "name": item["name"],
                                    "requested_by": username})
        self.statuses[item["id"]] = self._stored_status(
            self.database.get_registered_service(item["id"]) or item
        )
        self._reload_health_authorities()
        return next(service for service in self.list_services() if service["id"] == item["id"])

    async def update_service(self, service_id: str, payload: dict[str, Any],
                             username: str, source_ip: str) -> dict[str, Any]:
        self._require_idle()
        self._docker_handoff_lock.acquire()
        self._operation_pending = True
        removed = False
        try:
            current = self._require_service(service_id)
            item = validate_service_input({**current, **payload}, self.runner)
            mapping_changed = self._portproxy_identity(current) != self._portproxy_identity(item)
            if mapping_changed:
                removed = await self._remove_owned_portproxy(current)
            try:
                updated = self.database.update_registered_service(
                    service_id, item, source_ip,
                    {"service_id": service_id, "name": item["name"],
                     "requested_by": username},
                )
            except DatabaseError as exc:
                restore_error = await self._restore_owned_portproxy(current) if removed else None
                message = str(exc)
                if restore_error:
                    message = f"{message}; 旧端口转发恢复失败: {restore_error}"
                raise RegistryError(409, "service_conflict", message) from exc
            if not updated:
                restore_error = await self._restore_owned_portproxy(current) if removed else None
                message = "已登记服务不存在"
                if restore_error:
                    message = f"{message}; 旧端口转发恢复失败: {restore_error}"
                raise RegistryError(404, "service_not_found", message)
            stored = self.database.get_registered_service(service_id)
            if stored is None:
                raise RegistryError(404, "service_not_found", "已登记服务不存在")
            self.statuses[service_id] = self._stored_status(stored)
            self._health_failures.pop(service_id, None)
            self._reload_health_authorities()
            if mapping_changed:
                await self._sync_all_portproxies()
            return next(value for value in self.list_services() if value["id"] == service_id)
        finally:
            self._operation_pending = False
            self._docker_handoff_lock.release()

    async def delete_service(self, service_id: str, username: str, source_ip: str) -> None:
        self._require_idle()
        self._docker_handoff_lock.acquire()
        self._operation_pending = True
        removed = False
        try:
            service = self._require_service(service_id)
            if service_id in self._busy_services:
                raise RegistryError(409, "service_busy", "服务操作正在执行")
            removed = await self._remove_owned_portproxy(service)
            try:
                deleted = self.database.delete_registered_service(
                    service_id, source_ip,
                    {"service_id": service_id, "name": service["name"],
                     "requested_by": username},
                )
            except DatabaseError as exc:
                restore_error = await self._restore_owned_portproxy(service) if removed else None
                message = str(exc)
                if restore_error:
                    message = f"{message}; 旧端口转发恢复失败: {restore_error}"
                raise RegistryError(409, "service_conflict", message) from exc
            if not deleted:
                restore_error = await self._restore_owned_portproxy(service) if removed else None
                message = "已登记服务不存在"
                if restore_error:
                    message = f"{message}; 旧端口转发恢复失败: {restore_error}"
                raise RegistryError(404, "service_not_found", message)
            self.statuses.pop(service_id, None)
            self._health_failures.pop(service_id, None)
            self._health_authorities.pop(service_id, None)
            self._registered_ports.pop(service_id, None)
            self._service_locks.pop(service_id, None)
            self.portproxy_errors.pop(service_id, None)
        finally:
            self._operation_pending = False
            self._docker_handoff_lock.release()

    def create_scene(self, payload: dict[str, Any], username: str, source_ip: str) -> dict[str, Any]:
        self._require_idle()
        item = {"id": uuid.uuid4().hex, **validate_scene_input(payload, self.database)}
        try:
            self.database.create_scene(item)
        except DatabaseError as exc:
            raise RegistryError(409, "scene_conflict", str(exc)) from exc
        self.database.append_audit(source_ip, "management.scene.create", "success",
                                   {"scene_id": item["id"], "name": item["name"],
                                    "requested_by": username})
        return self._scene_with_state(self.database.get_scene(item["id"]))

    def update_scene(self, scene_id: str, payload: dict[str, Any],
                     username: str, source_ip: str) -> dict[str, Any]:
        self._require_idle()
        self._require_scene(scene_id)
        item = validate_scene_input(payload, self.database)
        try:
            updated = self.database.update_scene(scene_id, item)
        except DatabaseError as exc:
            raise RegistryError(409, "scene_conflict", str(exc)) from exc
        if not updated:
            raise RegistryError(404, "scene_not_found", "场景不存在")
        self.database.append_audit(source_ip, "management.scene.update", "success",
                                   {"scene_id": scene_id, "name": item["name"],
                                    "requested_by": username})
        return self._scene_with_state(self.database.get_scene(scene_id))

    def delete_scene(self, scene_id: str, username: str, source_ip: str) -> None:
        self._require_idle()
        scene = self._require_scene(scene_id)
        if not self.database.delete_scene(scene_id):
            raise RegistryError(404, "scene_not_found", "场景不存在")
        self.database.append_audit(source_ip, "management.scene.delete", "success",
                                   {"scene_id": scene_id, "name": scene["name"],
                                    "requested_by": username})

    def set_default_scene(self, scene_id: str, enabled: bool,
                          username: str, source_ip: str) -> dict[str, Any]:
        self._require_idle()
        scene = self._require_scene(scene_id)
        if not self.database.set_default_scene(scene_id, enabled):
            raise RegistryError(404, "scene_not_found", "场景不存在")
        self.database.append_audit(
            source_ip,
            "management.scene.default.set" if enabled else "management.scene.default.clear",
            "success",
            {"scene_id": scene_id, "name": scene["name"], "requested_by": username},
        )
        return self._scene_with_state(self.database.get_scene(scene_id))

    def reorder_scenes(self, scene_ids: list[str], username: str, source_ip: str) -> list[dict[str, Any]]:
        self._require_idle()
        known = {scene["id"] for scene in self.database.list_scenes()}
        if len(scene_ids) != len(set(scene_ids)) or any(
            ID_RE.fullmatch(scene_id) is None for scene_id in scene_ids
        ) or set(scene_ids) != known:
            raise RegistryError(422, "invalid_scene_order", "场景排序必须包含全部现有场景且不能重复")
        self.database.reorder_scenes(scene_ids, username, source_ip)
        return self.list_scenes()

    def list_scenes(self) -> list[dict[str, Any]]:
        return [self._scene_with_state(item) for item in self.database.list_scenes()]

    def active_scene(self) -> dict[str, Any] | None:
        scene = self.database.get_last_activated_scene()
        if scene is None or self._scene_with_state(scene)["state"] != "active":
            return None
        return scene

    def _scene_with_state(self, scene: dict[str, Any] | None) -> dict[str, Any]:
        if scene is None:
            raise RegistryError(404, "scene_not_found", "场景不存在")
        target = set(scene["service_ids"])
        services = self.database.list_registered_services()
        matches = True
        target_running = False
        for service in services:
            state = self.statuses.get(service["id"], {}).get("state", "unknown")
            if service["id"] in target:
                matches = matches and state == "running"
                target_running = target_running or state == "running"
            else:
                matches = matches and state != "running"
        item = dict(scene)
        service_map = {service["id"]: service for service in services}
        item["services"] = [
            {
                "id": service_id,
                "name": service_map[service_id]["name"],
                "ui_url": service_map[service_id]["ui_url"],
                "status": self.statuses.get(
                    service_id, {
                        "state": "unknown", "checked_at": None, "error": None,
                        "source": "stored",
                    }
                ),
                "desired_state": service_map[service_id].get("desired_state", "unknown"),
                "busy": service_id in self._busy_services,
            }
            for service_id in scene["service_ids"]
            if service_id in service_map
        ]
        if matches:
            item["state"] = "active"
        elif target and not target_running:
            item["state"] = "inactive"
        else:
            item["state"] = "partial"
        item["busy"] = self._operation_pending
        return item

    def submit_service_action(self, service_id: str, action: str,
                              username: str, source_ip: str) -> str:
        if action not in SERVICE_ACTIONS:
            raise RegistryError(422, "invalid_action", "动作只支持 start/stop/restart")
        self._require_service(service_id)
        return self._submit("service", service_id, action, username, source_ip,
                            self._run_service_operation)

    def submit_scene_activation(
        self, scene_id: str, username: str, source_ip: str, *, video_job_id: str | None = None
    ) -> str:
        self._require_scene(scene_id)
        return self._submit("scene", scene_id, "activate", username, source_ip,
                            self._run_scene_operation, video_job_id=video_job_id)

    def submit_default_scene_activation(self) -> str | None:
        if self.database.resource_lease_owner("gpu:4090") is not None:
            return None
        scene = self.database.get_default_scene()
        if scene is None:
            return None
        return self.submit_scene_activation(scene["id"], "system", "startup")

    def submit_stop_all(self, username: str, source_ip: str) -> str:
        return self._submit("service_group", "all", "stop_all", username, source_ip,
                            self._run_stop_all_operation)

    def request_scene_cancel(self, operation_id: str, username: str,
                             source_ip: str) -> dict[str, str]:
        if ID_RE.fullmatch(operation_id) is None:
            raise RegistryError(404, "operation_not_found", "操作记录不存在")
        cancel_event = self._cancel_requests.get(operation_id)
        if cancel_event is None:
            operation = self.database.get_operation(operation_id)
            if operation is None:
                raise RegistryError(404, "operation_not_found", "操作记录不存在")
            if operation["kind"] != "scene":
                raise RegistryError(409, "operation_not_cancellable", "只能终止场景切换")
            raise RegistryError(409, "operation_finished", "场景切换已经结束")
        result = self.database.request_scene_operation_cancel(
            operation_id, username, source_ip
        )
        if result == "missing":
            raise RegistryError(404, "operation_not_found", "操作记录不存在")
        if result == "not_scene":
            raise RegistryError(409, "operation_not_cancellable", "只能终止场景切换")
        if result == "finished":
            raise RegistryError(409, "operation_finished", "场景切换已经结束")
        cancel_event.set()
        return {"operation_id": operation_id, "status": "cancellation_requested"}

    def _submit(self, kind: str, target_id: str, action: str, username: str,
                source_ip: str, worker: Any, *, video_job_id: str | None = None) -> str:
        lease_owner = self.database.resource_lease_owner("gpu:4090")
        if lease_owner is not None and lease_owner != video_job_id:
            raise RegistryError(
                409, "gpu_4090_leased", "RTX 4090 正由视频任务独占，不能执行服务或场景操作"
            )
        if self._operation_pending or self.database.has_active_operation():
            raise RegistryError(409, "operation_busy", "已有服务或场景操作正在执行")
        operation_id = uuid.uuid4().hex
        self._docker_handoff_lock.acquire()
        self._operation_pending = True
        cancel_event = asyncio.Event()
        self._cancel_requests[operation_id] = cancel_event
        try:
            self.database.create_operation(operation_id, kind, target_id, action, username, source_ip)
        except OperationBusyError as exc:
            self._operation_pending = False
            self._cancel_requests.pop(operation_id, None)
            self._docker_handoff_lock.release()
            raise RegistryError(409, "operation_busy", str(exc)) from exc
        except Exception:
            self._operation_pending = False
            self._cancel_requests.pop(operation_id, None)
            self._docker_handoff_lock.release()
            raise
        task = asyncio.create_task(
            self._guard_operation(worker, operation_id, target_id, action)
        )
        self._operation_tasks.add(task)
        task.add_done_callback(self._operation_tasks.discard)
        return operation_id

    async def _guard_operation(self, worker: Any, operation_id: str,
                               target_id: str, action: str) -> None:
        release_pending = True
        try:
            await worker(operation_id, target_id, action)
            self.last_operation_error = None
        except Exception as exc:
            operation_error = f"{type(exc).__name__}: {exc}"
            try:
                self.database.finish_operation_with_audit(
                    operation_id, "failed", "failure", None, "unknown",
                    operation_error,
                )
            except DatabaseError as database_error:
                self.last_operation_error = (
                    f"{operation_error}; 无法持久化操作终态: {database_error}"
                )
                release_pending = False
            else:
                self.last_operation_error = None
        finally:
            self._cancel_requests.pop(operation_id, None)
            if release_pending:
                self._operation_pending = False
                self._docker_handoff_lock.release()

    async def _run_script_action(self, operation_id: str, sequence: int,
                                 phase: str, service: dict[str, Any], action: str) -> bool:
        service_id = service["id"]
        async with self._service_lock(service_id):
            before = self.statuses.get(service_id, {}).get("state", "unknown")
            self.database.create_operation_step(operation_id, sequence, phase, service_id,
                                                action, before_state=before)
            self._busy_services.add(service_id)
            try:
                expected = "stopped" if action == "stop" else "running"
                self._set_desired_state(service_id, expected)
                if action in {"start", "restart"}:
                    await self._sync_all_portproxies()
                    if service_id in self.portproxy_errors:
                        raise RegistryError(
                            503, "portproxy_sync_failed", self.portproxy_errors[service_id]
                        )
                result = await asyncio.to_thread(self.runner.run, service["script_path"], action)
                if result.returncode != 0:
                    error = result.stderr or result.stdout or f"脚本退出码 {result.returncode}"
                else:
                    error = None
                if service.get("health_url"):
                    verification_service = {**service, "desired_state": expected}
                    health_result = await self._probe_health(verification_service)
                    status = self._record_health_result(
                        verification_service, health_result, immediate=True
                    )
                    health_ok = status["state"] == expected
                    if result.returncode == 0 and not health_ok:
                        error = status.get("error") or (
                            f"脚本执行成功，但健康检查状态为 {status['state']}"
                        )
                else:
                    status = self._set_status(
                        service_id, expected if result.returncode == 0 else "unknown",
                        error, "action",
                    )
                    health_ok = status["state"] == expected
                success = result.returncode == 0 and health_ok
                if not success:
                    status, reconciliation_error = await self._reconcile_failed_action_status(
                        service, error or "服务未达到目标状态"
                    )
                    if reconciliation_error:
                        error = f"{error or '服务未达到目标状态'}; {reconciliation_error}"
                self.database.finish_operation_step(
                    operation_id, sequence, "succeeded" if success else "failed",
                    status["state"], "success" if success else "failure", error,
                )
                return success
            except RegistryError as exc:
                status, reconciliation_error = await self._reconcile_failed_action_status(
                    service, exc.message
                )
                error = exc.message
                if reconciliation_error:
                    error = f"{error}; {reconciliation_error}"
                self.database.finish_operation_step(
                    operation_id, sequence, "failed", status["state"], "failure", error
                )
                return False
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                status, reconciliation_error = await self._reconcile_failed_action_status(
                    service, message
                )
                if reconciliation_error:
                    message = f"{message}; {reconciliation_error}"
                self.database.finish_operation_step(
                    operation_id, sequence, "failed", status["state"], "failure", message
                )
                return False
            finally:
                self._busy_services.discard(service_id)

    async def _run_service_operation(self, operation_id: str, service_id: str, action: str) -> None:
        before = self.statuses.get(service_id, {}).get("state", "unknown")
        self.database.update_operation(operation_id, status="running", started_at=utc_now(),
                                       before_state=before)
        service = self._require_service(service_id)
        success = await self._run_script_action(operation_id, 1, "service", service, action)
        after = self.statuses.get(service_id, {}).get("state", "unknown")
        self.database.finish_operation_with_audit(
            operation_id, "succeeded" if success else "failed",
            "success" if success else "failure", before, after,
            None if success else "服务脚本执行失败",
        )

    async def _run_stop_all_operation(self, operation_id: str, _: str, __: str) -> None:
        await self.refresh_all_health(immediate=True)
        before = {key: value.get("state", "unknown") for key, value in self.statuses.items()}
        services = self.database.list_registered_services()
        targets = [
            service for service in services
            if self.statuses.get(service["id"], {}).get("state") != "stopped"
        ]
        self.database.update_operation(
            operation_id, status="running", started_at=utc_now(),
            before_state=str(before), total_steps=len(targets),
        )
        success = True
        for sequence, service in enumerate(targets, start=1):
            success = await self._run_script_action(
                operation_id, sequence, "stop_all", service, "stop"
            ) and success
        all_stopped = all(
            self.statuses.get(service["id"], {}).get("state") == "stopped"
            for service in services
        )
        success = success and all_stopped
        self.database.finish_operation_with_audit(
            operation_id, "succeeded" if success else "failed",
            "success" if success else "partial", str(before),
            "stopped" if all_stopped else "partial",
            None if success else "部分服务未能停止",
        )

    async def _run_scene_operation(self, operation_id: str, scene_id: str, _: str) -> None:
        scene = self._require_scene(scene_id)
        await self.refresh_all_health()
        before = {key: value.get("state", "unknown") for key, value in self.statuses.items()}
        target_ids = list(scene["service_ids"])
        target = set(target_ids)
        services = {item["id"]: item for item in self.database.list_registered_services()}
        stop_targets = [
            service for service in services.values()
            if service["id"] not in target
            and self.statuses.get(service["id"], {}).get("state") != "stopped"
        ]
        start_targets = [
            services[service_id] for service_id in target_ids
            if self.statuses.get(service_id, {}).get("state") != "running"
        ]
        self.database.update_operation(
            operation_id, status="running", started_at=utc_now(),
            before_state=str(before), total_steps=len(stop_targets) + len(start_targets),
        )
        sequence = 0
        stop_ok = True
        cancelled = False
        cancel_event = self._cancel_requests[operation_id]
        for service in services.values():
            if cancel_event.is_set():
                cancelled = True
                break
            if service["id"] not in target and self.statuses.get(
                service["id"], {}
            ).get("state") != "stopped":
                sequence += 1
                stop_ok = await self._run_script_action(
                    operation_id, sequence, "stop_unselected", service, "stop"
                ) and stop_ok
                if cancel_event.is_set():
                    cancelled = True
                    break
        start_ok = True
        if stop_ok and not cancelled:
            for service_id in target_ids:
                if cancel_event.is_set():
                    cancelled = True
                    break
                if self.statuses.get(service_id, {}).get("state") == "running":
                    continue
                sequence += 1
                step_ok = await self._run_script_action(
                    operation_id, sequence, "start_selected", services[service_id], "start"
                )
                start_ok = step_ok and start_ok
                if cancel_event.is_set():
                    cancelled = True
                    break
                if not step_ok:
                    break
        final_scene = self._scene_with_state(scene)
        if cancel_event.is_set():
            cancelled = True
        if cancelled:
            self.database.finish_operation_with_audit(
                operation_id, "interrupted", "cancelled", str(before), final_scene["state"],
                "用户终止了场景切换；已完成的服务动作不会自动回滚",
            )
            return
        success = stop_ok and start_ok and final_scene["state"] == "active"
        if success:
            self.database.set_last_activated_scene(scene_id)
        result = "success" if success else ("stop_failed" if not stop_ok else "partial")
        self.database.finish_operation_with_audit(
            operation_id, "succeeded" if success else "failed", result,
            str(before), final_scene["state"],
            None if success else "场景切换未达到全部目标状态",
        )

    def _require_service(self, service_id: str) -> dict[str, Any]:
        if ID_RE.fullmatch(service_id) is None:
            raise RegistryError(404, "service_not_found", "已登记服务不存在")
        item = self.database.get_registered_service(service_id)
        if item is None:
            raise RegistryError(404, "service_not_found", "已登记服务不存在")
        return item

    def _require_idle(self) -> None:
        if self.database.resource_lease_owner("gpu:4090") is not None:
            raise RegistryError(
                409, "gpu_4090_leased", "RTX 4090 正由视频任务独占，不能修改服务或场景"
            )
        if self._operation_pending or self.database.has_active_operation():
            raise RegistryError(409, "operation_busy", "已有服务或场景操作正在执行")

    def _require_scene(self, scene_id: str) -> dict[str, Any]:
        if ID_RE.fullmatch(scene_id) is None:
            raise RegistryError(404, "scene_not_found", "场景不存在")
        item = self.database.get_scene(scene_id)
        if item is None:
            raise RegistryError(404, "scene_not_found", "场景不存在")
        return item
