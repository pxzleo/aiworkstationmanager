from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import Settings
from .collectors import collect_snapshot


MAX_HISTORY_WINDOW_MINUTES = 24 * 60


class SamplerStopError(RuntimeError):
    """Raised when an in-flight collector fails during shutdown."""


class HistoryStore:
    def __init__(self, capacity: int) -> None:
        self._samples: deque[dict[str, Any]] = deque(maxlen=capacity)

    def append(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        host = snapshot.get("host", {})
        cpu = host.get("cpu", {})
        memory = host.get("memory", {})
        record = {
                "sampled_at": snapshot["sampled_at"],
                "cpu_load_percent": cpu.get("load_percent"),
                "cpu_temperature_c": cpu.get("temperature_c"),
                "memory_percent": memory.get("percent"),
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
                    }
                    for gpu in snapshot.get("gpus", [])
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
    ) -> None:
        self.settings = settings
        self.history = HistoryStore(settings.realtime_history_capacity)
        self.current: dict[str, Any] | None = None
        self._collector = collector
        self._sample_sink = sample_sink
        self._task: asyncio.Task[None] | None = None
        self._collection_task: asyncio.Task[dict[str, Any]] | None = None
        self._lock = asyncio.Lock()
        self.last_error: dict[str, str] | None = None
        self.history_persistence_error: dict[str, str] | None = None

    def set_sample_sink(self, sample_sink: Callable[[dict[str, Any]], None]) -> None:
        self._sample_sink = sample_sink

    async def sample_once(self) -> dict[str, Any]:
        async with self._lock:
            collection_task = self._collection_task
            if collection_task is None:
                collection_task = asyncio.create_task(
                    asyncio.to_thread(self._collector, self.settings),
                    name="read-only-resource-collection",
                )
                self._collection_task = collection_task
            try:
                snapshot = await asyncio.shield(collection_task)
            finally:
                if collection_task.done():
                    self._collection_task = None
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

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="read-only-resource-sampler")

    async def stop(self) -> None:
        sampler_task = self._task
        collection_task = self._collection_task
        if sampler_task is None and collection_task is None:
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
                raise SamplerStopError(str(exc)) from exc
            finally:
                if self._collection_task is collection_task and collection_task.done():
                    self._collection_task = None

        if sampler_failure is not None:
            self.last_error = {
                "collector": "sampler",
                "error_type": type(sampler_failure).__name__,
                "message": "停止采样器时后台任务失败",
                "cause": str(sampler_failure),
            }
            raise SamplerStopError(str(sampler_failure)) from sampler_failure

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.settings.sample_interval_seconds)
            try:
                await self.sample_once()
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError) as exc:
                self.last_error = {
                    "collector": "sampler",
                    "error_type": type(exc).__name__,
                    "message": "后台采样失败，将在下个周期重试",
                    "cause": str(exc),
                }


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
