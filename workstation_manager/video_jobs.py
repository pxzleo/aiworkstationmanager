from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import os
import re
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from .database import Database, DatabaseError, utc_now
from .registry import RegisteredServiceManager, RegistryError


GPU_4090_LEASE = "gpu:4090"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
METRIC_RE = re.compile(r"^(?:llamacpp|ninfer):requests_(processing|deferred)\s+([0-9]+(?:\.[0-9]+)?)$")
MAX_VIDEO_BYTES = 16 * 1024 * 1024 * 1024
MIN_AVAILABLE_MEMORY_BYTES = 24 * 1024 * 1024 * 1024
MIN_COMMIT_HEADROOM_BYTES = 48 * 1024 * 1024 * 1024


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
    def _progress_event(message: str, prompt_id: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            return None
        data = payload["data"]
        event_prompt_id = data.get("prompt_id")
        if event_prompt_id is None or str(event_prompt_id) != prompt_id:
            return None
        event_type = str(payload.get("type") or "")
        node_id = str(data["node"]) if data.get("node") is not None else None
        if event_type == "progress":
            value = data.get("value")
            maximum = data.get("max")
            if isinstance(value, bool) or isinstance(maximum, bool) \
                    or not isinstance(value, (int, float)) \
                    or not isinstance(maximum, (int, float)) \
                    or isinstance(value, float) and not math.isfinite(value) \
                    or isinstance(maximum, float) and not math.isfinite(maximum) \
                    or not 0 <= value <= maximum <= 1_000_000_000 \
                    or maximum <= 0:
                return None
            return {
                "available": True, "kind": "sampling", "node_id": node_id,
                "value": value, "max": maximum,
                "percent": round(value / maximum * 100, 1),
            }
        if event_type == "executing" and node_id is not None:
            return {"available": True, "kind": "node", "node_id": node_id}
        return None

    def _websocket_url(self, job_id: str) -> str:
        parsed = urlsplit(self.http.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        client_id = quote(f"axis-{job_id}", safe="")
        return f"{scheme}://{parsed.netloc}/ws?clientId={client_id}"

    async def connect_progress(self, job_id: str):
        return await websocket_connect(
            self._websocket_url(job_id), open_timeout=self.http.timeout_seconds,
            close_timeout=2, ping_interval=20, ping_timeout=20,
            max_size=1024 * 1024,
        )

    async def progress_events(self, job_id: str, prompt_id: str, connection=None):
        pending_connection = connection
        while True:
            try:
                websocket = pending_connection or await self.connect_progress(job_id)
                pending_connection = None
                try:
                    yield {"available": True, "kind": "connected"}
                    async for message in websocket:
                        if not isinstance(message, str):
                            continue
                        event = self._progress_event(message, prompt_id)
                        if event is not None:
                            yield event
                finally:
                    await websocket.close()
                yield {"available": False, "kind": "disconnected"}
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, InvalidHandshake, InvalidURI, OSError, TimeoutError):
                yield {"available": False, "kind": "disconnected"}
                await asyncio.sleep(1)

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

    def release_memory(self) -> None:
        self.http.request(
            "POST", "/free", {"unload_models": True, "free_memory": True},
            expected=(200, 204),
        )

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
        resource_snapshot: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.database = database
        self.registry = registry
        self.comfy = comfy or ComfyUIClient(comfyui_base_url)
        self.ninfer = ninfer or NInferClient(ninfer_base_url, ninfer_model_id)
        self.callback = callback or OpenCodeCallbackClient()
        self.resource_snapshot = resource_snapshot
        self.output_directory = Path(output_directory)
        self.poll_interval_seconds = poll_interval_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.scene_timeout_seconds = scene_timeout_seconds
        self.generation_timeout_seconds = generation_timeout_seconds
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self.last_error: str | None = None

    def _generation_scene_active(self, generation_scene_id: str) -> bool:
        if not generation_scene_id:
            return False
        active = self.registry.active_scene()
        return active is not None and active["id"] == generation_scene_id

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

    def _prepare_submission(
        self, payload: dict[str, Any], *, idempotency_key: str,
        batch_id: str | None = None, batch_index: int | None = None,
        batch_size: int | None = None,
    ) -> tuple[dict[str, Any], str]:
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
        self._validate_workflow_safety(workflow)
        workflow_json = json.dumps(workflow, ensure_ascii=False, separators=(",", ":"))
        workflow_sha256 = hashlib.sha256(workflow_json.encode("utf-8")).hexdigest()
        output_text = str(payload.get("output_path") or "").strip()
        if output_text and not Path(output_text).is_absolute():
            raise VideoJobError("invalid_output_path", "output_path 必须是绝对路径")
        callback_url = self._loopback_url(str(payload.get("callback_url") or ""), "callback_url")
        callback_directory = str(payload.get("callback_directory") or "").strip() or None
        if callback_directory and not Path(callback_directory).is_absolute():
            raise VideoJobError("invalid_callback_directory", "callback_directory 必须是绝对路径")
        scene_name = str(payload.get("scene_name") or "").strip()
        generation_scene = (
            self.database.get_scene_by_name(scene_name)
            if scene_name else self.database.get_default_generation_scene()
        )
        if generation_scene is None:
            if scene_name:
                raise VideoJobError(
                    "generation_scene_not_found", f"指定的生成场景不存在: {scene_name}"
                )
            raise VideoJobError(
                "default_generation_scene_missing", "尚未设置默认生成场景"
            )
        canonical = {
            "session_id": session_id, "workflow_path": str(workflow_path.resolve()),
            "output_path": output_text, "callback_url": callback_url,
            "callback_directory": callback_directory,
            "generation_scene_id": generation_scene["id"],
            "workflow_sha256": workflow_sha256,
        }
        payload_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        item = {
            "id": uuid.uuid4().hex, "idempotency_key": idempotency_key,
            "payload_hash": payload_hash, "session_id": session_id,
            "workflow_path": canonical["workflow_path"],
            "workflow_json": workflow_json,
            "requested_output_path": output_text or None, "callback_url": callback_url,
            "callback_directory": callback_directory,
            "generation_scene_id": generation_scene["id"],
            "generation_scene_name": generation_scene["name"],
            "batch_id": batch_id, "batch_index": batch_index, "batch_size": batch_size,
        }
        return item, payload_hash

    def submit(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        item_data, payload_hash = self._prepare_submission(
            payload, idempotency_key=idempotency_key,
        )
        item, created = self.database.create_video_job(item_data)
        if item["payload_hash"] != payload_hash:
            raise VideoJobError(
                "idempotency_conflict", "相同 idempotency_key 已用于不同的视频任务参数"
            )
        if created:
            self.database.append_audit(
                "local", "management.video_job.submit", "success",
                {"job_id": item["id"], "session_id": item_data["session_id"],
                 "idempotency_key": idempotency_key},
            )
            self._wake.set()
        return item, created

    def submit_batch(self, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        batch_key = str(payload.get("idempotency_key") or "").strip()
        workflows = payload.get("workflows")
        if not isinstance(workflows, list) or not 1 <= len(workflows) <= 100:
            raise VideoJobError("invalid_batch", "workflows 必须包含 1..100 个有序工作流")
        batch_id = hashlib.sha256(f"axis-video-batch:{batch_key}".encode("utf-8")).hexdigest()[:32]
        segment_key_prefix = hashlib.sha256(batch_key.encode("utf-8")).hexdigest()
        items: list[dict[str, Any]] = []
        hashes: list[str] = []
        for offset, workflow in enumerate(workflows):
            if not isinstance(workflow, dict):
                raise VideoJobError("invalid_batch", "每个批次工作流必须是对象")
            segment_payload = {
                **payload,
                "workflow_path": workflow.get("workflow_path"),
                "output_path": workflow.get("output_path"),
            }
            segment_payload.pop("workflows", None)
            item, payload_hash = self._prepare_submission(
                segment_payload, idempotency_key=f"batch:{segment_key_prefix}:{offset + 1}",
                batch_id=batch_id, batch_index=offset + 1, batch_size=len(workflows),
            )
            items.append(item)
            hashes.append(payload_hash)
        jobs, created = self.database.create_video_job_batch(items)
        if len(jobs) != len(items) or any(
            job["payload_hash"] != hashes[index] for index, job in enumerate(jobs)
        ):
            raise VideoJobError(
                "idempotency_conflict", "相同 idempotency_key 已用于不同的视频任务批次参数"
            )
        if created:
            self.database.append_audit(
                "local", "management.video_job_batch.submit", "success",
                {"batch_id": batch_id, "session_id": items[0]["session_id"],
                 "segment_count": len(items), "idempotency_key": batch_key},
            )
            self._wake.set()
        return jobs, created

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

    async def _activate_scene(
        self, scene_id: str, job_id: str, missing_code: str, missing_message: str,
    ) -> None:
        scene = self.database.get_scene(scene_id)
        if scene is None:
            raise VideoJobError(missing_code, missing_message)
        if self.registry._scene_with_state(scene)["state"] == "active":
            self.database.set_last_activated_scene(scene_id)
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

    async def _wait_for_registry_idle(self, job_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.scene_timeout_seconds
        while self.registry._operation_pending or self.database.has_active_operation():
            if await self._cancel_requested(job_id):
                raise VideoJobError("cancelled", "用户在等待现有场景操作完成时取消了视频任务")
            if asyncio.get_running_loop().time() >= deadline:
                raise VideoJobError("operation_busy", "等待现有服务或场景操作完成超时")
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

    @staticmethod
    def _validate_workflow_safety(workflow: dict[str, Any]) -> None:
        class_types = [
            str(node.get("class_type") or "")
            for node in workflow.values() if isinstance(node, dict)
        ]
        if not any(class_type.startswith("MiniMaxH3") for class_type in class_types):
            return
        h3_conditioning = sum(
            class_type.startswith("MiniMaxH3") and class_type.endswith("ToVideo")
            for class_type in class_types
        )
        h3_native_samplers = sum(
            class_type.startswith("MiniMaxH3") and "Sampler" in class_type
            for class_type in class_types
        )
        advanced_samplers = class_types.count("SamplerCustomAdvanced")
        if max(h3_conditioning, h3_native_samplers, advanced_samplers) > 1:
            raise VideoJobError(
                "h3_multi_segment_workflow",
                "一个 H3 工作流只允许一个生成分支；请将各视频段拆成独立任务，由 AXIS 串行执行",
            )

    @staticmethod
    def _workflow_node_names(workflow_json: str) -> dict[str, str]:
        try:
            workflow = json.loads(workflow_json)
        except (TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(workflow, dict):
            return {}
        names: dict[str, str] = {}
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            meta = node.get("_meta")
            title = meta.get("title") if isinstance(meta, dict) else None
            names[str(node_id)] = str(title or node.get("class_type") or node_id)
        return names

    def _check_resource_headroom(self) -> None:
        if self.resource_snapshot is None:
            raise VideoJobError(
                "resource_metrics_unavailable", "未配置主机资源快照，拒绝提交 H3 工作流"
            )
        try:
            snapshot = self.resource_snapshot()
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError) as exc:
            raise VideoJobError(
                "resource_metrics_unavailable", f"读取主机资源快照失败: {exc}"
            ) from exc
        if not isinstance(snapshot, dict):
            raise VideoJobError(
                "resource_metrics_unavailable", "主机资源快照不可用，拒绝提交 H3 工作流"
            )
        stale_collectors = snapshot.get("stale_collectors", {})
        if not isinstance(stale_collectors, dict) or "snapshot" in stale_collectors:
            raise VideoJobError(
                "resource_metrics_unavailable", "主机资源快照已经过期，拒绝提交 H3 工作流"
            )
        host = snapshot.get("host")
        memory = host.get("memory") if isinstance(host, dict) else None
        if not isinstance(memory, dict):
            raise VideoJobError(
                "resource_metrics_unavailable", "主机内存资源快照格式无效，拒绝提交 H3 工作流"
            )
        available = memory.get("available_bytes")
        commit_used = memory.get("commit_used_bytes")
        commit_limit = memory.get("commit_limit_bytes")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (
            available, commit_used, commit_limit,
        )):
            raise VideoJobError(
                "resource_metrics_unavailable", "主机内存或提交量指标缺失，拒绝提交 H3 工作流"
            )
        if available < MIN_AVAILABLE_MEMORY_BYTES:
            raise VideoJobError(
                "insufficient_available_memory",
                f"可用物理内存不足 24 GiB（当前 {available / 1024 ** 3:.1f} GiB），拒绝提交 H3 工作流",
            )
        commit_headroom = commit_limit - commit_used
        if commit_headroom < MIN_COMMIT_HEADROOM_BYTES:
            raise VideoJobError(
                "insufficient_commit_headroom",
                f"系统提交余量不足 48 GiB（当前 {commit_headroom / 1024 ** 3:.1f} GiB），拒绝提交 H3 工作流",
            )

    async def _wait_for_resource_headroom(self, job_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.idle_timeout_seconds
        while True:
            if await self._cancel_requested(job_id):
                raise VideoJobError("cancelled", "用户在等待内存释放时取消了视频任务")
            try:
                self._check_resource_headroom()
                return
            except VideoJobError as exc:
                if exc.code not in {"insufficient_available_memory", "insufficient_commit_headroom"}:
                    raise
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                self.database.update_video_job(
                    job_id, status="waiting_for_memory", phase="waiting_for_memory",
                )
                await asyncio.sleep(self.poll_interval_seconds)

    def _target_path(self, job: dict[str, Any], filename: str) -> Path:
        requested = job.get("requested_output_path")
        if not requested:
            return self.output_directory / job["id"] / Path(filename).name
        path = Path(requested)
        if path.exists() and path.is_dir() or not path.suffix:
            return path / Path(filename).name
        return path

    async def _monitor_prompt(
        self, job_id: str, prompt_id: str, node_names: dict[str, str],
        progress_connection=None,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.generation_timeout_seconds
        realtime: dict[str, Any] = {
            "available": False, "kind": "connecting", "updated_at": utc_now(),
        }
        last_status: dict[str, Any] = {"state": "running"}
        events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        progress_events = getattr(self.comfy, "progress_events", None)

        async def listen() -> None:
            if not callable(progress_events):
                await events.put({"available": False, "kind": "unsupported"})
                return
            try:
                args = (job_id, prompt_id, progress_connection) \
                    if progress_connection is not None else (job_id, prompt_id)
                async for event in progress_events(*args):
                    if events.full():
                        with suppress(asyncio.QueueEmpty):
                            events.get_nowait()
                    events.put_nowait(event)
            except asyncio.CancelledError:
                raise
            except (VideoJobError, OSError, ValueError, TypeError):
                if events.full():
                    with suppress(asyncio.QueueEmpty):
                        events.get_nowait()
                events.put_nowait({"available": False, "kind": "disconnected"})

        listener = asyncio.create_task(listen())
        loop = asyncio.get_running_loop()
        next_poll = loop.time()
        last_realtime_write = 0.0
        try:
            while True:
                if await self._cancel_requested(job_id):
                    await asyncio.to_thread(self.comfy.cancel, prompt_id)
                    raise VideoJobError("cancelled", "用户取消了视频任务")
                now = loop.time()
                if now >= next_poll:
                    status = await asyncio.to_thread(self.comfy.status, prompt_id)
                    if status["state"] == "completed":
                        self.database.update_video_job(
                            job_id, progress={"state": "completed", "realtime": realtime},
                        )
                        return status["record"]
                    last_status = status
                    self.database.update_video_job(
                        job_id, progress={**last_status, "realtime": realtime},
                    )
                    next_poll = loop.time() + self.poll_interval_seconds
                if loop.time() >= deadline:
                    await asyncio.to_thread(self.comfy.cancel, prompt_id)
                    raise VideoJobError("generation_timeout", "ComfyUI 视频生成超时并已请求取消")
                wait_seconds = max(0.01, min(next_poll - loop.time(), deadline - loop.time()))
                try:
                    event = await asyncio.wait_for(events.get(), timeout=wait_seconds)
                except asyncio.TimeoutError:
                    continue
                node_id = event.get("node_id")
                if node_id is not None:
                    event["node_name"] = node_names.get(str(node_id), str(node_id))
                previous = {key: value for key, value in realtime.items() if key != "updated_at"}
                changed = event != previous
                availability_changed = event.get("available") != realtime.get("available")
                realtime = {**event, "updated_at": utc_now()}
                should_write = changed and (
                    availability_changed or event.get("value") == event.get("max")
                    or loop.time() - last_realtime_write >= 0.25
                )
                if should_write:
                    self.database.update_video_job(
                        job_id, progress={**last_status, "realtime": realtime},
                    )
                    last_realtime_write = loop.time()
        finally:
            listener.cancel()
            with suppress(asyncio.CancelledError):
                await listener

    async def _submit_and_monitor_prompt(
        self, job_id: str, workflow: dict[str, Any], node_names: dict[str, str],
    ) -> tuple[str, dict[str, Any]]:
        progress_connection = None
        try:
            connect_progress = getattr(self.comfy, "connect_progress", None)
            if callable(connect_progress):
                try:
                    progress_connection = await connect_progress(job_id)
                except (ConnectionClosed, InvalidHandshake, InvalidURI, OSError, TimeoutError):
                    progress_connection = None
            prompt_id = await asyncio.to_thread(self.comfy.submit, workflow, job_id)
            self.database.update_video_job(
                job_id, status="running", phase="monitoring_comfy", prompt_id=prompt_id,
            )
            record = await self._monitor_prompt(
                job_id, prompt_id, node_names, progress_connection,
            )
            return prompt_id, record
        finally:
            if progress_connection is not None:
                with suppress(ConnectionClosed, OSError, TimeoutError):
                    await progress_connection.close()

    async def _deliver_callback_and_finish(
        self, job_id: str, outcome: str, output_path: str | None,
        error_code: str | None, error_summary: str | None,
    ) -> None:
        job = self.database.get_video_job(job_id, include_internal=True)
        if job is None:
            raise DatabaseError("视频任务在回调前消失")
        original_scene_name = str(job.get("original_scene_name") or "原场景")
        def callback_message() -> str:
            batch_id = str(job.get("batch_id") or "")
            if batch_id:
                batch = self.database.video_jobs_in_batch(batch_id)
                outputs = [item["output_path"] for item in batch if item.get("output_path")]
                if outcome == "succeeded":
                    return (
                        f"AXIS 视频批次 {batch_id} 的 {len(batch)} 段已全部完成，"
                        f"输出文件：{'；'.join(outputs)}。已恢复场景 {original_scene_name}，"
                        "请继续原任务。"
                    )
                index = int(job.get("batch_index") or 1)
                if outcome == "cancelled":
                    return (
                        f"AXIS 视频批次 {batch_id} 已在第 {index} / {len(batch)} 段取消，"
                        f"后续段已终止，已尝试恢复场景 {original_scene_name}，请继续原任务。"
                    )
                return (
                    f"AXIS 视频批次 {batch_id} 第 {index} / {len(batch)} 段失败："
                    f"{error_code}: {error_summary}。后续段已终止，已尝试恢复场景 "
                    f"{original_scene_name}，请继续处理失败。"
                )
            if outcome == "succeeded":
                return (
                    f"AXIS 视频任务 {job_id} 已完成，输出文件：{output_path}。"
                    f"已恢复场景 {original_scene_name}，请继续原任务。"
                )
            if outcome == "cancelled":
                return (
                    f"AXIS 视频任务 {job_id} 已取消，已恢复场景 "
                    f"{original_scene_name}。请继续原任务。"
                )
            return (
                f"AXIS 视频任务 {job_id} 失败：{error_code}: {error_summary}。"
                f"已尝试恢复场景 {original_scene_name}，请继续处理失败。"
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
            original_scene_id = str(job.get("original_scene_id") or "")
            if not original_scene_id:
                raise VideoJobError("original_scene_missing", "视频任务没有可恢复的原场景")
            await self._activate_scene(
                original_scene_id, job["id"], "original_scene_missing",
                "视频任务的原场景已不存在，无法恢复",
            )
        except asyncio.CancelledError:
            raise
        except (VideoJobError, RegistryError, DatabaseError) as exc:
            outcome = "failed"
            error_code = error_code or (
                exc.code if isinstance(exc, VideoJobError) else "scene_restore_failed"
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
        if initial_phase == "scene_held":
            initial_phase = "restoring_scene"
        if initial_phase in {"restoring_code", "restoring_scene", "verifying_ninfer"}:
            await self._resume_cleanup(initial_job)
            return
        if initial_phase == "collecting_output" and initial_job.get("output_path") \
                and Path(initial_job["output_path"]).is_file():
            outcome = "cancelled" if await self._cancel_requested(job_id) else "succeeded"
            error_code = None
            error_summary = None
            try:
                await asyncio.to_thread(self.comfy.release_memory)
            except VideoJobError as exc:
                outcome = "failed"
                error_code = exc.code
                error_summary = str(exc)
            self.database.update_video_job(
                job_id, result=outcome, error_code=error_code, error_summary=error_summary,
            )
            resumed = self.database.get_video_job(job_id, include_internal=True)
            if resumed is None:
                raise DatabaseError("视频任务在输出恢复时消失")
            if resumed.get("batch_id") and int(resumed.get("batch_index") or 1) \
                    < int(resumed.get("batch_size") or 1) and outcome == "succeeded":
                self.database.finish_video_job_with_audit(
                    job_id, "succeeded", "succeeded", resumed.get("output_path"),
                    None, None, GPU_4090_LEASE,
                )
                return
            await self._resume_cleanup(resumed)
            return
        outcome = "failed"
        error_code: str | None = None
        error_summary: str | None = None
        output_path: str | None = initial_job.get("output_path")
        keep_scene = False
        generation_scene_id = str(initial_job.get("generation_scene_id") or "")
        try:
            if not initial_job.get("original_scene_id"):
                batch_id = str(initial_job.get("batch_id") or "")
                batch_index = int(initial_job.get("batch_index") or 1)
                if batch_id and batch_index > 1:
                    batch = self.database.video_jobs_in_batch(batch_id)
                    first = batch[0] if batch else None
                    if first is None or not first.get("original_scene_id"):
                        raise VideoJobError(
                            "batch_anchor_missing", "视频批次缺少首段记录的原场景"
                        )
                    anchor = {
                        "id": str(first["original_scene_id"]),
                        "name": str(first.get("original_scene_name") or "原场景"),
                    }
                else:
                    await self._wait_for_registry_idle(job_id)
                    original_scene = self.registry.active_scene()
                    if original_scene is None:
                        raise VideoJobError(
                            "active_scene_missing", "当前没有已记录且完整激活的场景，无法在生成后恢复"
                        )
                    anchor = original_scene
                self.database.update_video_job(
                    job_id, original_scene_id=anchor["id"],
                    original_scene_name=anchor["name"],
                )
                initial_job = {
                    **initial_job,
                    "original_scene_id": anchor["id"],
                    "original_scene_name": anchor["name"],
                }
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
                if not generation_scene_id:
                    raise VideoJobError(
                        "generation_scene_not_found", "视频任务没有可用的生成场景"
                    )
                scene_already_active = self._generation_scene_active(generation_scene_id)
                continuing_video_scene = bool(
                    scene_already_active and initial_job.get("original_scene_id")
                    and str(initial_job.get("original_scene_id")) != generation_scene_id
                )
                if not continuing_video_scene:
                    await self._wait_for_ninfer_idle(job_id)
                if not scene_already_active:
                    self.database.update_video_job(
                        job_id, status="switching_to_video", phase="switching_to_video", progress=None
                    )
                    await self._activate_scene(
                        generation_scene_id, job_id,
                        "generation_scene_not_found", "视频任务指定的生成场景已不存在",
                    )
                    if await self._cancel_requested(job_id):
                        raise VideoJobError("cancelled", "用户在场景切换后取消了视频任务")
                self.database.update_video_job(job_id, status="checking_comfy", phase="checking_comfy")
                await asyncio.to_thread(self.comfy.health)
                await self._wait_for_resource_headroom(job_id)
                if await self._cancel_requested(job_id):
                    raise VideoJobError("cancelled", "用户在 ComfyUI 提交前取消了视频任务")
                try:
                    workflow = json.loads(initial_job["workflow_json"])
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise VideoJobError("workflow_snapshot_invalid", "持久化工作流快照损坏") from exc
                if not isinstance(workflow, dict) or not workflow:
                    raise VideoJobError("workflow_snapshot_invalid", "持久化工作流快照格式无效")
                self._validate_workflow_safety(workflow)
                self.database.update_video_job(job_id, status="submitting", phase="submitting")
                node_names = self._workflow_node_names(initial_job.get("workflow_json", ""))
                prompt_id, record = await self._submit_and_monitor_prompt(
                    job_id, workflow, node_names,
                )
            else:
                self.database.update_video_job(
                    job_id, status="running", phase="restart_monitoring_comfy", prompt_id=prompt_id
                )
                node_names = self._workflow_node_names(initial_job.get("workflow_json", ""))
                record = await self._monitor_prompt(job_id, prompt_id, node_names)
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
            keep_scene = (
                outcome == "succeeded"
                and self._generation_scene_active(generation_scene_id)
                and bool(initial_job.get("batch_id"))
                and int(initial_job.get("batch_index") or 1)
                    < int(initial_job.get("batch_size") or 1)
            )
            await asyncio.to_thread(self.comfy.release_memory)
        except asyncio.CancelledError:
            raise
        except VideoJobError as exc:
            outcome = "cancelled" if exc.code == "cancelled" else "failed"
            error_code = exc.code
            error_summary = str(exc)
        keep_scene = keep_scene and outcome == "succeeded"
        if outcome != "succeeded" and initial_job.get("batch_id"):
            self.database.abort_remaining_batch_jobs(
                str(initial_job["batch_id"]), int(initial_job.get("batch_index") or 1),
                f"批次在第 {int(initial_job.get('batch_index') or 1)} 段终止："
                f"{error_code}: {error_summary}",
            )
            try:
                await asyncio.to_thread(self.comfy.release_memory)
            except VideoJobError as exc:
                error_code = error_code or exc.code
                error_summary = "; ".join(filter(None, [error_summary, str(exc)]))
        try:
            self.database.update_video_job(
                job_id, status="restoring_scene",
                phase="batch_waiting" if keep_scene else "restoring_scene",
                output_path=output_path, result=outcome,
                error_code=error_code, error_summary=error_summary,
            )
            if keep_scene:
                self.database.finish_video_job_with_audit(
                    job_id, "succeeded", "succeeded", output_path, None, None, GPU_4090_LEASE,
                )
                return
            else:
                original_scene_id = str(initial_job.get("original_scene_id") or "")
                if not original_scene_id:
                    raise VideoJobError("original_scene_missing", "视频任务没有可恢复的原场景")
                await self._activate_scene(
                    original_scene_id, job_id, "original_scene_missing",
                    "视频任务的原场景已不存在，无法恢复",
                )
        except asyncio.CancelledError:
            raise
        except (VideoJobError, RegistryError, DatabaseError) as exc:
            restore_code = exc.code if isinstance(exc, VideoJobError) else "scene_restore_failed"
            restore_message = str(exc)
            outcome = "failed"
            error_code = error_code or restore_code
            error_summary = "; ".join(filter(None, [error_summary, restore_message]))

        await self._deliver_callback_and_finish(
            job_id, outcome, output_path, error_code, error_summary,
        )
