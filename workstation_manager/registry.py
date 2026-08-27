from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .database import Database, DatabaseError, OperationBusyError, utc_now


SERVICE_STATES = {"running", "stopped", "unhealthy", "unknown"}
SERVICE_ACTIONS = {"start", "stop", "restart"}
SCRIPT_SUFFIXES = {".cmd", ".bat", ".ps1"}
ID_RE = re.compile(r"[0-9a-f]{32}")


class RegistryError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ManagerInstanceLock:
    """Prevent two manager processes from controlling the same database."""

    def __init__(self, database_path: Path) -> None:
        self.path = database_path.with_suffix(f"{database_path.suffix}.lock")
        self._handle: Any | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        try:
            handle = self.path.open("a+b")
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
            if "handle" in locals():
                handle.close()
            raise RegistryError(
                409, "manager_already_running", "同一数据库已有管理器实例正在运行"
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
            raise RegistryError(500, "manager_unlock_failed", "无法释放管理器实例锁") from exc
        finally:
            handle.close()


@dataclass(frozen=True)
class ScriptResult:
    returncode: int
    stdout: str
    stderr: str


class ScriptRunner:
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

    def run(self, script_path: str, action: str) -> ScriptResult:
        if action not in SERVICE_ACTIONS | {"status"}:
            raise RegistryError(422, "invalid_action", "脚本动作无效")
        path = self.validate_path(script_path)
        timeout = self.status_timeout_seconds if action == "status" else self.action_timeout_seconds
        try:
            completed = subprocess.run(
                self._command(path, action), cwd=path.parent, shell=False,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise RegistryError(504, "script_timeout", f"脚本动作 {action} 执行超时") from exc
        except OSError as exc:
            raise RegistryError(500, "script_launch_failed", f"无法启动管理脚本: {exc}") from exc
        return ScriptResult(
            completed.returncode,
            completed.stdout[-4096:].strip(),
            completed.stderr[-4096:].strip(),
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
    return {"name": name, "description": description, "script_path": script_path,
            "gpu_label": gpu_label, "port": port, "ui_url": ui_url}


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
    return {"name": name, "description": description, "service_ids": service_ids}


class RegisteredServiceManager:
    def __init__(self, database: Database, runner: ScriptRunner | None = None,
                 poll_interval_seconds: float = 5.0) -> None:
        self.database = database
        self.runner = runner or ScriptRunner()
        self.poll_interval_seconds = poll_interval_seconds
        self.statuses: dict[str, dict[str, Any]] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._operation_tasks: set[asyncio.Task[None]] = set()
        self._operation_pending = False
        self._busy_services: set[str] = set()
        self._service_locks: dict[str, asyncio.Lock] = {}
        self._stop_event = asyncio.Event()
        self._instance_lock = ManagerInstanceLock(self.database.path)
        self.last_poll_error: str | None = None
        self.last_operation_error: str | None = None

    async def start(self) -> None:
        self._instance_lock.acquire()
        self._stop_event.clear()
        try:
            self.database.interrupt_simple_operations()
            await self.refresh_all_statuses()
            self._poll_task = asyncio.create_task(self._poll_loop())
        except Exception:
            self._instance_lock.release()
            raise

    async def shutdown(self) -> None:
        self._stop_event.set()
        if self._poll_task is not None:
            await asyncio.gather(self._poll_task, return_exceptions=True)
            self._poll_task = None
        if self._operation_tasks:
            await asyncio.gather(*tuple(self._operation_tasks), return_exceptions=True)
        self._instance_lock.release()

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                try:
                    await self.refresh_all_statuses()
                    self.last_poll_error = None
                except DatabaseError as exc:
                    self.last_poll_error = str(exc)

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
            self.statuses[service_id] = status
            return status

    async def refresh_all_statuses(self) -> None:
        services = self.database.list_registered_services()
        await asyncio.gather(*(self.refresh_status(item) for item in services))
        known = {item["id"] for item in services}
        self.statuses = {key: value for key, value in self.statuses.items() if key in known}

    def list_services(self) -> list[dict[str, Any]]:
        result = []
        for item in self.database.list_registered_services():
            enriched = dict(item)
            enriched["status"] = self.statuses.get(
                item["id"], {"state": "unknown", "checked_at": None, "error": None}
            )
            enriched["busy"] = self._operation_pending
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
        await self.refresh_status(item)
        return next(service for service in self.list_services() if service["id"] == item["id"])

    async def update_service(self, service_id: str, payload: dict[str, Any],
                             username: str, source_ip: str) -> dict[str, Any]:
        self._require_idle()
        self._require_service(service_id)
        item = validate_service_input(payload, self.runner)
        try:
            updated = self.database.update_registered_service(service_id, item)
        except DatabaseError as exc:
            raise RegistryError(409, "service_conflict", str(exc)) from exc
        if not updated:
            raise RegistryError(404, "service_not_found", "已登记服务不存在")
        self.database.append_audit(source_ip, "management.service.update", "success",
                                   {"service_id": service_id, "name": item["name"],
                                    "requested_by": username})
        service = self._require_service(service_id)
        await self.refresh_status(service)
        return next(value for value in self.list_services() if value["id"] == service_id)

    def delete_service(self, service_id: str, username: str, source_ip: str) -> None:
        self._require_idle()
        service = self._require_service(service_id)
        if service_id in self._busy_services:
            raise RegistryError(409, "service_busy", "服务操作正在执行")
        if not self.database.delete_registered_service(service_id):
            raise RegistryError(404, "service_not_found", "已登记服务不存在")
        self.statuses.pop(service_id, None)
        self._service_locks.pop(service_id, None)
        self.database.append_audit(source_ip, "management.service.delete", "success",
                                   {"service_id": service_id, "name": service["name"],
                                    "requested_by": username})

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

    def list_scenes(self) -> list[dict[str, Any]]:
        return [self._scene_with_state(item) for item in self.database.list_scenes()]

    def _scene_with_state(self, scene: dict[str, Any] | None) -> dict[str, Any]:
        if scene is None:
            raise RegistryError(404, "scene_not_found", "场景不存在")
        target = set(scene["service_ids"])
        services = self.database.list_registered_services()
        matches = True
        for service in services:
            state = self.statuses.get(service["id"], {}).get("state", "unknown")
            expected = "running" if service["id"] in target else "stopped"
            matches = matches and state == expected
        item = dict(scene)
        item["state"] = "active" if matches else "partial"
        item["busy"] = self._operation_pending
        return item

    def submit_service_action(self, service_id: str, action: str,
                              username: str, source_ip: str) -> str:
        if action not in SERVICE_ACTIONS:
            raise RegistryError(422, "invalid_action", "动作只支持 start/stop/restart")
        self._require_service(service_id)
        return self._submit("service", service_id, action, username, source_ip,
                            self._run_service_operation)

    def submit_scene_activation(self, scene_id: str, username: str, source_ip: str) -> str:
        self._require_scene(scene_id)
        return self._submit("scene", scene_id, "activate", username, source_ip,
                            self._run_scene_operation)

    def _submit(self, kind: str, target_id: str, action: str, username: str,
                source_ip: str, worker: Any) -> str:
        if self._operation_pending:
            raise RegistryError(409, "operation_busy", "已有服务或场景操作正在执行")
        operation_id = uuid.uuid4().hex
        self._operation_pending = True
        try:
            self.database.create_operation(operation_id, kind, target_id, action, username, source_ip)
        except OperationBusyError as exc:
            self._operation_pending = False
            raise RegistryError(409, "operation_busy", str(exc)) from exc
        except Exception:
            self._operation_pending = False
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
            if release_pending:
                self._operation_pending = False

    async def _run_script_action(self, operation_id: str, sequence: int,
                                 phase: str, service: dict[str, Any], action: str) -> bool:
        service_id = service["id"]
        async with self._service_lock(service_id):
            before = self.statuses.get(service_id, {}).get("state", "unknown")
            self.database.create_operation_step(operation_id, sequence, phase, service_id,
                                                action, before_state=before)
            self._busy_services.add(service_id)
            try:
                result = await asyncio.to_thread(self.runner.run, service["script_path"], action)
                status = await self._probe_status(service)
                self.statuses[service_id] = status
                expected = "stopped" if action == "stop" else "running"
                success = result.returncode == 0 and status["state"] == expected
                if result.returncode != 0:
                    error = result.stderr or result.stdout or f"脚本退出码 {result.returncode}"
                elif status["state"] != expected:
                    error = f"动作完成后状态为 {status['state']}，预期 {expected}"
                    if status.get("error"):
                        error = f"{error}；{status['error']}"
                else:
                    error = None
                self.database.finish_operation_step(
                    operation_id, sequence, "succeeded" if success else "failed",
                    status["state"], "success" if success else "failure", error,
                )
                return success
            except RegistryError as exc:
                self.database.finish_operation_step(operation_id, sequence, "failed", "unknown",
                                                    "failure", exc.message)
                self.statuses[service_id] = {"state": "unknown", "checked_at": utc_now(),
                                             "error": exc.message}
                return False
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.database.finish_operation_step(operation_id, sequence, "failed", "unknown",
                                                    "failure", message)
                self.statuses[service_id] = {"state": "unknown", "checked_at": utc_now(),
                                             "error": message}
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
            None if success else "服务脚本执行失败或最终状态不符合预期",
        )

    async def _run_scene_operation(self, operation_id: str, scene_id: str, _: str) -> None:
        scene = self._require_scene(scene_id)
        before = {key: value.get("state", "unknown") for key, value in self.statuses.items()}
        self.database.update_operation(operation_id, status="running", started_at=utc_now(),
                                       before_state=str(before))
        target_ids = list(scene["service_ids"])
        target = set(target_ids)
        services = {item["id"]: item for item in self.database.list_registered_services()}
        sequence = 0
        stop_ok = True
        for service in services.values():
            if service["id"] not in target:
                sequence += 1
                stop_ok = await self._run_script_action(
                    operation_id, sequence, "stop_unselected", service, "stop"
                ) and stop_ok
        start_ok = True
        if stop_ok:
            for service_id in target_ids:
                sequence += 1
                start_ok = await self._run_script_action(
                    operation_id, sequence, "start_selected", services[service_id], "start"
                ) and start_ok
        await self.refresh_all_statuses()
        final_scene = self._scene_with_state(scene)
        success = stop_ok and start_ok and final_scene["state"] == "active"
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
        if self._operation_pending or self.database.has_active_operation():
            raise RegistryError(409, "operation_busy", "已有服务或场景操作正在执行")

    def _require_scene(self, scene_id: str) -> dict[str, Any]:
        if ID_RE.fullmatch(scene_id) is None:
            raise RegistryError(404, "scene_not_found", "场景不存在")
        item = self.database.get_scene(scene_id)
        if item is None:
            raise RegistryError(404, "scene_not_found", "场景不存在")
        return item
