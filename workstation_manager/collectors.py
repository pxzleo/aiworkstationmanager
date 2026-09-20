from __future__ import annotations

import json
import math
import os
import ctypes
import csv
import io
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import psutil

from .config import Settings
from .power_model import PowerModel, estimate as estimate_power


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


_rate_lock = threading.Lock()
_rate_state: dict[str, tuple[float, dict[str, float]]] = {}
_slow_cache_lock = threading.Lock()
_slow_cache: dict[str, tuple[float, bool, Any]] = {}

# RAPL 能量累积计数器的差分状态。瞬时功率读数只覆盖采样瞬间，
# 3 秒一次的采样会漏掉中间的功耗尖峰；能量差分给出的是整个采样区间的真实平均值。
_energy_lock = threading.Lock()
_energy_state: dict[str, tuple[str, float, float]] = {}
_energy_scale: dict[str, float] = {}

# 原始能量计数器的单位由 Windows 决定且未公开，因此用同一份数据的瞬时功率读数
# 在运行时标定"每焦耳多少个原始计数"，不硬编码任何平台相关的常数。
_ENERGY_SCALE_MIN = 1e2
_ENERGY_SCALE_MAX = 1e15
_ENERGY_SCALE_SMOOTHING = 0.25
_ENERGY_MIN_ELAPSED_SECONDS = 0.5
_ENERGY_MAX_ELAPSED_SECONDS = 120.0
_MAX_PLAUSIBLE_SENSOR_W = 2000.0

_CPU_PACKAGE_HARDWARE_TYPES = {"cpupackage", "cpu"}
_CPU_PACKAGE_MARKERS = ("rapl", "package", "processor", "cpu")


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


def _rates(key: str, counters: dict[str, float]) -> dict[str, float | None]:
    now = time.monotonic()
    with _rate_lock:
        previous = _rate_state.get(key)
        _rate_state[key] = (now, counters)
    if previous is None or now <= previous[0]:
        return {name: None for name in counters}
    elapsed = now - previous[0]
    return {
        name: max(0.0, (value - previous[1].get(name, value)) / elapsed)
        for name, value in counters.items()
    }


def _cached(key: str, seconds: float, loader: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _slow_cache_lock:
        cached = _slow_cache.get(key)
        if cached is not None and now - cached[0] < seconds:
            if cached[1]:
                return cached[2]
            raise cached[2]
    try:
        value = loader()
    except Exception as exc:
        with _slow_cache_lock:
            _slow_cache[key] = (time.monotonic(), False, exc)
        raise
    with _slow_cache_lock:
        _slow_cache[key] = (time.monotonic(), True, value)
    return value


def _windows_commit_memory() -> dict[str, int | None]:
    if os.name != "nt":
        return {"commit_used_bytes": None, "commit_limit_bytes": None}

    class PerformanceInformation(ctypes.Structure):
        _fields_ = [
            ("size", ctypes.c_ulong), ("commit_total", ctypes.c_size_t),
            ("commit_limit", ctypes.c_size_t), ("commit_peak", ctypes.c_size_t),
            ("physical_total", ctypes.c_size_t), ("physical_available", ctypes.c_size_t),
            ("system_cache", ctypes.c_size_t), ("kernel_total", ctypes.c_size_t),
            ("kernel_paged", ctypes.c_size_t), ("kernel_nonpaged", ctypes.c_size_t),
            ("page_size", ctypes.c_size_t), ("handle_count", ctypes.c_ulong),
            ("process_count", ctypes.c_ulong), ("thread_count", ctypes.c_ulong),
        ]

    information = PerformanceInformation()
    information.size = ctypes.sizeof(information)
    if not ctypes.windll.psapi.GetPerformanceInfo(
        ctypes.byref(information), information.size
    ):
        raise OSError("GetPerformanceInfo 无法读取系统提交内存")
    return {
        "commit_used_bytes": int(information.commit_total * information.page_size),
        "commit_limit_bytes": int(information.commit_limit * information.page_size),
    }


def collect_gpus(settings: Settings, runner: CommandRunner = run_readonly_command) -> list[dict[str, Any]]:
    fields = (
        "index,uuid,name,utilization.gpu,memory.used,memory.total,"
        "temperature.gpu,power.draw,clocks.current.graphics,fan.speed,pstate,"
        "utilization.memory,utilization.encoder,utilization.decoder,"
        "pcie.link.gen.current,pcie.link.width.current,clocks_event_reasons.active"
    )
    output = runner(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
        settings.command_timeout_seconds,
    )
    gpus: list[dict[str, Any]] = []
    for line_number, columns in enumerate(
        csv.reader(io.StringIO(output), skipinitialspace=True), start=1
    ):
        if not columns or not any(column.strip() for column in columns):
            continue
        columns = [column.strip() for column in columns]
        if len(columns) != 17:
            raise ValueError(f"nvidia-smi 第 {line_number} 行应有 17 列，实际为 {len(columns)}")
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
                "fan_percent": _number(columns[9], float),
                "performance_state": columns[10] or None,
                "memory_utilization_percent": _number(columns[11], float),
                "encoder_percent": _number(columns[12], float),
                "decoder_percent": _number(columns[13], float),
                "pcie_generation": _number(columns[14], int),
                "pcie_width": _number(columns[15], int),
                "clock_event_reasons": columns[16] or None,
                "processes": [],
            }
        )
    return gpus


def collect_gpu_processes(
    settings: Settings, runner: CommandRunner = run_readonly_command
) -> list[dict[str, Any]]:
    output = runner(
        ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
         "--format=csv,noheader,nounits"],
        settings.command_timeout_seconds,
    )
    processes: list[dict[str, Any]] = []
    for line_number, columns in enumerate(
        csv.reader(io.StringIO(output), skipinitialspace=True), start=1
    ):
        if not columns or not any(column.strip() for column in columns):
            continue
        columns = [column.strip() for column in columns]
        if len(columns) != 4:
            raise ValueError(
                f"nvidia-smi 进程查询第 {line_number} 行应有 4 列，实际为 {len(columns)}"
            )
        pid = _number(columns[1], int)
        process_name = columns[2]
        if pid is not None and process_name == "[Insufficient Permissions]":
            try:
                process_name = psutil.Process(pid).exe() or psutil.Process(pid).name()
            except (psutil.Error, OSError):
                process_name = f"PID {pid}（权限不足）"
        processes.append({
            "gpu_uuid": columns[0],
            "pid": pid,
            "name": process_name,
            "memory_used_mib": _number(columns[3], int),
        })
    return processes


def collect_host_power_sensors(
    settings: Settings, runner: CommandRunner = run_readonly_command
) -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    script = r"""
$items = @()
$rawByName = @{}
Get-CimInstance -ClassName Win32_PerfRawData_PowerMeterCounter_EnergyMeter -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match '^RAPL_Package\d+_PKG$' } |
  ForEach-Object {
    $rawByName[[string]$_.Name] = [pscustomobject]@{
      energy_raw = [double]$_.Energy
      energy_timestamp = [double]$_.Timestamp_PerfTime
      energy_frequency = [double]$_.Frequency_PerfTime
    }
  }
Get-CimInstance -ClassName Win32_PerfFormattedData_PowerMeterCounter_EnergyMeter -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match '^RAPL_Package\d+_PKG$' -and $null -ne $_.Power } |
  ForEach-Object {
    $name = [string]$_.Name
    $raw = $rawByName[$name]
    $items += [pscustomobject]@{
      source = 'root\CIMV2:Win32_PerfFormattedData_PowerMeterCounter_EnergyMeter'
      name = $name
      hardware_name = $name
      hardware_type = 'CpuPackage'
      value_w = [double]$_.Power / 1000.0
      energy_raw = $(if ($raw) { $raw.energy_raw } else { $null })
      energy_timestamp = $(if ($raw) { $raw.energy_timestamp } else { $null })
      energy_frequency = $(if ($raw) { $raw.energy_frequency } else { $null })
    }
  }
$hasCpuPackagePower = @($items | Where-Object { $_.hardware_type -eq 'CpuPackage' }).Count -gt 0
foreach ($namespace in @('root\LibreHardwareMonitor', 'root\OpenHardwareMonitor')) {
  if (-not (Get-CimClass -Namespace $namespace -ClassName Sensor -ErrorAction SilentlyContinue)) {
    continue
  }
  Get-CimInstance -Namespace $namespace -ClassName Sensor -ErrorAction SilentlyContinue |
    Where-Object { $_.SensorType -eq 'Power' -and $null -ne $_.Value } |
    ForEach-Object {
      $hardwareName = [string]($_.HardwareName)
      $hardwareType = [string]($_.HardwareType)
      $sensorName = [string]($_.Name)
      $identity = $hardwareName + ' ' + $hardwareType + ' ' + $sensorName
      if ($identity -notmatch '(?i)nvidia|geforce|rtx|gpu' -and -not ($hasCpuPackagePower -and $identity -match '(?i)cpu|processor|package|core|rapl')) {
        $items += [pscustomobject]@{
          source = $namespace
          name = $sensorName
          hardware_name = $hardwareName
          hardware_type = $hardwareType
          value_w = [double]$_.Value
        }
      }
    }
}
$items | ConvertTo-Json -Compress
"""
    output = runner(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        settings.command_timeout_seconds,
    ).strip()
    if not output:
        return []
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"功耗传感器输出不是有效 JSON: {exc}") from exc
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError("功耗传感器输出必须是数组")
    sensors: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        identity = " ".join(
            str(item.get(field) or "")
            for field in ("name", "hardware_name", "hardware_type")
        ).lower()
        if any(marker in identity for marker in ("nvidia", "geforce", "rtx", "gpu")):
            continue
        value = item.get("value_w")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            continue
        instant_w = float(value)
        name = str(item.get("name") or "")
        watts, method = _energy_derived_watts(name, item, instant_w)
        if watts <= 0:
            continue
        sensors.append({
            "source": item.get("source"),
            "name": item.get("name"),
            "hardware_name": item.get("hardware_name"),
            "hardware_type": item.get("hardware_type"),
            "role": _sensor_role(identity, item.get("hardware_type")),
            "value_w": round(watts, 2),
            "instant_w": round(instant_w, 2),
            "method": method,
        })
    return sensors


def _sensor_role(identity: str, hardware_type: Any) -> str:
    """区分 CPU 封装功耗和其他功耗传感器。

    CPU 封装读数要额外补一层供电 VRM 的转换损耗，其他传感器（主板、电源等）
    读到的已经是转换之后的功率，不能重复补偿。
    """
    normalized = str(hardware_type or "").strip().lower()
    if normalized in _CPU_PACKAGE_HARDWARE_TYPES:
        return "cpu_package"
    if any(marker in identity for marker in _CPU_PACKAGE_MARKERS):
        return "cpu_package"
    return "other"


def _energy_derived_watts(
    name: str, item: dict[str, Any], instant_w: float
) -> tuple[float, str]:
    """用能量累积计数器的差分求采样区间的平均功率，拿不到就回退到瞬时读数。

    返回 (功率, 方法)，方法为 "energy_delta" 或 "instant"。
    """
    energy = _optional_float(item.get("energy_raw"))
    timestamp = _optional_float(item.get("energy_timestamp"))
    frequency = _optional_float(item.get("energy_frequency"))
    if not name or energy is None:
        return instant_w, "instant"
    if timestamp is not None and frequency is not None and frequency > 0 and timestamp > 0:
        clock, moment = "counter", timestamp / frequency
    else:
        clock, moment = "monotonic", time.monotonic()

    with _energy_lock:
        previous = _energy_state.get(name)
        _energy_state[name] = (clock, moment, energy)
        if previous is None or previous[0] != clock:
            return instant_w, "instant"
        elapsed = moment - previous[1]
        delta = energy - previous[2]
        if delta < 0 or not (
            _ENERGY_MIN_ELAPSED_SECONDS <= elapsed <= _ENERGY_MAX_ELAPSED_SECONDS
        ):
            # 计数器回绕、服务重启或采样间隔异常，之前标定的比例不再可信。
            _energy_scale.pop(name, None)
            return instant_w, "instant"
        rate = delta / elapsed
        scale = _energy_scale.get(name)
        if instant_w > 0 and rate > 0:
            candidate = rate / instant_w
            if _ENERGY_SCALE_MIN <= candidate <= _ENERGY_SCALE_MAX:
                scale = candidate if scale is None else (
                    scale * (1 - _ENERGY_SCALE_SMOOTHING)
                    + candidate * _ENERGY_SCALE_SMOOTHING
                )
                _energy_scale[name] = scale
        if scale is None or scale <= 0:
            return instant_w, "instant"
        watts = rate / scale

    if not math.isfinite(watts) or watts < 0 or watts > _MAX_PLAUSIBLE_SENSOR_W:
        return instant_w, "instant"
    return watts, "energy_delta"


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


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


def collect_docker_resources(
    settings: Settings, runner: CommandRunner = run_readonly_command
) -> dict[str, dict[str, Any]]:
    output = runner(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        settings.command_timeout_seconds,
    )
    resources: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"docker stats 第 {line_number} 行不是有效 JSON: {exc}") from exc
        name = str(item.get("Name") or "").strip()
        if name:
            resources[name] = {
                "cpu_percent": item.get("CPUPerc"),
                "memory_usage": item.get("MemUsage"),
                "network_io": item.get("NetIO"),
                "block_io": item.get("BlockIO"),
                "pids": item.get("PIDs"),
            }
    return resources


def collect_wsl_resources(
    settings: Settings, runner: CommandRunner = run_readonly_command
) -> dict[str, Any]:
    distro_output = runner(
        ["wsl.exe", "--list", "--quiet", "--running"],
        settings.command_timeout_seconds,
    ).replace("\x00", "")
    distros = [line.strip().lstrip("* ") for line in distro_output.splitlines() if line.strip()]
    if not distros:
        return {"distributions": [], "swap_used_bytes": 0, "swap_total_bytes": 0}
    memory_output = runner(
        ["wsl.exe", "--distribution", distros[0], "--exec", "cat", "/proc/meminfo"],
        settings.command_timeout_seconds,
    ).replace("\x00", "")
    values: dict[str, int] = {}
    for line in memory_output.splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        parts = raw_value.strip().split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024
    swap_total = values.get("SwapTotal")
    swap_free = values.get("SwapFree")
    return {
        "distributions": distros,
        "guest_memory_total_bytes": values.get("MemTotal"),
        "guest_memory_available_bytes": values.get("MemAvailable"),
        "swap_total_bytes": swap_total,
        "swap_used_bytes": None if swap_total is None or swap_free is None
        else max(0, swap_total - swap_free),
    }


def collect_host() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    commit = _windows_commit_memory()
    cpu_frequency = psutil.cpu_freq()
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
    disk_io: list[dict[str, Any]] = []
    for name, counters in (psutil.disk_io_counters(perdisk=True) or {}).items():
        values = {
            "read_bytes": float(counters.read_bytes),
            "write_bytes": float(counters.write_bytes),
            "read_count": float(counters.read_count),
            "write_count": float(counters.write_count),
            "read_time": float(counters.read_time),
            "write_time": float(counters.write_time),
        }
        rates = _rates(f"disk:{name}", values)
        operation_rate = (rates["read_count"] or 0) + (rates["write_count"] or 0)
        latency_ms = None if not operation_rate else (
            (rates["read_time"] or 0) + (rates["write_time"] or 0)
        ) / operation_rate
        disk_io.append({
            "name": name,
            "read_bytes_per_second": rates["read_bytes"],
            "write_bytes_per_second": rates["write_bytes"],
            "latency_ms": latency_ms,
        })

    networks: list[dict[str, Any]] = []
    interface_stats = psutil.net_if_stats()
    for name, counters in (psutil.net_io_counters(pernic=True) or {}).items():
        rates = _rates(f"network:{name}", {
            "received": float(counters.bytes_recv), "sent": float(counters.bytes_sent),
        })
        status = interface_stats.get(name)
        networks.append({
            "name": name,
            "is_up": bool(status and status.isup),
            "speed_mbps": status.speed if status and status.speed >= 0 else None,
            "received_bytes_per_second": rates["received"],
            "sent_bytes_per_second": rates["sent"],
            "errors": counters.errin + counters.errout,
            "drops": counters.dropin + counters.dropout,
        })
    physical_networks = [
        interface for interface in networks
        if interface["is_up"] and not any(
            marker in interface["name"].lower()
            for marker in ("vethernet", "loopback", "bluetooth", "蓝牙")
        )
    ]
    primary_network = max(
        physical_networks or [interface for interface in networks if interface["is_up"]],
        key=lambda interface: interface["speed_mbps"] or 0,
        default=None,
    )

    wsl_processes = []
    wsl_cpu_seconds = 0.0
    for process in psutil.process_iter(["pid", "name", "memory_info", "cpu_times"]):
        name = str(process.info.get("name") or "")
        if name.lower() not in {"vmmem", "vmmemwsl"}:
            continue
        memory_info = process.info.get("memory_info")
        cpu_times = process.info.get("cpu_times")
        memory_bytes = int(memory_info.rss) if memory_info is not None else None
        if cpu_times is not None:
            wsl_cpu_seconds += float(cpu_times.user + cpu_times.system)
        wsl_processes.append({"pid": process.info["pid"], "name": name, "memory_bytes": memory_bytes})
    wsl_cpu_rate = _rates("wsl:cpu", {"seconds": wsl_cpu_seconds})["seconds"]
    wsl_cpu_percent = None if wsl_cpu_rate is None else min(
        100.0, wsl_cpu_rate / max(1, psutil.cpu_count() or 1) * 100
    )
    return {
        "cpu": {
            "load_percent": psutil.cpu_percent(interval=None),
            "frequency_mhz": cpu_frequency.current if cpu_frequency else None,
            "temperature_c": cpu_temperature,
            "temperature_status": temperature_status,
        },
        "memory": {
            "used_bytes": memory.used,
            "available_bytes": memory.available,
            "total_bytes": memory.total,
            "percent": memory.percent,
            "commit_used_bytes": commit["commit_used_bytes"],
            "commit_limit_bytes": commit["commit_limit_bytes"],
            "swap_used_bytes": swap.used,
            "swap_total_bytes": swap.total,
        },
        "disks": disks,
        "disk_io": disk_io,
        "networks": networks,
        "primary_network": primary_network,
        "wsl": {
            "running": bool(wsl_processes),
            "memory_used_bytes": sum(
                process["memory_bytes"] or 0 for process in wsl_processes
            ) if wsl_processes else None,
            "cpu_percent": wsl_cpu_percent,
            "processes": wsl_processes,
        },
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


def summarize_power(
    gpus: list[dict[str, Any]], sensors: list[dict[str, Any]], model: PowerModel
) -> dict[str, Any]:
    """把 GPU 板卡功耗和主机功耗传感器汇总成实测/估算/合计三个口径。

    采样进程里用 enabled=False 的模型，只输出实测值；估算由持有配置和校准数据的
    Sampler 在主进程里补上，这样采样子进程不需要访问数据库。
    """
    gpu_values = [
        float(gpu["power_w"]) for gpu in gpus
        if isinstance(gpu.get("power_w"), (int, float))
        and not isinstance(gpu.get("power_w"), bool)
    ]
    cpu_values = [
        float(sensor["value_w"]) for sensor in sensors
        if sensor.get("role") == "cpu_package"
    ]
    other_values = [
        float(sensor["value_w"]) for sensor in sensors
        if sensor.get("role") != "cpu_package"
    ]
    summary = estimate_power(
        model,
        gpu_w=sum(gpu_values) if gpu_values else None,
        cpu_package_w=sum(cpu_values) if cpu_values else None,
        other_sensor_w=sum(other_values) if other_values else None,
    )
    summary["sensors"] = sensors
    return summary


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
    host_power_sensors = _safe_collect(
        "host_power",
        lambda: collect_host_power_sensors(settings, runner),
        errors,
        [],
    )
    if host:
        host["power"] = summarize_power(gpus, host_power_sensors, PowerModel(enabled=False))
    if runner is run_readonly_command:
        docker = _safe_collect(
            "docker",
            lambda: _cached("docker_inventory", 30, lambda: collect_docker(settings, runner)),
            errors,
            [],
        )
        gpu_processes = _safe_collect(
            "nvidia_processes",
            lambda: _cached(
                "nvidia_processes", 15, lambda: collect_gpu_processes(settings, runner)
            ),
            errors,
            [],
        )
        docker_resources = _safe_collect(
            "docker_resources",
            lambda: _cached(
                "docker_resources", 30, lambda: collect_docker_resources(settings, runner)
            ),
            errors,
            {},
        )
        if host.get("wsl", {}).get("running"):
            wsl_resources = _safe_collect(
                "wsl_resources",
                lambda: _cached(
                    "wsl_resources", 30, lambda: collect_wsl_resources(settings, runner)
                ),
                errors,
                {},
            )
            host["wsl"].update(wsl_resources)
    else:
        docker = _safe_collect("docker", lambda: collect_docker(settings, runner), errors, [])
        gpu_processes = []
        docker_resources = {}
    for gpu in gpus:
        gpu["processes"] = [
            process for process in gpu_processes if process["gpu_uuid"] == gpu.get("uuid")
        ]
    for container in docker:
        container["resources"] = docker_resources.get(str(container.get("name") or ""))
    ports = _safe_collect("ports", lambda: collect_ports(settings), errors, [])
    return {
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "host": host,
        "gpus": gpus,
        "docker": {"containers": docker},
        "ports": ports,
        "collector_errors": errors,
    }
