from __future__ import annotations

import asyncio
import copy
import ctypes
import multiprocessing
import os
import queue
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import Settings
from .collectors import collect_snapshot, summarize_power
from .power_model import PowerModel


MAX_HISTORY_WINDOW_MINUTES = 24 * 60


class SamplerStopError(RuntimeError):
    """Raised when an in-flight collector fails during shutdown."""


class CollectionWorkerError(RuntimeError):
    """Raised when the isolated resource collector cannot return a sample."""


class CollectionTimeoutError(CollectionWorkerError):
    """Raised after terminating a resource collector that exceeded its deadline."""


def _create_kill_on_close_job() -> Any:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(information), ctypes.sizeof(information)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    return job


def _collection_worker_main(
    settings: Settings,
    collector: Callable[[Settings], dict[str, Any]],
    requests: Any,
    responses: Any,
) -> None:
    job = None
    setup_error: Exception | None = None
    try:
        job = _create_kill_on_close_job()
    except (OSError, RuntimeError, ValueError) as exc:
        setup_error = exc
    try:
        while True:
            request_id = requests.get()
            if request_id is None:
                return
            if setup_error is not None:
                responses.put(
                    (request_id, False, type(setup_error).__name__, str(setup_error))
                )
                continue
            try:
                snapshot = collector(settings)
            except Exception as exc:
                responses.put((request_id, False, type(exc).__name__, str(exc)))
            else:
                responses.put((request_id, True, snapshot, None))
    finally:
        if job is not None:
            ctypes.windll.kernel32.CloseHandle(job)


class ResourceCollectionWorker:
    """Runs synchronous resource collection in a replaceable child process."""

    def __init__(
        self,
        settings: Settings,
        collector: Callable[[Settings], dict[str, Any]] = collect_snapshot,
    ) -> None:
        self.settings = settings
        self.collector = collector
        self._context = multiprocessing.get_context("spawn")
        self._process: Any = None
        self._requests: Any = None
        self._responses: Any = None
        self._request_id = 0
        self._lock = threading.Lock()

    def collect(self, timeout: float) -> dict[str, Any]:
        if timeout <= 0:
            raise ValueError("采集超时必须大于 0 秒")
        with self._lock:
            self._ensure_started()
            self._request_id += 1
            request_id = self._request_id
            self._requests.put(request_id)
            try:
                response = self._responses.get(timeout=timeout)
            except queue.Empty as exc:
                self._discard_worker(terminate=True)
                raise CollectionTimeoutError(
                    f"资源采集超过 {timeout:g} 秒，已终止并重建采集进程"
                ) from exc
            if response[0] != request_id:
                self._discard_worker(terminate=True)
                raise CollectionWorkerError("资源采集进程返回了不匹配的请求")
            if not response[1]:
                raise CollectionWorkerError(
                    f"资源采集失败（{response[2]}）: {response[3]}"
                )
            return response[2]

    def close(self) -> None:
        with self._lock:
            self._discard_worker(terminate=False)

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        self._discard_worker(terminate=True)
        requests = self._context.Queue()
        responses = self._context.Queue()
        process = self._context.Process(
            target=_collection_worker_main,
            args=(self.settings, self.collector, requests, responses),
            name="axis-resource-collector",
            daemon=True,
        )
        try:
            process.start()
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            requests.close()
            responses.close()
            raise CollectionWorkerError(f"无法启动资源采集进程: {exc}") from exc
        self._requests = requests
        self._responses = responses
        self._process = process

    def _discard_worker(self, terminate: bool) -> None:
        process = self._process
        requests = self._requests
        responses = self._responses
        self._process = None
        self._requests = None
        self._responses = None
        if process is not None:
            if terminate and process.is_alive():
                process.terminate()
            elif process.is_alive() and requests is not None:
                try:
                    requests.put(None)
                except (OSError, ValueError):
                    process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            process.close()
        for channel in (requests, responses):
            if channel is not None:
                channel.cancel_join_thread()
                channel.close()


class HistoryStore:
    def __init__(self, capacity: int) -> None:
        self._samples: deque[dict[str, Any]] = deque(maxlen=capacity)

    def append(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        host = snapshot.get("host", {})
        cpu = host.get("cpu", {})
        memory = host.get("memory", {})
        primary_network = host.get("primary_network") or {}
        wsl = host.get("wsl") or {}
        power = host.get("power") or {}
        stale_collectors = snapshot.get("stale_collectors") or {}
        history_gpus = [] if "nvidia" in stale_collectors else snapshot.get("gpus", [])
        record = {
                "sampled_at": snapshot["sampled_at"],
                "total_power_w": power.get("total_w"),
                "measured_power_w": power.get("measured_w"),
                "estimated_power_w": power.get("estimated_w"),
                "cpu_load_percent": cpu.get("load_percent"),
                "cpu_temperature_c": cpu.get("temperature_c"),
                "cpu_frequency_mhz": cpu.get("frequency_mhz"),
                "memory_percent": memory.get("percent"),
                "memory_used_bytes": memory.get("used_bytes"),
                "memory_total_bytes": memory.get("total_bytes"),
                "memory_available_bytes": memory.get("available_bytes"),
                "commit_used_bytes": memory.get("commit_used_bytes"),
                "commit_limit_bytes": memory.get("commit_limit_bytes"),
                "swap_used_bytes": memory.get("swap_used_bytes"),
                "swap_total_bytes": memory.get("swap_total_bytes"),
                "network_received_bytes_per_second": primary_network.get(
                    "received_bytes_per_second"
                ),
                "network_sent_bytes_per_second": primary_network.get(
                    "sent_bytes_per_second"
                ),
                "wsl_memory_used_bytes": wsl.get("memory_used_bytes"),
                "wsl_swap_used_bytes": wsl.get("swap_used_bytes"),
                "disks": [
                    {
                        "name": disk.get("name"),
                        "read_bytes_per_second": disk.get("read_bytes_per_second"),
                        "write_bytes_per_second": disk.get("write_bytes_per_second"),
                        "latency_ms": disk.get("latency_ms"),
                    }
                    for disk in host.get("disk_io", [])
                ],
                "gpus": [
                    {
                        "uuid": gpu.get("uuid"),
                        "index": gpu.get("index"),
                        "name": gpu.get("name"),
                        "load_percent": gpu.get("load_percent"),
                        "memory_used_mib": gpu.get("memory_used_mib"),
                        "memory_total_mib": gpu.get("memory_total_mib"),
                        "memory_percent": gpu.get("memory_percent"),
                        "temperature_c": gpu.get("temperature_c"),
                        "power_w": gpu.get("power_w"),
                        "graphics_clock_mhz": gpu.get("graphics_clock_mhz"),
                        "memory_utilization_percent": gpu.get(
                            "memory_utilization_percent"
                        ),
                        "encoder_percent": gpu.get("encoder_percent"),
                        "decoder_percent": gpu.get("decoder_percent"),
                    }
                    for gpu in history_gpus
                ],
            }
        self._samples.append(record)
        return record

    def query(
        self, window_minutes: int, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=window_minutes)
        return [sample for sample in self._samples if datetime.fromisoformat(sample["sampled_at"]) >= cutoff]


class Sampler:
    def __init__(
        self,
        settings: Settings,
        collector: Callable[[Settings], dict[str, Any]] = collect_snapshot,
        sample_sink: Callable[[dict[str, Any]], None] | None = None,
        isolated_collection: bool = False,
        power_model_provider: Callable[[], PowerModel] | None = None,
    ) -> None:
        self.settings = settings
        self._power_model_provider = power_model_provider
        self.history = HistoryStore(settings.realtime_history_capacity)
        self.current: dict[str, Any] | None = None
        self._collector = collector
        self._collection_worker = ResourceCollectionWorker(settings, collector) \
            if isolated_collection else None
        self._collection_timeout_seconds = max(30.0, settings.command_timeout_seconds * 8)
        self._sample_sink = sample_sink
        self._task: asyncio.Task[None] | None = None
        self._collection_task: asyncio.Task[dict[str, Any]] | None = None
        self._lock = asyncio.Lock()
        self.last_error: dict[str, str] | None = None
        self.history_persistence_error: dict[str, str] | None = None
        self._last_successful_gpus: list[dict[str, Any]] | None = None
        self._last_successful_gpu_at: str | None = None

    def set_sample_sink(self, sample_sink: Callable[[dict[str, Any]], None]) -> None:
        self._sample_sink = sample_sink

    def set_power_model_provider(
        self, power_model_provider: Callable[[], PowerModel] | None
    ) -> None:
        self._power_model_provider = power_model_provider

    def _apply_power_model(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """在主进程里补上估算功耗。

        采集子进程只输出实测值，配置和校准数据都在主进程，因此估算放在这里做。
        这里重新汇总 GPU 功耗，是为了让 nvidia 采集短暂失败、界面回退到上一批
        GPU 数据时，功耗曲线和界面显示的是同一批数据，而不是只剩 CPU 的那一半。
        """
        if self._power_model_provider is None:
            return snapshot
        host = snapshot.get("host")
        if not isinstance(host, dict):
            return snapshot
        power = host.get("power")
        sensors = power.get("sensors", []) if isinstance(power, dict) else []
        try:
            model = self._power_model_provider()
        except Exception:
            # 估算参数取不到时保留实测值，不能因为估算失败而丢掉整条曲线。
            return snapshot
        host["power"] = summarize_power(snapshot.get("gpus", []), sensors, model)
        return snapshot

    def record_error(self, exc: Exception, message: str) -> None:
        self.last_error = {
            "collector": "sampler",
            "error_type": type(exc).__name__,
            "message": message,
            "cause": str(exc),
        }
        if self.current is None:
            return
        current = copy.deepcopy(self.current)
        last_success_at = str(current.get("sampled_at") or "") or None
        stale_collectors = dict(current.get("stale_collectors") or {})
        stale_collectors["snapshot"] = {"last_success_at": last_success_at}
        if self._last_successful_gpus is not None:
            stale_collectors.setdefault(
                "nvidia", {"last_success_at": self._last_successful_gpu_at}
            )
        current["stale_collectors"] = stale_collectors
        collector_errors = [
            error for error in current.get("collector_errors", [])
            if not isinstance(error, dict) or error.get("collector") != "sampler"
        ]
        collector_errors.append({
            "collector": "sampler",
            "error_type": self.last_error["error_type"],
            "message": self.last_error["message"],
        })
        current["collector_errors"] = collector_errors
        self.current = current

    async def sample_once(self) -> dict[str, Any]:
        async with self._lock:
            collection_task = self._collection_task
            if collection_task is None:
                collection_task = asyncio.create_task(
                    asyncio.to_thread(self._collect),
                    name="read-only-resource-collection",
                )
                self._collection_task = collection_task
            try:
                snapshot = await asyncio.shield(collection_task)
            finally:
                if collection_task.done():
                    self._collection_task = None
            snapshot = self._preserve_last_successful_gpus(snapshot)
            snapshot = self._apply_power_model(snapshot)
            self.current = snapshot
            history_record = self.history.append(snapshot)
            if self._sample_sink is not None:
                try:
                    await asyncio.to_thread(self._sample_sink, history_record)
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    self.history_persistence_error = {
                        "error_type": type(exc).__name__,
                        "message": "资源历史写入失败，将在下个采样周期重试",
                        "cause": str(exc),
                    }
                else:
                    self.history_persistence_error = None
            self.last_error = None
            return snapshot

    def _collect(self) -> dict[str, Any]:
        if self._collection_worker is not None:
            return self._collection_worker.collect(self._collection_timeout_seconds)
        return self._collector(self.settings)

    def _preserve_last_successful_gpus(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        nvidia_failed = any(
            error.get("collector") == "nvidia"
            for error in snapshot.get("collector_errors", [])
            if isinstance(error, dict)
        )
        if not nvidia_failed:
            self._last_successful_gpus = copy.deepcopy(snapshot.get("gpus", []))
            self._last_successful_gpu_at = str(snapshot.get("sampled_at") or "") or None
            stale_collectors = dict(snapshot.get("stale_collectors") or {})
            stale_collectors.pop("nvidia", None)
            if stale_collectors:
                snapshot["stale_collectors"] = stale_collectors
            else:
                snapshot.pop("stale_collectors", None)
            return snapshot
        if self._last_successful_gpus is None:
            return snapshot
        snapshot["gpus"] = copy.deepcopy(self._last_successful_gpus)
        stale_collectors = dict(snapshot.get("stale_collectors") or {})
        stale_collectors["nvidia"] = {
            "last_success_at": self._last_successful_gpu_at,
        }
        snapshot["stale_collectors"] = stale_collectors
        return snapshot

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="read-only-resource-sampler")

    async def stop(self) -> None:
        sampler_task = self._task
        collection_task = self._collection_task
        if sampler_task is None and collection_task is None:
            if self._collection_worker is not None:
                await asyncio.to_thread(self._collection_worker.close)
            return
        sampler_failure: Exception | None = None
        if sampler_task is not None:
            sampler_task.cancel()
            try:
                await sampler_task
            except asyncio.CancelledError:
                sampler_failure = None
            except Exception as exc:
                sampler_failure = exc
            finally:
                if self._task is sampler_task and sampler_task.done():
                    self._task = None

        collection_task = self._collection_task
        collection_failure: Exception | None = None
        if collection_task is not None:
            try:
                await asyncio.shield(collection_task)
            except Exception as exc:
                self.last_error = {
                    "collector": "sampler",
                    "error_type": type(exc).__name__,
                    "message": "停止采样器时当前采集失败",
                    "cause": str(exc),
                }
                collection_failure = exc
            finally:
                if self._collection_task is collection_task and collection_task.done():
                    self._collection_task = None

        if sampler_failure is not None and collection_failure is None:
            self.last_error = {
                "collector": "sampler",
                "error_type": type(sampler_failure).__name__,
                "message": "停止采样器时后台任务失败",
                "cause": str(sampler_failure),
            }
        close_failure: Exception | None = None
        try:
            if self._collection_worker is not None:
                await asyncio.to_thread(self._collection_worker.close)
        except (OSError, RuntimeError, ValueError) as exc:
            close_failure = exc
            if collection_failure is None and sampler_failure is None:
                self.record_error(exc, "停止采样器时无法关闭采集进程")
        if collection_failure is not None:
            message = str(collection_failure)
            if close_failure is not None:
                message = f"{message}；关闭采集进程同时失败: {close_failure}"
                self.last_error["cause"] = message
            raise SamplerStopError(message) from collection_failure
        if sampler_failure is not None:
            message = str(sampler_failure)
            if close_failure is not None:
                message = f"{message}；关闭采集进程同时失败: {close_failure}"
                self.last_error["cause"] = message
            raise SamplerStopError(message) from sampler_failure
        if close_failure is not None:
            raise SamplerStopError(str(close_failure)) from close_failure

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.settings.sample_interval_seconds)
            try:
                await self.sample_once()
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError) as exc:
                self.record_error(exc, "后台采样失败，将在下个周期重试")


def parse_window(window: str) -> int:
    normalized = window.strip().lower()
    if not normalized.endswith("m"):
        raise ValueError("window 必须使用分钟格式，例如 15m")
    digits = normalized[:-1]
    if not digits.isascii() or not digits.isdigit():
        raise ValueError("window 必须使用分钟格式，例如 15m")
    if len(digits) > len(str(MAX_HISTORY_WINDOW_MINUTES)):
        raise ValueError(f"window 不能超过 {MAX_HISTORY_WINDOW_MINUTES} 分钟")
    try:
        minutes = int(digits)
    except ValueError as exc:
        raise ValueError("window 必须使用分钟格式，例如 15m") from exc
    if minutes <= 0:
        raise ValueError("window 必须大于 0 分钟")
    if minutes > MAX_HISTORY_WINDOW_MINUTES:
        raise ValueError(f"window 不能超过 {MAX_HISTORY_WINDOW_MINUTES} 分钟")
    return minutes
