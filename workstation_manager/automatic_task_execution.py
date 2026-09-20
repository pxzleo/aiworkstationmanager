from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .database import Database, DatabaseError


class AutomaticTaskExecutionError(RuntimeError):
    pass


class AutomaticTaskExecutor:
    """Launch one OpenCode session for the saved automatic-task queue."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._lock = asyncio.Lock()
        self._scheduler: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self.status = "idle"
        self.last_trigger: str | None = None
        self.last_started_at: str | None = None
        self.last_finished_at: str | None = None
        self.last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        queue = self.database.automatic_task_execution_queue_state()
        return {
            "settings": self.database.automatic_task_execution_settings(),
            "queue": queue,
            "status": self.status,
            "last_trigger": self.last_trigger,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_error": self.last_error,
        }

    @staticmethod
    def validate_directory(value: str) -> str:
        path = Path(value).expanduser()
        if not value or not path.is_absolute() or not path.is_dir():
            raise AutomaticTaskExecutionError("工作目录必须是本机已存在的绝对目录")
        return str(path.resolve())

    @staticmethod
    def _opencode_path() -> str:
        found = shutil.which("opencode")
        if found:
            return found
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        candidate = Path(local_app_data) / "Programs/@openchamberelectron/resources/opencode-cli/opencode.exe"
        if local_app_data and candidate.is_file():
            return str(candidate)
        raise AutomaticTaskExecutionError("找不到 OpenCode 命令行程序，请安装 OpenCode 或将其加入 PATH")

    async def start(self, trigger: str, requested_by: str = "AXIS", source_ip: str = "127.0.0.1") -> dict[str, Any]:
        async with self._lock:
            if self._worker is not None and not self._worker.done():
                raise AutomaticTaskExecutionError("自动任务队列已经在执行")
            queue = self.database.automatic_task_execution_queue_state()
            if queue["busy"]:
                raise AutomaticTaskExecutionError("已有 OpenCode 会话正在执行自动任务")
            if not queue["runnable"]:
                raise AutomaticTaskExecutionError("没有未执行的自动任务")
            settings = self.database.automatic_task_execution_settings()
            directory = self.validate_directory(settings["working_directory"])
            executable = self._opencode_path()
            self.database.append_audit(
                source_ip, "management.automatic_task.execution_start", "requested",
                {"trigger": trigger, "requested_by": requested_by, "working_directory": directory},
            )
            try:
                self._process = await asyncio.create_subprocess_exec(
                    executable, "run", "--dir", directory, "--title", "AXIS 自动任务",
                    "使用 axis-automatic-tasks Skill，从 AXIS 领取并依次完整执行当前所有未执行任务，逐项回写结果，直到队列为空。",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                raise AutomaticTaskExecutionError(f"启动 OpenCode 失败: {exc}") from exc
            self.status = "running"
            self.last_trigger = trigger
            self.last_started_at = datetime.now().astimezone().isoformat()
            self.last_finished_at = None
            self.last_error = None
            self._worker = asyncio.create_task(self._monitor(self._process))
            return self.snapshot()

    async def _monitor(self, process: asyncio.subprocess.Process) -> None:
        code = await process.wait()
        self.last_finished_at = datetime.now().astimezone().isoformat()
        try:
            summary = self.database.automatic_task_summary()
        except DatabaseError as exc:
            self.status = "failed"
            self.last_error = f"OpenCode 已退出，但无法读取剩余任务: {exc}"
            return
        remaining = summary["pending"] + summary["running"]
        if code or remaining:
            self.status = "failed"
            self.last_error = f"OpenCode 已退出（代码 {code}），仍有 {remaining} 项未完成任务" if remaining else f"OpenCode 已退出（代码 {code}）"
        else:
            self.status = "succeeded"

    async def tick(self, now: datetime | None = None) -> None:
        current = now or datetime.now().astimezone()
        settings = self.database.automatic_task_execution_settings()
        local_date = current.date().isoformat()
        if not settings["enabled"] or current.strftime("%H:%M") != settings["time_local"]:
            return
        if settings["last_trigger_date"] == local_date:
            return
        if self._worker is not None and not self._worker.done():
            return
        if self.database.automatic_task_execution_queue_state()["busy"]:
            return
        if not self.database.mark_automatic_task_daily_trigger(local_date):
            return
        try:
            await self.start("schedule")
        except AutomaticTaskExecutionError as exc:
            self.status = "failed"
            self.last_trigger = "schedule"
            self.last_finished_at = current.isoformat()
            self.last_error = str(exc)

    def start_scheduler(self) -> None:
        self._scheduler = asyncio.create_task(self._schedule_loop())

    async def shutdown(self) -> None:
        if self._scheduler:
            self._scheduler.cancel()
            try:
                await self._scheduler
            except asyncio.CancelledError:
                pass

    async def _schedule_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except (AutomaticTaskExecutionError, DatabaseError, OSError, ValueError) as exc:
                self.status = "failed"
                self.last_trigger = "schedule"
                self.last_finished_at = datetime.now().astimezone().isoformat()
                self.last_error = f"定时检查失败: {exc}"
            await asyncio.sleep(20)
