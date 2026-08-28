from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import psutil

from .config import Settings


@dataclass(frozen=True)
class CollectorError:
    collector: str
    error_type: str
    message: str
    cause: str

    def as_dict(self) -> dict[str, str]:
        return {
            "collector": self.collector,
            "error_type": self.error_type,
            "message": self.message,
            "cause": self.cause,
        }


class CommandError(RuntimeError):
    """Raised when a read-only external collector command fails."""


CommandRunner = Callable[[list[str], float], str]


def run_readonly_command(command: list[str], timeout: float) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        raise FileNotFoundError(f"未找到命令: {command[0]}")
    completed = subprocess.run(
        [executable, *command[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "命令未提供错误输出"
        raise CommandError(f"{command[0]} 返回退出码 {completed.returncode}: {stderr}")
    return completed.stdout


def _number(value: str, kind: type[int] | type[float]) -> int | float | None:
    cleaned = value.strip()
    if not cleaned or cleaned.upper() in {"N/A", "[N/A]", "NOT SUPPORTED"}:
        return None
    try:
        return kind(float(cleaned)) if kind is int else kind(cleaned)
    except ValueError as exc:
        raise ValueError(f"无法将 {value!r} 解析为 {kind.__name__}") from exc


def collect_gpus(settings: Settings, runner: CommandRunner = run_readonly_command) -> list[dict[str, Any]]:
    fields = (
        "index,uuid,name,utilization.gpu,memory.used,memory.total,"
        "temperature.gpu,power.draw,clocks.current.graphics"
    )
    output = runner(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
        settings.command_timeout_seconds,
    )
    gpus: list[dict[str, Any]] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        columns = [column.strip() for column in line.split(",")]
        if len(columns) != 9:
            raise ValueError(f"nvidia-smi 第 {line_number} 行应有 9 列，实际为 {len(columns)}")
        memory_used = _number(columns[4], int)
        memory_total = _number(columns[5], int)
        memory_percent = None
        if memory_used is not None and memory_total:
            memory_percent = round(memory_used / memory_total * 100, 2)
        gpus.append(
            {
                "index": _number(columns[0], int),
                "uuid": columns[1],
                "name": columns[2],
                "load_percent": _number(columns[3], float),
                "memory_used_mib": memory_used,
                "memory_total_mib": memory_total,
                "memory_percent": memory_percent,
                "temperature_c": _number(columns[6], float),
                "power_w": _number(columns[7], float),
                "graphics_clock_mhz": _number(columns[8], float),
            }
        )
    return gpus


def collect_docker(settings: Settings, runner: CommandRunner = run_readonly_command) -> list[dict[str, Any]]:
    output = runner(
        ["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"],
        settings.command_timeout_seconds,
    )
    containers: list[dict[str, Any]] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"docker ps 第 {line_number} 行不是有效 JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"docker ps 第 {line_number} 行的 JSON 根节点必须是对象")
        containers.append(
            {
                "id": item.get("ID"),
                "name": item.get("Names"),
                "image": item.get("Image"),
                "state": item.get("State"),
                "status": item.get("Status"),
                "ports": item.get("Ports"),
            }
        )
    return containers


def collect_host() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    cpu_temperature = None
    temperature_status = "unsupported"
    sensors_temperatures = getattr(psutil, "sensors_temperatures", None)
    if sensors_temperatures is not None:
        readings = sensors_temperatures()
        cpu_sensor_groups = {"coretemp", "k10temp", "cpu_thermal", "zenpower"}
        candidates = [
            entry.current
            for group, entries in readings.items()
            if group.lower() in cpu_sensor_groups
            for entry in entries
            if entry.current is not None
        ]
        if candidates:
            cpu_temperature = max(candidates)
            temperature_status = "available"

    disks: list[dict[str, Any]] = []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (OSError, PermissionError) as exc:
            disks.append(
                {
                    "device": partition.device,
                    "mountpoint": partition.mountpoint,
                    "error": {
                        "error_type": type(exc).__name__,
                        "message": "无法读取磁盘空间",
                        "cause": str(exc),
                    },
                }
            )
            continue
        disks.append(
            {
                "device": partition.device,
                "mountpoint": partition.mountpoint,
                "filesystem": partition.fstype,
                "used_bytes": usage.used,
                "total_bytes": usage.total,
                "percent": usage.percent,
            }
        )
    return {
        "cpu": {
            "load_percent": psutil.cpu_percent(interval=None),
            "temperature_c": cpu_temperature,
            "temperature_status": temperature_status,
        },
        "memory": {
            "used_bytes": memory.used,
            "total_bytes": memory.total,
            "percent": memory.percent,
        },
        "disks": disks,
    }


def collect_ports(settings: Settings) -> list[dict[str, Any]]:
    listeners: dict[int, list[dict[str, Any]]] = {port: [] for port in settings.critical_ports}
    for connection in psutil.net_connections(kind="inet"):
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        port = connection.laddr.port
        if port in listeners:
            listeners[port].append(
                {
                    "address": connection.laddr.ip,
                    "pid": connection.pid,
                }
            )
    return [
        {"port": port, "listening": bool(entries), "listeners": entries}
        for port, entries in listeners.items()
    ]


def _safe_collect(
    collector: str, function: Callable[[], Any], errors: list[dict[str, str]], fallback: Any
) -> Any:
    try:
        return function()
    except Exception as exc:
        errors.append(
            CollectorError(
                collector=collector,
                error_type=type(exc).__name__,
                message=f"{collector} 采集失败",
                cause=str(exc),
            ).as_dict()
        )
        return fallback


def collect_snapshot(settings: Settings, runner: CommandRunner = run_readonly_command) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    host = _safe_collect("host", collect_host, errors, {})
    gpus = _safe_collect("nvidia", lambda: collect_gpus(settings, runner), errors, [])
    docker = _safe_collect("docker", lambda: collect_docker(settings, runner), errors, [])
    ports = _safe_collect("ports", lambda: collect_ports(settings), errors, [])
    return {
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "host": host,
        "gpus": gpus,
        "docker": {"containers": docker},
        "ports": ports,
        "collector_errors": errors,
    }
