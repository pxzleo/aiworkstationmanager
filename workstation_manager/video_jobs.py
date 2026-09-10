from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .database import Database, DatabaseError, utc_now
from .registry import RegisteredServiceManager, RegistryError


GPU_4090_LEASE = "gpu:4090"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
METRIC_RE = re.compile(r"^(?:llamacpp|ninfer):requests_(processing|deferred)\s+([0-9]+(?:\.[0-9]+)?)$")
MAX_VIDEO_BYTES = 16 * 1024 * 1024 * 1024


class VideoJobError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class JsonHttpClient:
    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def request(
        self, method: str, path: str, body: Any | None = None,
        *, headers: dict[str, str] | None = None, expected: tuple[int, ...] = (200,),
    ) -> tuple[int, bytes, str]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers = dict(headers or {})
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                content = response.read()
                content_type = response.headers.get("Content-Type", "")
        except HTTPError as exc:
            detail = exc.read(1024).decode("utf-8", errors="replace")
            raise VideoJobError(
                "http_error", f"{method} {path} 返回 HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise VideoJobError("http_unreachable", f"{method} {path} 无法访问: {exc}") from exc
        if status not in expected:
            raise VideoJobError("http_status", f"{method} {path} 返回意外 HTTP {status}")
        return status, content, content_type

    def json(self, method: str, path: str, body: Any | None = None) -> Any:
        _, content, _ = self.request(method, path, body)
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise VideoJobError("invalid_json", f"{method} {path} 返回无效 JSON: {exc}") from exc


class NInferClient:
    def __init__(self, base_url: str, model_id: str) -> None:
        self.http = JsonHttpClient(base_url)
        self.model_id = model_id

    def activity(self) -> dict[str, Any]:
        slots = self.http.json("GET", "/slots")
        if not isinstance(slots, list):
            raise VideoJobError("invalid_ninfer_slots", "NInfer /slots 根节点不是数组")
        active_slots = []
        for slot in slots:
            if not isinstance(slot, dict):
                raise VideoJobError("invalid_ninfer_slots", "NInfer /slots 包含非对象条目")
            if slot.get("is_processing") is True or str(slot.get("state", "")).lower() not in {"idle"}:
                active_slots.append(slot.get("id"))
        _, metrics_bytes, _ = self.http.request("GET", "/metrics")
        metrics = metrics_bytes.decode("utf-8", errors="strict")
        counts: dict[str, int] = {}
        for line in metrics.splitlines():
            match = METRIC_RE.match(line.strip())
            if match:
                counts[match.group(1)] = max(counts.get(match.group(1), 0), int(float(match.group(2))))
        if set(counts) != {"processing", "deferred"}:
            raise VideoJobError(
                "invalid_ninfer_metrics", "NInfer /metrics 缺少 requests_processing 或 requests_deferred"
            )
        return {
            "processing": counts["processing"],
            "deferred": counts["deferred"],
            "active_slots": active_slots,
            "idle": counts["processing"] == 0 and counts["deferred"] == 0 and not active_slots,
        }

    def verify(self) -> None:
        self.http.request("GET", "/health")
        models = self.http.json("GET", "/v1/models")
        if not isinstance(models, dict) or not isinstance(models.get("data"), list):
            raise VideoJobError("ninfer_models_invalid", "NInfer /v1/models 返回格式无效")
        model_ids = {
            str(item.get("id")) for item in models.get("data", [])
            if isinstance(item, dict)
        }
        if self.model_id not in model_ids:
            raise VideoJobError(
                "ninfer_model_missing", f"NInfer /v1/models 未返回模型 {self.model_id}"
            )
        response = self.http.json(
            "POST", "/v1/chat/completions",
            {"model": self.model_id, "messages": [{"role": "user", "content": "Reply OK."}],
             "max_tokens": 1, "stream": False},
        )
        if not isinstance(response, dict) or not response.get("choices"):
            raise VideoJobError("ninfer_completion_invalid", "NInfer 真实请求未返回 choices")


class ComfyUIClient:
    def __init__(self, base_url: str) -> None:
        self.http = JsonHttpClient(base_url)

    def health(self) -> None:
        value = self.http.json("GET", "/system_stats")
        if not isinstance(value, dict):
            raise VideoJobError("comfy_health_invalid", "ComfyUI /system_stats 返回格式无效")

    def submit(self, workflow: dict[str, Any], job_id: str) -> str:
        response = self.http.json(
            "POST", "/prompt",
            {"prompt": workflow, "client_id": f"axis-{job_id}",
             "extra_data": {"axis_job_id": job_id}},
        )
        prompt_id = response.get("prompt_id") if isinstance(response, dict) else None
        if not isinstance(prompt_id, str) or not prompt_id:
            raise VideoJobError("comfy_prompt_missing", "ComfyUI 提交响应缺少 prompt_id")
        return prompt_id

    @staticmethod
    def _queue_entry_prompt_id(entry: Any) -> str | None:
        return str(entry[1]) if isinstance(entry, list) and len(entry) > 1 else None

    def status(self, prompt_id: str) -> dict[str, Any]:
        history = self.http.json("GET", f"/history/{quote(prompt_id, safe='')}")
        if isinstance(history, dict) and prompt_id in history:
            record = history[prompt_id]
            if not isinstance(record, dict):
                raise VideoJobError("comfy_history_invalid", "ComfyUI history 任务记录格式无效")
            status = record.get("status")
            if isinstance(status, dict) and status.get("status_str") in {"error", "failed"}:
                raise VideoJobError("comfy_execution_failed", "ComfyUI 工作流执行失败")
            outputs = record.get("outputs")
            completed = isinstance(status, dict) and (
                status.get("completed") is True
                or status.get("status_str") in {"success", "completed"}
            )
            if completed or isinstance(outputs, dict) and outputs:
                return {"state": "completed", "record": record}
        queue = self.http.json("GET", "/queue")
        if not isinstance(queue, dict):
            raise VideoJobError("comfy_queue_invalid", "ComfyUI /queue 返回格式无效")
        running = queue.get("queue_running", [])
        pending = queue.get("queue_pending", [])
        if any(self._queue_entry_prompt_id(item) == prompt_id for item in running):
            return {"state": "running", "queue_position": 0}
        for index, item in enumerate(pending):
            if self._queue_entry_prompt_id(item) == prompt_id:
                return {"state": "queued", "queue_position": index + 1}
        return {"state": "unknown"}

    @staticmethod
    def _contains_job_marker(value: Any, marker: str) -> bool:
        if isinstance(value, dict):
            return any(
                key == "axis_job_id" and item == marker
                or ComfyUIClient._contains_job_marker(item, marker)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(ComfyUIClient._contains_job_marker(item, marker) for item in value)
        return False

    def recover_prompt_id(self, job_id: str) -> str | None:
        queue = self.http.json("GET", "/queue")
        if isinstance(queue, dict):
            for group in (queue.get("queue_running", []), queue.get("queue_pending", [])):
                for entry in group:
                    if self._contains_job_marker(entry, job_id):
                        return self._queue_entry_prompt_id(entry)
        history = self.http.json("GET", "/history")
        if isinstance(history, dict):
            matches = [key for key, value in history.items() if self._contains_job_marker(value, job_id)]
            if len(matches) == 1:
                return str(matches[0])
            if len(matches) > 1:
                raise VideoJobError("comfy_recovery_ambiguous", "发现多个匹配的 ComfyUI prompt_id")
        return None

    def cancel(self, prompt_id: str) -> None:
        queue = self.http.json("GET", "/queue")
        if not isinstance(queue, dict):
            raise VideoJobError("comfy_queue_invalid", "ComfyUI /queue 返回格式无效")
        running = any(
            self._queue_entry_prompt_id(item) == prompt_id
            for item in queue.get("queue_running", [])
        )
        pending = any(
            self._queue_entry_prompt_id(item) == prompt_id
            for item in queue.get("queue_pending", [])
        )
        if pending:
            self.http.request("POST", "/queue", {"delete": [prompt_id]}, expected=(200, 204))
            return
        if running:
            self.http.request("POST", "/interrupt", {}, expected=(200, 204))
            return
        raise VideoJobError("comfy_cancel_unknown", "目标 prompt_id 不在 ComfyUI 队列中，拒绝全局中断")

    @staticmethod
    def output_descriptor(record: dict[str, Any]) -> dict[str, str]:
        outputs = record.get("outputs", {})
        for node in outputs.values() if isinstance(outputs, dict) else ():
            if not isinstance(node, dict):
                continue
            for group in ("videos", "gifs", "images"):
                for item in node.get(group, []):
                    if not isinstance(item, dict):
                        continue
                    filename = str(item.get("filename") or "")
                    if Path(filename).suffix.lower() in VIDEO_EXTENSIONS:
                        return {"filename": filename, "subfolder": str(item.get("subfolder") or ""),
                                "type": str(item.get("type") or "output")}
        raise VideoJobError("comfy_video_output_missing", "ComfyUI history 未包含视频输出文件")

    def download_output(self, descriptor: dict[str, str], target: Path) -> None:
        query = urlencode(descriptor)
        request = Request(f"{self.http.base_url}/view?{query}", method="GET")
        temporary = target.with_name(f".{target.name}.axis-part")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(target)
            temporary.unlink(missing_ok=True)
            with urlopen(request, timeout=self.http.timeout_seconds) as response, temporary.open("xb") as stream:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_VIDEO_BYTES:
                    raise VideoJobError("output_too_large", "视频输出超过 16 GiB 限制")
                written = 0
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_VIDEO_BYTES:
                        raise VideoJobError("output_too_large", "视频输出超过 16 GiB 限制")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                raise FileExistsError(target)
            temporary.rename(target)
        except FileExistsError as exc:
            temporary.unlink(missing_ok=True)
            raise VideoJobError("output_exists", f"输出路径已存在，拒绝覆盖: {target}") from exc
        except HTTPError as exc:
            raise VideoJobError("http_error", f"GET /view 返回 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            cleanup_error = None
            try:
                temporary.unlink(missing_ok=True)
            except OSError as unlink_exc:
                cleanup_error = f"；清理不完整文件也失败: {unlink_exc}"
            raise VideoJobError(
                "output_write_failed", f"写入输出文件失败 {target}: {exc}{cleanup_error or ''}"
            ) from exc
        except VideoJobError:
            temporary.unlink(missing_ok=True)
            raise


class OpenCodeCallbackClient:
    @staticmethod
    def send(job: dict[str, Any], message: str) -> None:
        base_url = str(job["callback_url"]).rstrip("/")
        query = ""
        if job.get("callback_directory"):
            query = "?" + urlencode({"directory": job["callback_directory"]})
        url = f"{base_url}/session/{quote(job['session_id'], safe='')}/prompt_async{query}"
        headers = {"Content-Type": "application/json"}
        if job.get("callback_authorization"):
            headers["Authorization"] = job["callback_authorization"]
        body = json.dumps({"parts": [{"type": "text", "text": message}]}, ensure_ascii=False).encode()
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=10) as response:
                if response.status != 204:
                    raise VideoJobError(
                        "opencode_callback_status", f"OpenCode 回调返回意外 HTTP {response.status}"
                    )
        except HTTPError as exc:
            raise VideoJobError(
                "opencode_callback_failed", f"OpenCode 回调返回 HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise VideoJobError("opencode_callback_failed", f"OpenCode 回调失败: {exc}") from exc


class VideoJobManager:
    def __init__(
        self, database: Database, registry: RegisteredServiceManager, *, comfyui_base_url: str,
        ninfer_base_url: str, ninfer_model_id: str, output_directory: Path,
        poll_interval_seconds: float = 2.0, idle_timeout_seconds: float = 3600.0,
        scene_timeout_seconds: float = 1200.0, generation_timeout_seconds: float = 7200.0,
        comfy: ComfyUIClient | None = None, ninfer: NInferClient | None = None,
        callback: OpenCodeCallbackClient | None = None,
    ) -> None:
        self.database = database
        self.registry = registry
        self.comfy = comfy or ComfyUIClient(comfyui_base_url)
        self.ninfer = ninfer or NInferClient(ninfer_base_url, ninfer_model_id)
        self.callback = callback or OpenCodeCallbackClient()
        self.output_directory = Path(output_directory)
        self.poll_interval_seconds = poll_interval_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.scene_timeout_seconds = scene_timeout_seconds
        self.generation_timeout_seconds = generation_timeout_seconds
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self.database.recover_video_jobs()
        self._task = asyncio.create_task(self._run())
        self._wake.set()

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _loopback_url(value: str, field: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname \
                or parsed.username or parsed.password or parsed.path not in {"", "/"} \
                or parsed.query or parsed.fragment:
            raise VideoJobError("invalid_callback_url", f"{field} 必须是无用户信息的完整 HTTP 地址")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            raise VideoJobError("callback_loopback_required", f"{field} 只允许 loopback 地址")
        return value.rstrip("/")

    def submit(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        if not 1 <= len(idempotency_key) <= 200:
            raise VideoJobError("invalid_idempotency_key", "idempotency_key 长度必须为 1..200")
        if not 1 <= len(session_id) <= 200:
            raise VideoJobError("invalid_session_id", "session_id 长度必须为 1..200")
        workflow_path = Path(str(payload.get("workflow_path") or ""))
        if not workflow_path.is_absolute() or workflow_path.suffix.lower() != ".json":
            raise VideoJobError("invalid_workflow_path", "workflow_path 必须是 JSON 文件的绝对路径")
        if not workflow_path.is_file():
            raise VideoJobError("workflow_not_found", f"工作流文件不存在: {workflow_path}")
        workflow = self._load_workflow(str(workflow_path))
        workflow_json = json.dumps(workflow, ensure_ascii=False, separators=(",", ":"))
        workflow_sha256 = hashlib.sha256(workflow_json.encode("utf-8")).hexdigest()
        output_text = str(payload.get("output_path") or "").strip()
        if output_text and not Path(output_text).is_absolute():
            raise VideoJobError("invalid_output_path", "output_path 必须是绝对路径")
        callback_url = self._loopback_url(str(payload.get("callback_url") or ""), "callback_url")
        callback_directory = str(payload.get("callback_directory") or "").strip() or None
        if callback_directory and not Path(callback_directory).is_absolute():
            raise VideoJobError("invalid_callback_directory", "callback_directory 必须是绝对路径")
        authorization = str(payload.get("callback_authorization") or "")
        if "\r" in authorization or "\n" in authorization:
            raise VideoJobError("invalid_callback_authorization", "callback_authorization 不能包含换行")
        canonical = {
            "session_id": session_id, "workflow_path": str(workflow_path.resolve()),
            "output_path": output_text, "callback_url": callback_url,
            "callback_directory": callback_directory,
            "workflow_sha256": workflow_sha256,
            "callback_authorization_sha256": hashlib.sha256(
                authorization.encode("utf-8")
            ).hexdigest(),
        }
        payload_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        item, created = self.database.create_video_job({
            "id": uuid.uuid4().hex, "idempotency_key": idempotency_key,
            "payload_hash": payload_hash, "session_id": session_id,
            "workflow_path": canonical["workflow_path"],
            "workflow_json": workflow_json,
            "requested_output_path": output_text or None, "callback_url": callback_url,
            "callback_authorization": authorization, "callback_directory": callback_directory,
        })
        if item["payload_hash"] != payload_hash:
            raise VideoJobError(
                "idempotency_conflict", "相同 idempotency_key 已用于不同的视频任务参数"
            )
        if created:
            self.database.append_audit(
                "local", "management.video_job.submit", "success",
                {"job_id": item["id"], "session_id": session_id,
                 "idempotency_key": idempotency_key},
            )
            self._wake.set()
        return item, created

    def cancel(self, job_id: str, username: str, source_ip: str) -> dict[str, Any]:
        result = self.database.request_video_job_cancel(job_id)
        if result == "missing":
            raise VideoJobError("video_job_not_found", "视频任务不存在")
        if result == "finished":
            raise VideoJobError("video_job_finished", "视频任务已经结束")
        self.database.append_audit(
            source_ip, "management.video_job.cancel", "success",
            {"job_id": job_id, "requested_by": username},
        )
        self._wake.set()
        job = self.database.get_video_job(job_id)
        if job is None:
            raise DatabaseError("保存取消请求后无法读回视频任务")
        return job

    async def _run(self) -> None:
        while True:
            job = self.database.next_video_job()
            if job is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self._process(job)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except (VideoJobError, RegistryError, DatabaseError, OSError, ValueError, TypeError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:1024]
                await asyncio.sleep(self.poll_interval_seconds)

    async def _cancel_requested(self, job_id: str) -> bool:
        job = self.database.get_video_job(job_id)
        if job is None:
            raise VideoJobError("video_job_not_found", "视频任务在执行期间消失")
        return bool(job["cancel_requested"])

    async def _activate_scene(self, purpose: str, job_id: str) -> None:
        scene = self.database.get_scene_by_purpose(purpose)
        if scene is None:
            raise VideoJobError("scene_purpose_missing", f"没有场景被设置为用途 {purpose}")
        if self.registry._scene_with_state(scene)["state"] == "active":
            return
        deadline = asyncio.get_running_loop().time() + self.scene_timeout_seconds
        while True:
            try:
                operation_id = self.registry.submit_scene_activation(
                    scene["id"], "video-scheduler", "local", video_job_id=job_id
                )
                break
            except RegistryError as exc:
                if exc.code != "operation_busy" or asyncio.get_running_loop().time() >= deadline:
                    raise VideoJobError(exc.code, str(exc)) from exc
                await asyncio.sleep(self.poll_interval_seconds)
        while True:
            operation = self.database.get_operation(operation_id)
            if operation is None:
                raise VideoJobError("scene_operation_missing", "场景切换操作记录消失")
            if operation["status"] in {"succeeded", "failed", "interrupted"}:
                if operation["status"] != "succeeded":
                    raise VideoJobError(
                        "scene_activation_failed", operation.get("error_summary") or "场景切换失败"
                    )
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise VideoJobError("scene_activation_timeout", "等待场景切换完成超时")
            await asyncio.sleep(self.poll_interval_seconds)

    async def _wait_for_ninfer_idle(self, job_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.idle_timeout_seconds
        while True:
            if await self._cancel_requested(job_id):
                raise VideoJobError("cancelled", "用户在场景切换前取消了视频任务")
            activity = await asyncio.to_thread(self.ninfer.activity)
            self.database.update_video_job(job_id, progress=activity)
            if activity["idle"]:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise VideoJobError("ninfer_busy_timeout", "等待 NInfer 所有活动与排队请求结束超时")
            await asyncio.sleep(self.poll_interval_seconds)

    @staticmethod
    def _load_workflow(path: str) -> dict[str, Any]:
        workflow_path = Path(path)
        try:
            if workflow_path.stat().st_size > 4 * 1024 * 1024:
                raise VideoJobError("workflow_too_large", "工作流 JSON 不能超过 4 MiB")
            value = json.loads(workflow_path.read_text(encoding="utf-8"))
        except VideoJobError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise VideoJobError("workflow_invalid", f"无法读取工作流 JSON: {exc}") from exc
        if not isinstance(value, dict) or not value:
            raise VideoJobError("workflow_invalid", "工作流 JSON 根节点必须是非空对象")
        return value

    def _target_path(self, job: dict[str, Any], filename: str) -> Path:
        requested = job.get("requested_output_path")
        if not requested:
            return self.output_directory / job["id"] / Path(filename).name
        path = Path(requested)
        if path.exists() and path.is_dir() or not path.suffix:
            return path / Path(filename).name
        return path

    async def _monitor_prompt(self, job_id: str, prompt_id: str) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.generation_timeout_seconds
        while True:
            if await self._cancel_requested(job_id):
                await asyncio.to_thread(self.comfy.cancel, prompt_id)
                raise VideoJobError("cancelled", "用户取消了视频任务")
            status = await asyncio.to_thread(self.comfy.status, prompt_id)
            self.database.update_video_job(job_id, progress=status)
            if status["state"] == "completed":
                return status["record"]
            if asyncio.get_running_loop().time() >= deadline:
                await asyncio.to_thread(self.comfy.cancel, prompt_id)
                raise VideoJobError("generation_timeout", "ComfyUI 视频生成超时并已请求取消")
            await asyncio.sleep(self.poll_interval_seconds)

    async def _deliver_callback_and_finish(
        self, job_id: str, outcome: str, output_path: str | None,
        error_code: str | None, error_summary: str | None,
    ) -> None:
        job = self.database.get_video_job(job_id, include_secret=True)
        if job is None:
            raise DatabaseError("视频任务在回调前消失")
        def callback_message() -> str:
            if outcome == "succeeded":
                return f"AXIS 视频任务 {job_id} 已完成，输出文件：{output_path}。请继续原任务。"
            if outcome == "cancelled":
                return f"AXIS 视频任务 {job_id} 已取消，Code Agent 场景和 NInfer 已恢复。请继续原任务。"
            return (
                f"AXIS 视频任务 {job_id} 失败：{error_code}: {error_summary}。"
                "已尝试 Code Agent 场景恢复和 NInfer 验证，请继续处理失败。"
            )
        attempts = int(job.get("callback_attempts") or 0)
        self.database.update_video_job(job_id, status="callback_pending", phase="callback_pending")
        callback_error: VideoJobError | None = None
        while attempts < 3:
            latest = self.database.get_video_job(job_id)
            if latest is None:
                raise DatabaseError("视频任务在回调重试前消失")
            if latest["cancel_requested"] and outcome == "succeeded":
                outcome = "cancelled"
                output_path = None
            attempts += 1
            self.database.update_video_job(job_id, callback_attempts=attempts)
            try:
                await asyncio.to_thread(self.callback.send, job, callback_message())
                callback_error = None
                break
            except asyncio.CancelledError:
                raise
            except VideoJobError as exc:
                callback_error = exc
                if attempts < 3:
                    await asyncio.sleep(min(2 ** (attempts - 1), 4))
        if callback_error is not None or attempts >= 3 and int(job.get("callback_attempts") or 0) >= 3:
            exc = callback_error or VideoJobError(
                "opencode_callback_failed", "OpenCode 回调重试次数已耗尽"
            )
            outcome = "failed"
            error_code = error_code or exc.code
            error_summary = "; ".join(filter(None, [error_summary, str(exc)]))
            self.database.finish_video_job_with_audit(
                job_id, "failed", outcome, output_path, error_code, error_summary, GPU_4090_LEASE
            )
            return
        terminal = outcome if outcome in TERMINAL_STATUSES else "failed"
        self.database.update_video_job(
            job_id, status="callback_delivered", phase="callback_delivered", result=outcome,
            output_path=output_path, error_code=error_code, error_summary=error_summary,
        )
        self.database.finish_video_job_with_audit(
            job_id, terminal, outcome, output_path, error_code, error_summary, GPU_4090_LEASE
        )

    async def _resume_cleanup(self, job: dict[str, Any]) -> None:
        outcome = str(job.get("result") or "failed")
        error_code = job.get("error_code")
        error_summary = job.get("error_summary")
        try:
            await self._activate_scene("code_agent", job["id"])
            self.database.update_video_job(
                job["id"], status="verifying_ninfer", phase="verifying_ninfer"
            )
            await asyncio.to_thread(self.ninfer.verify)
        except asyncio.CancelledError:
            raise
        except (VideoJobError, RegistryError, DatabaseError) as exc:
            outcome = "failed"
            error_code = error_code or (
                exc.code if isinstance(exc, VideoJobError) else "code_restore_failed"
            )
            error_summary = "; ".join(filter(None, [error_summary, str(exc)]))
        await self._deliver_callback_and_finish(
            job["id"], outcome, job.get("output_path"), error_code, error_summary
        )

    async def _process(self, initial_job: dict[str, Any]) -> None:
        job_id = initial_job["id"]
        if not self.database.acquire_resource_lease(GPU_4090_LEASE, job_id):
            await asyncio.sleep(self.poll_interval_seconds)
            return
        initial_phase = initial_job.get("phase")
        if initial_phase == "callback_delivered":
            outcome = str(initial_job.get("result") or "failed")
            terminal = outcome if outcome in TERMINAL_STATUSES else "failed"
            self.database.finish_video_job_with_audit(
                job_id, terminal, outcome, initial_job.get("output_path"),
                initial_job.get("error_code"), initial_job.get("error_summary"), GPU_4090_LEASE,
            )
            return
        if initial_phase == "callback_pending":
            await self._deliver_callback_and_finish(
                job_id, str(initial_job.get("result") or "failed"),
                initial_job.get("output_path"), initial_job.get("error_code"),
                initial_job.get("error_summary"),
            )
            return
        if initial_phase in {"restoring_code", "verifying_ninfer"}:
            await self._resume_cleanup(initial_job)
            return
        if initial_phase == "collecting_output" and initial_job.get("output_path") \
                and Path(initial_job["output_path"]).is_file():
            self.database.update_video_job(job_id, result="succeeded")
            resumed = self.database.get_video_job(job_id, include_secret=True)
            if resumed is None:
                raise DatabaseError("视频任务在输出恢复时消失")
            await self._resume_cleanup(resumed)
            return
        outcome = "failed"
        error_code: str | None = None
        error_summary: str | None = None
        output_path: str | None = initial_job.get("output_path")
        try:
            started_at = initial_job.get("started_at") or utc_now()
            self.database.update_video_job(
                job_id, status="waiting_for_ninfer_idle", phase="waiting_for_ninfer_idle",
                started_at=started_at,
            )
            prompt_id = initial_job.get("prompt_id")
            if prompt_id is None and initial_phase == "submitting":
                prompt_id = await asyncio.to_thread(self.comfy.recover_prompt_id, job_id)
                if prompt_id is None:
                    raise VideoJobError(
                        "submission_state_unknown",
                        "AXIS 在提交 ComfyUI 时重启且无法确认 prompt_id，拒绝重复提交",
                    )
                self.database.update_video_job(job_id, prompt_id=prompt_id)
            if prompt_id is None:
                await self._wait_for_ninfer_idle(job_id)
                self.database.update_video_job(
                    job_id, status="switching_to_video", phase="switching_to_video", progress=None
                )
                await self._activate_scene("video_gen", job_id)
                if await self._cancel_requested(job_id):
                    raise VideoJobError("cancelled", "用户在场景切换后取消了视频任务")
                self.database.update_video_job(job_id, status="checking_comfy", phase="checking_comfy")
                await asyncio.to_thread(self.comfy.health)
                if await self._cancel_requested(job_id):
                    raise VideoJobError("cancelled", "用户在 ComfyUI 提交前取消了视频任务")
                try:
                    workflow = json.loads(initial_job["workflow_json"])
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise VideoJobError("workflow_snapshot_invalid", "持久化工作流快照损坏") from exc
                self.database.update_video_job(job_id, status="submitting", phase="submitting")
                prompt_id = await asyncio.to_thread(self.comfy.submit, workflow, job_id)
                self.database.update_video_job(
                    job_id, status="running", phase="monitoring_comfy", prompt_id=prompt_id
                )
            else:
                self.database.update_video_job(
                    job_id, status="running", phase="restart_monitoring_comfy", prompt_id=prompt_id
                )
            record = await self._monitor_prompt(job_id, prompt_id)
            if await self._cancel_requested(job_id):
                raise VideoJobError("cancelled", "用户在输出收集前取消了视频任务")
            descriptor = self.comfy.output_descriptor(record)
            target = self._target_path(initial_job, descriptor["filename"])
            if target.exists():
                raise VideoJobError("output_exists", f"输出路径已存在，拒绝覆盖: {target}")
            output_path = str(target)
            self.database.update_video_job(
                job_id, status="collecting_output", phase="collecting_output",
                output_path=output_path,
            )
            await asyncio.to_thread(self.comfy.download_output, descriptor, target)
            outcome = "cancelled" if await self._cancel_requested(job_id) else "succeeded"
        except asyncio.CancelledError:
            raise
        except VideoJobError as exc:
            outcome = "cancelled" if exc.code == "cancelled" else "failed"
            error_code = exc.code
            error_summary = str(exc)
        try:
            self.database.update_video_job(
                job_id, status="restoring_code", phase="restoring_code",
                output_path=output_path, result=outcome,
                error_code=error_code, error_summary=error_summary,
            )
            await self._activate_scene("code_agent", job_id)
            self.database.update_video_job(
                job_id, status="verifying_ninfer", phase="verifying_ninfer"
            )
            await asyncio.to_thread(self.ninfer.verify)
        except asyncio.CancelledError:
            raise
        except (VideoJobError, RegistryError, DatabaseError) as exc:
            restore_code = exc.code if isinstance(exc, VideoJobError) else "code_restore_failed"
            restore_message = str(exc)
            outcome = "failed"
            error_code = error_code or restore_code
            error_summary = "; ".join(filter(None, [error_summary, restore_message]))

        await self._deliver_callback_and_finish(
            job_id, outcome, output_path, error_code, error_summary
        )
