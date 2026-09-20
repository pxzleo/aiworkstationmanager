from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .database import Database, DatabaseError


class AutomaticTaskExecutionError(RuntimeError):
    pass


class AutomaticTaskExecutor:
    """Run the queue serially, with one OpenCode session and directory per task."""

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
        self.current_task_id: str | None = None
        self.current_working_directory: str | None = None

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
            "current_task_id": self.current_task_id,
            "current_working_directory": self.current_working_directory,
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
            parent_directory = self.validate_directory(settings["working_directory"])
            executable = self._opencode_path()
            self.database.append_audit(
                source_ip, "management.automatic_task.execution_start", "requested",
                {"trigger": trigger, "requested_by": requested_by, "working_directory": parent_directory},
            )
            task = self.database.next_automatic_task_for_execution()
            if task is None:
                raise AutomaticTaskExecutionError("没有未执行的自动任务")
            process = await self._launch_task(executable, Path(parent_directory), task["id"])
            self.status = "running"
            self.last_trigger = trigger
            self.last_started_at = datetime.now().astimezone().isoformat()
            self.last_finished_at = None
            self.last_error = None
            self._worker = asyncio.create_task(
                self._run_queue(executable, Path(parent_directory), task["id"], process)
            )
            return self.snapshot()

    async def _launch_task(
        self, executable: str, parent_directory: Path, task_id: str,
    ) -> asyncio.subprocess.Process:
        if re.fullmatch(r"[0-9a-f]{32}", task_id) is None:
            raise AutomaticTaskExecutionError("自动任务 ID 无效，不能创建执行目录")
        directory = parent_directory / f"task-{task_id[:12]}-{uuid4().hex[:8]}"
        try:
            directory.mkdir()
            process = await asyncio.create_subprocess_exec(
                executable, "run", "--dir", str(directory), "--title", f"AXIS 自动任务 {task_id[:8]}",
                f"使用 axis-automatic-tasks Skill 的单项执行模式。调用 axis_automatic_task_claim，"
                f"传入 expected_task_id={task_id}，只执行并回写这一条任务，然后结束当前会话；不要领取下一条。",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, ValueError) as exc:
            raise AutomaticTaskExecutionError(f"任务 {task_id[:8]} 启动失败: {exc}") from exc
        self._process = process
        self.current_task_id = task_id
        self.current_working_directory = str(directory)
        return process

    async def _run_queue(
        self, executable: str, parent_directory: Path, task_id: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        try:
            while True:
                code = await process.wait()
                status = self.database.automatic_task_status(task_id)
                if status not in {"succeeded", "failed"}:
                    raise AutomaticTaskExecutionError(
                        f"任务 {task_id[:8]} 的 OpenCode 会话已退出（代码 {code}），任务仍为 {status or '不存在'}"
                    )
                task = self.database.next_automatic_task_for_execution()
                if task is None:
                    self.status = "succeeded"
                    break
                task_id = task["id"]
                process = await self._launch_task(executable, parent_directory, task_id)
        except (AutomaticTaskExecutionError, DatabaseError, OSError) as exc:
            self.status = "failed"
            self.last_error = str(exc)
        finally:
            self.last_finished_at = datetime.now().astimezone().isoformat()
            self.current_task_id = None
            self.current_working_directory = None

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
