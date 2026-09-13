from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError, URLError

from fastapi.testclient import TestClient

from workstation_manager.app import create_app
from workstation_manager.config import Settings
from workstation_manager.database import Database, DatabaseError
from workstation_manager.history import Sampler
from workstation_manager.registry import RegisteredServiceManager, RegistryError
from workstation_manager.video_jobs import (
    ComfyUIClient,
    NInferClient,
    OpenCodeCallbackClient,
    VideoJobError,
    VideoJobManager,
)


class IdleNInfer:
    def __init__(self, activities: list[dict] | None = None) -> None:
        self.activities = list(activities or [{
            "processing": 0, "deferred": 0, "active_slots": [], "idle": True,
        }])
        self.verify_calls = 0

    def activity(self) -> dict:
        if len(self.activities) > 1:
            return self.activities.pop(0)
        return self.activities[0]

    def verify(self) -> None:
        self.verify_calls += 1


class CompletedComfy:
    def __init__(self) -> None:
        self.submit_calls = 0
        self.release_calls = 0
        self.recovered_prompt_id: str | None = None
        self.cancel_calls: list[str] = []

    def health(self) -> None:
        return None

    def submit(self, workflow: dict, job_id: str) -> str:
        self.submit_calls += 1
        self.last_workflow = workflow
        self.last_job_id = job_id
        return "prompt-1"

    def recover_prompt_id(self, job_id: str) -> str | None:
        return self.recovered_prompt_id

    def status(self, prompt_id: str) -> dict:
        return {"state": "completed", "record": {"outputs": {
            "12": {"videos": [{"filename": "result.mp4", "subfolder": "", "type": "output"}]}
        }}}

    def output_descriptor(self, record: dict) -> dict[str, str]:
        return {"filename": "result.mp4", "subfolder": "", "type": "output"}

    def download_output(self, descriptor: dict[str, str], target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video")

    def cancel(self, prompt_id: str) -> None:
        self.cancel_calls.append(prompt_id)

    def release_memory(self) -> None:
        self.release_calls += 1


class RealtimeComfy(CompletedComfy):
    def __init__(self) -> None:
        super().__init__()
        self.status_calls = 0

    def status(self, prompt_id: str) -> dict:
        self.status_calls += 1
        if self.status_calls == 1:
            return {"state": "running", "queue_position": 0}
        return super().status(prompt_id)

    async def progress_events(self, job_id: str, prompt_id: str):
        yield {
            "available": True, "kind": "sampling", "node_id": "3",
            "value": 5, "max": 8, "percent": 62.5,
        }
        await asyncio.Future()


class RecordingProgressConnection:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class PreconnectedComfy(CompletedComfy):
    def __init__(self) -> None:
        super().__init__()
        self.connection = RecordingProgressConnection()

    async def connect_progress(self, job_id: str):
        return self.connection


class RecordingCallback:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.handoff_checks = 0

    def handoff_ready(self, job: dict) -> bool:
        self.handoff_checks += 1
        return True

    def send(self, job: dict, message: str) -> None:
        self.messages.append(message)


class WaitingHandoffCallback(RecordingCallback):
    def __init__(self, ready_after: int) -> None:
        super().__init__()
        self.ready_after = ready_after

    def handoff_ready(self, job: dict) -> bool:
        self.handoff_checks += 1
        return self.handoff_checks >= self.ready_after


class UnreachableHandoffCallback(RecordingCallback):
    def handoff_ready(self, job: dict) -> bool:
        self.handoff_checks += 1
        raise VideoJobError(
            "opencode_handoff_unreachable", "OpenCode 空闲握手端点不可达: connection refused",
        )


class HttpResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class CountingNInfer:
    def __init__(self) -> None:
        self.calls = 0

    def activity(self) -> dict:
        self.calls += 1
        return {"processing": 0, "deferred": 0, "active_slots": [], "idle": True}

    def verify(self) -> None:
        return None


class FailingCallback:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, job: dict, message: str) -> None:
        self.calls += 1
        raise VideoJobError("opencode_callback_failed", "temporary failure")


class CancelDuringCallback:
    def __init__(self) -> None:
        self.manager: VideoJobManager | None = None
        self.job_id = ""
        self.messages: list[str] = []

    def send(self, job: dict, message: str) -> None:
        self.messages.append(message)
        if len(self.messages) == 1:
            assert self.manager is not None
            try:
                self.manager.cancel(self.job_id, "admin", "local")
            except VideoJobError as exc:
                self.cancel_error = exc


class FakeHttp:
    def json(self, method: str, path: str, body=None):
        if path == "/slots":
            return [{"id": 3, "is_processing": True, "state": "decode"}]
        raise AssertionError(path)

    def request(self, method: str, path: str, body=None):
        if path == "/metrics":
            return 200, b"llamacpp:requests_processing 1\nllamacpp:requests_deferred 2\n", "text/plain"
        raise AssertionError(path)


class CompletedWithoutOutputHttp:
    def json(self, method: str, path: str, body=None):
        if path == "/history/prompt-empty":
            return {"prompt-empty": {
                "status": {"status_str": "success", "completed": True},
                "outputs": {},
            }}
        if path == "/queue":
            return {"queue_running": [], "queue_pending": []}
        raise AssertionError(path)


class QueueHttp:
    def __init__(self, running: bool) -> None:
        self.running = running
        self.calls: list[tuple[str, str]] = []

    def json(self, method: str, path: str, body=None):
        if path == "/queue":
            entry = [0, "prompt-1"]
            return {
                "queue_running": [entry] if self.running else [],
                "queue_pending": [] if self.running else [entry],
            }
        raise AssertionError(path)

    def request(self, method: str, path: str, body=None, expected=(200,)):
        self.calls.append((method, path))
        return 200, b"", "application/json"

class VideoJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "axis.db")
        self.registry = RegisteredServiceManager(self.database)
        self.database.create_scene({
            "id": "a" * 32, "name": "Code", "description": "",
            "service_ids": [],
        })
        self.database.create_scene({
            "id": "b" * 32, "name": "Video", "description": "",
            "is_default_generation": True,
            "service_ids": [],
        })
        self.database.set_last_activated_scene("a" * 32)
        self.workflow = self.root / "workflow.json"
        self.workflow.write_text(json.dumps({"1": {"class_type": "Test"}}), encoding="utf-8")
        self.shared_output_directory = self.root / "shared"
        self.shared_output_directory.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def manager(
        self, *, ninfer=None, comfy=None, callback=None, resource_snapshot=None,
    ) -> VideoJobManager:
        return VideoJobManager(
            self.database, self.registry, comfyui_base_url="http://127.0.0.1:8189",
            ninfer_base_url="http://127.0.0.1:8080", ninfer_model_id="qwen3.8-27b",
            output_directory=self.root / "outputs", poll_interval_seconds=0,
            shared_output_directory=self.shared_output_directory, file_service_port=18765,
            idle_timeout_seconds=1, scene_timeout_seconds=1, generation_timeout_seconds=1,
            ninfer=ninfer or IdleNInfer(), comfy=comfy or CompletedComfy(),
            callback=callback or RecordingCallback(),
            resource_snapshot=resource_snapshot or (lambda: {
                "host": {"memory": {
                    "available_bytes": 32 * 1024 ** 3,
                    "commit_used_bytes": 32 * 1024 ** 3,
                    "commit_limit_bytes": 128 * 1024 ** 3,
                }},
            }),
        )

    def payload(self, key: str = "job-key") -> dict:
        return {
            "idempotency_key": key, "session_id": "ses_test",
            "workflow_path": str(self.workflow), "output_path": None,
            "callback_url": "http://127.0.0.1:61714",
            "callback_directory": str(self.root),
        }

    def batch_payload(self, key: str = "batch-key", count: int = 3) -> dict:
        workflows = []
        for index in range(count):
            path = self.root / f"workflow-{index + 1}.json"
            path.write_text(json.dumps({str(index + 1): {"class_type": "Test"}}), encoding="utf-8")
            workflows.append({"workflow_path": str(path), "output_path": None})
        payload = self.payload(key)
        payload.pop("workflow_path")
        payload.pop("output_path")
        payload["workflows"] = workflows
        return payload

    def test_default_generation_scene_is_unique_and_user_selected(self) -> None:
        scene = self.database.get_default_generation_scene()
        self.assertEqual(scene["id"], "b" * 32)
        self.database.create_scene({
            "id": "c" * 32, "name": "Video 2", "description": "",
            "is_default_generation": True, "service_ids": [],
        })
        self.assertEqual(self.database.get_default_generation_scene()["id"], "c" * 32)
        self.assertEqual(self.database.get_scene("b" * 32)["is_default_generation"], 0)

    def test_submit_can_select_generation_scene_by_name(self) -> None:
        self.database.create_scene({
            "id": "c" * 32, "name": "Video 2", "description": "", "service_ids": [],
        })
        payload = self.payload("named-scene")
        payload["scene_name"] = "Video 2"
        job, created = self.manager().submit(payload)
        self.assertTrue(created)
        self.assertEqual(job["generation_scene_id"], "c" * 32)
        self.assertEqual(job["generation_scene_name"], "Video 2")

    def test_active_scene_uses_last_successful_selection_when_services_match(self) -> None:
        self.database.set_last_activated_scene("b" * 32)
        self.assertEqual(self.registry.active_scene()["id"], "b" * 32)

    def test_database_schema_contains_no_callback_authorization_column(self) -> None:
        with self.database.connect() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(video_jobs)")
            }
        self.assertNotIn("callback_authorization", columns)
        self.assertTrue({
            "batch_id", "batch_index", "batch_size", "shared_output_path",
        }.issubset(columns))

    def test_video_job_exposes_workflow_video_spec(self) -> None:
        self.workflow.write_text(json.dumps({
            "1": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
                "width": 768, "height": 1344, "length": 175,
            }},
            "2": {"class_type": "CreateVideo", "inputs": {"fps": 24.0}},
            "3": {"class_type": "BasicScheduler", "inputs": {"steps": 8}},
        }), encoding="utf-8")
        job, _ = self.manager().submit(self.payload("video-spec"))
        self.assertEqual(job["video_spec"], {
            "title": None,
            "width": 768, "height": 1344, "frames": 175,
            "fps": 24.0, "duration_seconds": 7.292, "steps": 8,
        })

    def test_video_job_exposes_t8_workflow_video_spec(self) -> None:
        self.workflow.write_text(json.dumps({
            "1": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {
                "width": 1280, "height": 720, "length": 241,
            }},
            "2": {"class_type": "VHS_VideoCombine", "inputs": {"frame_rate": 24}},
            "3": {"class_type": "MiniMaxH3DualClockSamplerT8", "inputs": {"steps": 16}},
        }), encoding="utf-8")
        job, _ = self.manager().submit(self.payload("video-spec-t8"))
        self.assertEqual(job["video_spec"], {
            "title": None,
            "width": 1280, "height": 720, "frames": 241,
            "fps": 24.0, "duration_seconds": 10.042, "steps": 16,
        })

    def test_video_spec_ignores_non_finite_and_out_of_range_values(self) -> None:
        spec = Database._video_spec(json.dumps({
            "1": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {
                "width": 10 ** 1000, "height": -1, "length": 1_000_000,
            }},
            "2": {"class_type": "CreateVideo", "inputs": {"fps": 5e-324}},
            "3": {"class_type": "BasicScheduler", "inputs": {"steps": 10001}},
        }))
        self.assertEqual(spec, {
            "title": None,
            "width": None, "height": None, "frames": 1_000_000,
            "fps": 5e-324, "duration_seconds": None, "steps": None,
        })

    def test_video_job_list_does_not_load_full_workflow_json(self) -> None:
        self.manager().submit(self.payload("list-without-workflow"))
        statements: list[str] = []
        original_connect = self.database.connect

        @contextmanager
        def traced_connect():
            with original_connect() as connection:
                connection.set_trace_callback(statements.append)
                yield connection

        with patch.object(self.database, "connect", traced_connect):
            jobs = self.database.list_video_jobs()
        select = next(statement for statement in statements if "FROM video_jobs" in statement)
        self.assertNotIn("workflow_json", select)
        self.assertNotIn("SELECT *", select)
        self.assertEqual(jobs[0]["video_spec"], {
            "title": None,
            "width": None, "height": None, "frames": None,
            "fps": None, "duration_seconds": None, "steps": None,
        })

    def test_video_title_prefers_source_filename_and_cleans_technical_affixes(self) -> None:
        workflow = {
            "1": {"class_type": "VHS_LoadVideoPath", "inputs": {
                "video": r"D:\共享\这又是谁的白月光_src.mp4",
            }},
            "2": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
                "prompt": "summary: This fallback should not be used.",
            }},
        }
        self.assertEqual(Database._video_title(workflow), "这又是谁的白月光")

    def test_video_title_uses_project_directory_for_generic_source_name(self) -> None:
        paths = (
            r"D:\共享\和这个夏天说再见吧_全裸_工作流交接\src\source.mp4",
            r"D:\共享\和这个夏天说再见吧_全裸_工作流交接\source_video.mp4",
            r"D:\共享\和这个夏天说再见吧_全裸_工作流交接\video-source.mp4",
            r"D:\共享\和这个夏天说再见吧_全裸_工作流交接\assets\source.mp4",
            r"D:\共享\和这个夏天说再见吧_全裸_工作流交接\素材\原视频.mp4",
        )
        for path in paths:
            with self.subTest(path=path):
                workflow = {
                    "1": {"class_type": "VHS_LoadVideoPath", "inputs": {"video": path}},
                }
                self.assertEqual(
                    Database._video_title(workflow), "和这个夏天说再见吧_全裸"
                )

    def test_video_title_falls_back_to_prompt_summary(self) -> None:
        workflow = {
            "1": {"class_type": "MiniMaxH3TextToVideo", "inputs": {
                "prompt": "subject_definitions:\nA dancer.\n\nsummary: A dancer crosses a snowy stage.\n",
            }},
        }
        self.assertEqual(Database._video_title(workflow), "A dancer crosses a snowy stage.")

    def test_schema_28_backfills_existing_video_titles(self) -> None:
        project = self.root / "雪场_全裸_工作流交接"
        project.mkdir()
        source = project / "源片.mp4"
        source.write_bytes(b"video")
        self.workflow.write_text(json.dumps({
            "1": {"class_type": "VHS_LoadVideoPath", "inputs": {
                "video": str(source),
            }},
        }), encoding="utf-8")
        job, _ = self.manager().submit(self.payload("title-backfill"))
        with self.database.connect() as connection:
            with connection:
                connection.execute(
                    "UPDATE video_jobs SET video_spec='{}' WHERE id=?", (job["id"],)
                )
                connection.execute("UPDATE schema_version SET version=27")

        migrated = Database(self.database.path)
        self.assertEqual(
            migrated.get_video_job(job["id"])["video_spec"]["title"], "雪场_全裸"
        )

    def test_schema_25_migrates_legacy_scene_purposes_and_job_route(self) -> None:
        job_id = "d" * 32
        now = "2026-09-11T00:00:00+00:00"
        with self.database.connect() as connection:
            with connection:
                connection.execute(
                    "UPDATE scenes SET purpose='code_agent' WHERE id=?", ("a" * 32,)
                )
                connection.execute(
                    "UPDATE scenes SET purpose='video_gen',is_default_generation=0 WHERE id=?",
                    ("b" * 32,),
                )
                connection.execute("DROP INDEX IF EXISTS idx_scenes_single_default_generation")
                connection.execute("DROP INDEX IF EXISTS idx_scenes_single_last_activated")
                connection.execute("ALTER TABLE scenes DROP COLUMN is_default_generation")
                connection.execute("ALTER TABLE scenes DROP COLUMN is_last_activated")
                for column in (
                    "generation_scene_id", "generation_scene_name",
                    "original_scene_id", "original_scene_name",
                ):
                    connection.execute(f"ALTER TABLE video_jobs DROP COLUMN {column}")
                connection.execute(
                    """CREATE UNIQUE INDEX idx_scenes_unique_purpose
                       ON scenes(purpose) WHERE purpose <> ''"""
                )
                connection.execute(
                    """INSERT INTO video_jobs(
                           id,idempotency_key,payload_hash,session_id,workflow_path,workflow_json,
                           callback_url,status,phase,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id, "legacy-job", "hash", "ses_legacy", str(self.workflow),
                        "{}", "http://127.0.0.1:61714", "queued", "queued", now, now,
                    ),
                )
                connection.execute("UPDATE schema_version SET version=24")

        migrated = Database(self.database.path)
        self.assertEqual(migrated.get_default_generation_scene()["id"], "b" * 32)
        self.assertNotIn("purpose", migrated.get_scene("a" * 32))
        job = migrated.get_video_job(job_id)
        self.assertEqual(job["generation_scene_id"], "b" * 32)
        self.assertEqual(job["original_scene_id"], "a" * 32)

    async def test_process_waits_for_existing_operation_before_freezing_original_scene(self) -> None:
        manager = self.manager()
        job, _ = manager.submit(self.payload("wait-operation"))
        with patch.object(
            self.database, "has_active_operation", side_effect=[True, False]
        ) as active_operation:
            await manager._process(job)
        self.assertGreaterEqual(active_operation.call_count, 2)
        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["original_scene_id"], "a" * 32)

    def test_idempotent_submit_uses_non_secret_payload(self) -> None:
        manager = self.manager()
        first, created = manager.submit(self.payload())
        second, created_again = manager.submit(self.payload())
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], second["id"])
        conflict = self.payload()
        conflict["session_id"] = "ses_other"
        with self.assertRaises(VideoJobError) as raised:
            manager.submit(conflict)
        self.assertEqual(raised.exception.code, "idempotency_conflict")

    def test_submit_rejects_multiple_h3_generation_branches(self) -> None:
        self.workflow.write_text(json.dumps({
            "1": {"class_type": "MiniMaxH3ReferenceToVideo"},
            "2": {"class_type": "SamplerCustomAdvanced"},
            "3": {"class_type": "SamplerCustomAdvanced"},
        }), encoding="utf-8")

        with self.assertRaises(VideoJobError) as raised:
            self.manager().submit(self.payload("multi-h3"))

        self.assertEqual(raised.exception.code, "h3_multi_segment_workflow")

    def test_submit_verifies_expected_workflow_file_hash_during_read(self) -> None:
        payload = self.payload("workflow-file-hash")
        payload["workflow_file_sha256"] = hashlib.sha256(self.workflow.read_bytes()).hexdigest()
        self.workflow.write_text('{"changed": {"class_type": "Test"}}', encoding="utf-8")
        with self.assertRaises(VideoJobError) as raised:
            self.manager().submit(payload)
        self.assertEqual(raised.exception.code, "workflow_hash_mismatch")

    async def test_execution_rechecks_persisted_h3_workflow_safety(self) -> None:
        manager = self.manager()
        job, _ = manager.submit(self.payload("persisted-multi-h3"))
        unsafe = json.dumps({
            "1": {"class_type": "MiniMaxH3ImageToVideo"},
            "2": {"class_type": "SamplerCustomAdvanced"},
            "3": {"class_type": "SamplerCustomAdvanced"},
        })
        with self.database.connect() as connection:
            with connection:
                connection.execute(
                    "UPDATE video_jobs SET workflow_json=? WHERE id=?", (unsafe, job["id"])
                )

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "h3_multi_segment_workflow")

    def test_ninfer_activity_requires_slots_and_queue_to_be_idle(self) -> None:
        client = NInferClient("http://127.0.0.1:8080", "qwen3.8-27b")
        client.http = FakeHttp()
        activity = client.activity()
        self.assertFalse(activity["idle"])
        self.assertEqual(activity["processing"], 1)
        self.assertEqual(activity["deferred"], 2)
        self.assertEqual(activity["active_slots"], [3])

    def test_completed_comfy_job_without_video_reports_missing_output(self) -> None:
        client = ComfyUIClient("http://127.0.0.1:8189")
        client.http = CompletedWithoutOutputHttp()
        status = client.status("prompt-empty")
        self.assertEqual(status["state"], "completed")
        with self.assertRaises(VideoJobError) as raised:
            client.output_descriptor(status["record"])
        self.assertEqual(raised.exception.code, "comfy_video_output_missing")

    def test_comfy_cancel_only_interrupts_the_target_when_running(self) -> None:
        pending = ComfyUIClient("http://127.0.0.1:8189")
        pending.http = QueueHttp(running=False)
        pending.cancel("prompt-1")
        self.assertEqual(pending.http.calls, [("POST", "/queue")])

        running = ComfyUIClient("http://127.0.0.1:8189")
        running.http = QueueHttp(running=True)
        running.cancel("prompt-1")
        self.assertEqual(running.http.calls, [("POST", "/interrupt")])

    def test_comfy_progress_event_is_prompt_scoped_and_validated(self) -> None:
        event = ComfyUIClient._progress_event(json.dumps({
            "type": "progress", "data": {
                "prompt_id": "prompt-1", "node": "3", "value": 5, "max": 8,
            },
        }), "prompt-1")
        self.assertEqual(event, {
            "available": True, "kind": "sampling", "node_id": "3",
            "value": 5, "max": 8, "percent": 62.5,
        })
        self.assertIsNone(ComfyUIClient._progress_event(json.dumps({
            "type": "progress", "data": {
                "prompt_id": "other", "node": "3", "value": 5, "max": 8,
            },
        }), "prompt-1"))
        self.assertIsNone(ComfyUIClient._progress_event(json.dumps({
            "type": "progress", "data": {"node": "3", "value": 5, "max": 8},
        }), "prompt-1"))
        self.assertIsNone(ComfyUIClient._progress_event(json.dumps({
            "type": "progress", "data": {
                "prompt_id": "prompt-1", "node": "3", "value": 9, "max": 8,
            },
        }), "prompt-1"))
        self.assertIsNone(ComfyUIClient._progress_event(json.dumps({
            "type": "progress", "data": {
                "prompt_id": "prompt-1", "node": "3", "value": 1,
                "max": int("9" * 4000),
            },
        }), "prompt-1"))

    async def test_monitor_persists_realtime_sampling_and_compact_completion(self) -> None:
        comfy = RealtimeComfy()
        manager = self.manager(comfy=comfy)
        manager.poll_interval_seconds = 0.05
        job, _ = manager.submit(self.payload("realtime-progress"))
        record = await manager._monitor_prompt(
            job["id"], "prompt-1", {"3": "H3 采样器"},
        )
        self.assertIn("outputs", record)
        progress = self.database.get_video_job(job["id"])["progress"]
        self.assertEqual(progress["state"], "completed")
        self.assertNotIn("record", progress)
        self.assertEqual(progress["realtime"], {
            "available": True, "kind": "sampling", "node_id": "3",
            "node_name": "H3 采样器", "value": 5, "max": 8, "percent": 62.5,
            "updated_at": progress["realtime"]["updated_at"],
        })

    async def test_preconnected_progress_closes_when_prompt_persistence_fails(self) -> None:
        comfy = PreconnectedComfy()
        manager = self.manager(comfy=comfy)
        with patch.object(
            self.database, "update_video_job", side_effect=DatabaseError("write failed"),
        ):
            with self.assertRaises(DatabaseError):
                await manager._submit_and_monitor_prompt(
                    "a" * 32, {"1": {"class_type": "Test"}}, {},
                )
        self.assertEqual(comfy.connection.close_calls, 1)

    async def test_success_waits_for_idle_collects_output_restores_and_callbacks(self) -> None:
        ninfer = IdleNInfer([
            {"processing": 1, "deferred": 0, "active_slots": [3], "idle": False},
            {"processing": 0, "deferred": 0, "active_slots": [], "idle": True},
        ])
        comfy = CompletedComfy()
        callback = RecordingCallback()
        manager = self.manager(ninfer=ninfer, comfy=comfy, callback=callback)
        job, _ = manager.submit(self.payload())
        self.workflow.write_text(json.dumps({"changed": {}}), encoding="utf-8")

        with patch.object(
            manager, "_activate_scene", wraps=manager._activate_scene
        ) as activate_scene:
            await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["prompt_id"], "prompt-1")
        self.assertTrue(Path(finished["output_path"]).is_file())
        shared_relative = f"video-jobs/{job['id']}/result.mp4"
        shared_output = self.shared_output_directory / Path(shared_relative)
        self.assertEqual(shared_output.read_bytes(), b"video")
        self.assertEqual(manager.public_job(finished)["shared_output_path"], shared_relative)
        self.assertEqual(comfy.submit_calls, 1)
        self.assertEqual(comfy.release_calls, 1)
        self.assertEqual(comfy.last_workflow, {"1": {"class_type": "Test"}})
        self.assertEqual(ninfer.verify_calls, 0)
        self.assertEqual(finished["generation_scene_name"], "Video")
        self.assertEqual(finished["original_scene_name"], "Code")
        self.assertEqual(
            [call.args[0] for call in activate_scene.await_args_list],
            ["b" * 32, "a" * 32],
        )
        self.assertIn("已完成", callback.messages[0])
        self.assertIn(
            f"http://127.0.0.1:18765/api/v1/files/content?path="
            f"video-jobs%2F{job['id']}%2Fresult.mp4",
            callback.messages[0],
        )
        self.assertEqual(callback.handoff_checks, 1)
        self.assertIsNone(self.database.resource_lease_owner("gpu:4090"))

    async def test_success_copies_original_video_to_shared_output_directory(self) -> None:
        source = self.root / "原视频.mp4"
        source.write_bytes(b"original-video")
        self.workflow.write_text(json.dumps({
            "1": {"class_type": "VHS_LoadVideoPath", "inputs": {"video": str(source)}},
            "2": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {}},
        }), encoding="utf-8")
        manager = self.manager()
        job, _ = manager.submit(self.payload("publish-original-video"))

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(
            (self.shared_output_directory / "输出" / source.name).read_bytes(),
            b"original-video",
        )

    async def test_original_video_conflict_fails_without_overwriting(self) -> None:
        source = self.root / "source.mp4"
        source.write_bytes(b"original-video")
        self.workflow.write_text(json.dumps({
            "1": {"class_type": "VHS_LoadVideoPath", "inputs": {"video": str(source)}},
        }), encoding="utf-8")
        existing = self.shared_output_directory / "输出" / source.name
        existing.parent.mkdir()
        existing.write_bytes(b"different")
        callback = RecordingCallback()
        manager = self.manager(callback=callback)
        job, _ = manager.submit(self.payload("original-video-conflict"))

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "source_video_conflict")
        self.assertEqual(existing.read_bytes(), b"different")
        self.assertIn("原视频已存在且内容不同", callback.messages[0])

    async def test_changed_original_video_fails_instead_of_publishing_other_content(self) -> None:
        source = self.root / "source.mp4"
        source.write_bytes(b"original-video")
        self.workflow.write_text(json.dumps({
            "1": {"class_type": "VHS_LoadVideoPath", "inputs": {"video": str(source)}},
        }), encoding="utf-8")
        callback = RecordingCallback()
        manager = self.manager(callback=callback)
        job, _ = manager.submit(self.payload("changed-original-video"))
        source.write_bytes(b"replaced-video")

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "source_video_changed")
        self.assertFalse((self.shared_output_directory / "输出" / source.name).exists())
        self.assertIn("原视频内容在任务执行期间已改变", callback.messages[0])

    def test_concurrent_original_video_publish_is_idempotent(self) -> None:
        source = self.root / "same-source.mp4"
        source.write_bytes(b"original-video")
        workflow_json = json.dumps({
            "1": {"class_type": "VHS_LoadVideoPath", "inputs": {"video": str(source)}},
        })
        manager = self.manager()
        barrier = Barrier(2)
        copyfileobj = shutil.copyfileobj

        def synchronized_copy(input_file, output_file, length=0):
            copyfileobj(input_file, output_file, length=length)
            barrier.wait(timeout=5)

        with patch("workstation_manager.video_jobs.shutil.copyfileobj", synchronized_copy):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    lambda _: manager._publish_source_videos(workflow_json), range(2),
                ))

        self.assertEqual(results, [["输出/same-source.mp4"], ["输出/same-source.mp4"]])
        self.assertEqual(
            (self.shared_output_directory / "输出" / source.name).read_bytes(),
            b"original-video",
        )

    async def test_waits_for_opencode_session_idle_before_switching_scene(self) -> None:
        callback = WaitingHandoffCallback(ready_after=2)
        manager = self.manager(callback=callback)
        job, _ = manager.submit(self.payload("opencode-handoff"))

        with patch.object(
            manager, "_activate_scene", wraps=manager._activate_scene,
        ) as activate_scene:
            await manager._process(self.database.next_video_job())

        self.assertEqual(self.database.get_video_job(job["id"])["status"], "succeeded")
        self.assertEqual(callback.handoff_checks, 2)
        self.assertEqual(activate_scene.await_args_list[0].args[0], "b" * 32)

    async def test_opencode_handoff_timeout_keeps_ninfer_scene_active(self) -> None:
        callback = WaitingHandoffCallback(ready_after=100)
        comfy = CompletedComfy()
        manager = self.manager(callback=callback, comfy=comfy)
        manager.scene_timeout_seconds = 0
        job, _ = manager.submit(self.payload("opencode-handoff-timeout"))

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "opencode_handoff_timeout")
        self.assertEqual(comfy.submit_calls, 0)
        self.assertEqual(self.registry.active_scene()["id"], "a" * 32)

    async def test_opencode_handoff_timeout_preserves_last_connection_error(self) -> None:
        callback = UnreachableHandoffCallback()
        comfy = CompletedComfy()
        manager = self.manager(callback=callback, comfy=comfy)
        manager.scene_timeout_seconds = 0
        job, _ = manager.submit(self.payload("opencode-handoff-unreachable"))

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["error_code"], "opencode_handoff_timeout")
        self.assertIn("connection refused", finished["error_summary"])
        self.assertEqual(comfy.submit_calls, 0)
        self.assertEqual(self.registry.active_scene()["id"], "a" * 32)

    def test_opencode_handoff_endpoint_supports_new_and_legacy_plugins(self) -> None:
        job = self.payload("handoff-http")
        job["id"] = "job-handoff-http"
        with patch("workstation_manager.video_jobs.urlopen", return_value=HttpResponse(204)):
            self.assertTrue(OpenCodeCallbackClient.handoff_ready(job))
        for status, expected in ((425, False), (404, True), (405, True)):
            with self.subTest(status=status), patch(
                "workstation_manager.video_jobs.urlopen",
                side_effect=HTTPError("http://callback", status, "test", {}, None),
            ):
                self.assertEqual(OpenCodeCallbackClient.handoff_ready(job), expected)

        with patch("workstation_manager.video_jobs.urlopen", side_effect=URLError("refused")):
            with self.assertRaisesRegex(VideoJobError, "握手端点不可达:.*refused") as raised:
                OpenCodeCallbackClient.handoff_ready(job)
            self.assertEqual(raised.exception.code, "opencode_handoff_unreachable")

    async def test_insufficient_host_memory_blocks_comfy_submission(self) -> None:
        comfy = CompletedComfy()
        callback = RecordingCallback()
        manager = self.manager(
            comfy=comfy,
            callback=callback,
            resource_snapshot=lambda: {
                "host": {"memory": {
                    "available_bytes": 8 * 1024 ** 3,
                    "commit_used_bytes": 120 * 1024 ** 3,
                    "commit_limit_bytes": 144 * 1024 ** 3,
                }},
            },
        )
        job, _ = manager.submit(self.payload("low-memory"))

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "insufficient_available_memory")
        self.assertEqual(comfy.submit_calls, 0)
        self.assertIn("可用物理内存", callback.messages[0])

    async def test_restart_recovers_prompt_without_duplicate_submit(self) -> None:
        comfy = CompletedComfy()
        comfy.recovered_prompt_id = "prompt-recovered"
        manager = self.manager(comfy=comfy)
        job, _ = manager.submit(self.payload("restart-key"))
        self.database.update_video_job(job["id"], status="submitting", phase="submitting")
        self.database.recover_video_jobs()

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["prompt_id"], "prompt-recovered")
        self.assertEqual(comfy.submit_calls, 0)

    def test_restart_preserves_callback_cancellation_cutoff(self) -> None:
        manager = self.manager()
        for phase in ("callback_pending", "callback_delivered"):
            job, _ = manager.submit(self.payload(f"restart-{phase}"))
            self.database.update_video_job(job["id"], status="queued", phase=phase)

        self.database.recover_video_jobs()

        for job in self.database.list_video_jobs():
            if job["phase"] not in {"callback_pending", "callback_delivered"}:
                continue
            self.assertEqual(job["status"], job["phase"])
            with self.assertRaises(VideoJobError) as raised:
                manager.cancel(job["id"], "admin", "local")
            self.assertEqual(raised.exception.code, "video_job_finished")

    async def test_restart_after_output_download_does_not_download_again(self) -> None:
        comfy = CompletedComfy()
        manager = self.manager(comfy=comfy)
        source = self.root / "restart-source.mp4"
        source.write_bytes(b"original-video")
        self.workflow.write_text(json.dumps({
            "1": {"class_type": "VHS_LoadVideoPath", "inputs": {"video": str(source)}},
        }), encoding="utf-8")
        job, _ = manager.submit(self.payload("collected-key"))
        output = self.root / "already-collected.mp4"
        output.write_bytes(b"video")
        self.database.update_video_job(
            job["id"], status="collecting_output", phase="collecting_output",
            prompt_id="prompt-1", output_path=str(output), result="succeeded",
            original_scene_id="a" * 32, original_scene_name="Code",
        )

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["output_path"], str(output))
        self.assertEqual(
            (self.shared_output_directory / "video-jobs" / job["id"] / output.name).read_bytes(),
            b"video",
        )
        self.assertEqual(
            (self.shared_output_directory / "输出" / source.name).read_bytes(),
            b"original-video",
        )
        self.assertEqual(comfy.submit_calls, 0)
        self.assertEqual(comfy.release_calls, 1)

    async def test_shared_output_conflict_fails_without_overwriting(self) -> None:
        callback = RecordingCallback()
        manager = self.manager(callback=callback)
        job, _ = manager.submit(self.payload("shared-conflict"))
        shared_output = self.shared_output_directory / "video-jobs" / job["id"] / "result.mp4"
        shared_output.parent.mkdir(parents=True)
        shared_output.write_bytes(b"different")

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "shared_output_conflict")
        self.assertEqual(shared_output.read_bytes(), b"different")
        self.assertIsNone(manager.public_job(finished)["shared_output_path"])
        self.assertIn("共享输出已存在且内容不同", callback.messages[0])

    def test_shared_output_race_does_not_overwrite_new_file(self) -> None:
        source = self.root / "result.mp4"
        source.write_bytes(b"video")
        manager = self.manager()
        job_id = "c" * 32
        target = self.shared_output_directory / "video-jobs" / job_id / source.name
        real_link = os.link

        def create_target_then_link(temporary_path, target_path):
            Path(target_path).write_bytes(b"new owner")
            return real_link(temporary_path, target_path)

        with patch(
            "workstation_manager.video_jobs.os.link",
            side_effect=create_target_then_link,
        ):
            with self.assertRaises(VideoJobError) as raised:
                manager._publish_shared_output(job_id, str(source))

        self.assertEqual(raised.exception.code, "shared_output_conflict")
        self.assertEqual(target.read_bytes(), b"new owner")

    def test_shared_output_read_error_is_explicit(self) -> None:
        source = self.root / "result.mp4"
        source.write_bytes(b"video")
        manager = self.manager()
        job_id = "d" * 32
        target = self.shared_output_directory / "video-jobs" / job_id / source.name
        target.parent.mkdir(parents=True)
        target.write_bytes(b"video")

        with patch.object(manager, "_file_sha256", side_effect=PermissionError("denied")):
            with self.assertRaises(VideoJobError) as raised:
                manager._publish_shared_output(job_id, str(source))

        self.assertEqual(raised.exception.code, "shared_output_copy_failed")
        self.assertIn("denied", str(raised.exception))

    def test_shared_output_dangling_temporary_symlink_cannot_escape_root(self) -> None:
        source = self.root / "result.mp4"
        source.write_bytes(b"video")
        manager = self.manager()
        job_id = "e" * 32
        target, _ = manager._shared_target(job_id, str(source), create_parent=True)
        temporary_id = "1" * 32
        temporary = target.with_name(f".axis-upload-{temporary_id}.part")
        outside = self.root / "outside.mp4"
        try:
            temporary.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"当前平台不能创建文件符号链接: {exc}")

        fixed_uuid = type("FixedUuid", (), {"hex": temporary_id})()
        with patch("workstation_manager.video_jobs.uuid.uuid4", return_value=fixed_uuid):
            with self.assertRaises(VideoJobError) as raised:
                manager._publish_shared_output(job_id, str(source))

        self.assertFalse(outside.exists())
        self.assertEqual(raised.exception.code, "shared_output_copy_failed")
        self.assertFalse(target.exists())

    def test_shared_output_final_symlink_is_not_accepted_as_published_file(self) -> None:
        source = self.root / "result.mp4"
        source.write_bytes(b"video")
        manager = self.manager()
        job_id = "f" * 32
        target, relative = manager._shared_target(job_id, str(source), create_parent=True)
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"video")
        try:
            target.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"当前平台不能创建文件符号链接: {exc}")

        with self.assertRaises(VideoJobError) as raised:
            manager._publish_shared_output(job_id, str(source))

        self.assertEqual(raised.exception.code, "shared_output_conflict")
        job = {
            "id": job_id, "output_path": str(source), "shared_output_path": relative,
        }
        self.assertIsNone(manager.public_job(job)["shared_output_path"])

    def test_shared_output_rejects_invalid_job_id_before_creating_directories(self) -> None:
        source = self.root / "result.mp4"
        source.write_bytes(b"video")
        manager = self.manager()

        with self.assertRaises(VideoJobError) as raised:
            manager._publish_shared_output("../outside", str(source))

        self.assertEqual(raised.exception.code, "shared_output_path_invalid")
        self.assertFalse((self.root / "outside").exists())

    async def test_batch_cancel_during_shared_copy_stops_remaining_segments(self) -> None:
        callback = RecordingCallback()
        manager = self.manager(callback=callback)
        jobs, _ = manager.submit_batch(self.batch_payload("cancel-copy", 2))
        publish = manager._publish_shared_output

        def publish_then_cancel(job_id: str, output_path: str) -> str:
            relative = publish(job_id, output_path)
            manager.cancel(job_id, "admin", "local")
            return relative

        with patch.object(manager, "_publish_shared_output", side_effect=publish_then_cancel):
            await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(jobs[0]["id"])
        self.assertEqual(finished["status"], "cancelled")
        self.assertIsNotNone(finished["shared_output_path"])
        self.assertEqual(self.database.get_video_job(jobs[1]["id"])["status"], "cancelled")
        self.assertEqual(manager.comfy.release_calls, 1)
        self.assertIn("已在第 1 / 2 段取消", callback.messages[0])

    async def test_invalid_resource_snapshot_fails_without_submitting(self) -> None:
        comfy = CompletedComfy()
        manager = self.manager(comfy=comfy, resource_snapshot=lambda: {"host": None})
        job, _ = manager.submit(self.payload("invalid-resource-snapshot"))

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "resource_metrics_unavailable")
        self.assertEqual(comfy.submit_calls, 0)
        self.assertIsNone(self.database.resource_lease_owner("gpu:4090"))

    async def test_callback_retries_are_bounded_and_visible(self) -> None:
        callback = FailingCallback()
        manager = self.manager(callback=callback)
        job, _ = manager.submit(self.payload("callback-key"))
        with patch("workstation_manager.video_jobs.asyncio.sleep", new=AsyncMock()):
            await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(callback.calls, 3)
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["callback_attempts"], 3)
        self.assertIn("temporary failure", finished["error_summary"])

    async def test_cancelled_failed_outcome_delivers_terminal_cancel_message(self) -> None:
        callback = RecordingCallback()
        manager = self.manager(callback=callback)
        job, _ = manager.submit(self.payload("callback-cancel-key"))
        manager.cancel(job["id"], "admin", "local")
        self.assertTrue(self.database.acquire_resource_lease("gpu:4090", job["id"]))

        await manager._deliver_callback_and_finish(
            job["id"], "failed", None, "generation_failed", "failure before callback",
        )

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "cancelled")
        self.assertEqual(len(callback.messages), 1)
        self.assertIn("已取消", callback.messages[0])
        self.assertIn("不得重新生成", callback.messages[0])
        self.assertIn("新的明确生成要求", callback.messages[0])

    async def test_cancel_is_rejected_after_callback_delivery_begins(self) -> None:
        callback = CancelDuringCallback()
        callback.cancel_error = None
        manager = self.manager(callback=callback)
        callback.manager = manager
        job, _ = manager.submit(self.payload("callback-cancel-key"))
        callback.job_id = job["id"]
        self.assertTrue(self.database.acquire_resource_lease("gpu:4090", job["id"]))

        await manager._deliver_callback_and_finish(job["id"], "succeeded", "output.mp4", None, None)

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertIsNotNone(callback.cancel_error)
        self.assertEqual(callback.cancel_error.code, "video_job_finished")
        self.assertEqual(len(callback.messages), 1)

    async def test_cancel_before_scene_switch_does_not_submit_comfy(self) -> None:
        comfy = CompletedComfy()
        callback = RecordingCallback()
        manager = self.manager(comfy=comfy, callback=callback)
        job, _ = manager.submit(self.payload("cancel-key"))
        manager.cancel(job["id"], "admin", "local")

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "cancelled")
        self.assertEqual(comfy.submit_calls, 0)
        self.assertIn("已取消", callback.messages[0])
        self.assertIn("不得重新生成", callback.messages[0])
        self.assertIn("新的明确生成要求", callback.messages[0])

    def test_gpu_lease_blocks_manual_scene_switch(self) -> None:
        manager = self.manager()
        job, _ = manager.submit(self.payload("lease-key"))
        self.assertTrue(self.database.acquire_resource_lease("gpu:4090", job["id"]))
        with self.assertRaises(RegistryError) as raised:
            self.registry.submit_scene_activation("a" * 32, "admin", "local")
        self.assertEqual(raised.exception.code, "gpu_4090_leased")

    def test_video_submit_api_accepts_unauthenticated_loopback_only(self) -> None:
        settings = Settings(
            database_path=self.database.path, manager_log_path=self.root / "manager.log",
            sample_interval_seconds=60,
        )
        sampler = Sampler(settings, collector=lambda _: {
            "sampled_at": "2099-01-01T00:00:00+00:00",
            "host": {"cpu": {}, "memory": {}, "disks": []}, "gpus": [],
            "docker": {"containers": []}, "ports": [], "collector_errors": [],
        })
        manager = self.manager()
        with TestClient(
            create_app(settings, sampler, self.database, self.registry, manager),
            client=("127.0.0.1", 50000),
        ) as client:
            accepted = client.post("/api/v1/video-jobs", json=self.payload("api-1"))
            self.assertEqual(accepted.status_code, 202, accepted.text)
            self.assertNotIn("workflow_json", accepted.json()["job"])
            self.assertNotIn("source_videos", accepted.json()["job"])
            self.assertNotIn("_source_videos", accepted.json()["job"]["video_spec"])
            legacy = self.payload("api-legacy")
            legacy["callback_authorization"] = "Basic obsolete"
            self.assertEqual(client.post("/api/v1/video-jobs", json=legacy).status_code, 422)
            batch = client.post(
                "/api/v1/video-job-batches", json=self.batch_payload("api-batch", 2)
            )
            self.assertEqual(batch.status_code, 202, batch.text)
            self.assertEqual(len(batch.json()["jobs"]), 2)
            self.assertEqual(
                self.database.video_job_queue_summary()["nonterminal_segments"], 3
            )
        remote_sampler = Sampler(settings, collector=lambda _: {
            "sampled_at": "2099-01-01T00:00:00+00:00",
            "host": {"cpu": {}, "memory": {}, "disks": []}, "gpus": [],
            "docker": {"containers": []}, "ports": [], "collector_errors": [],
        })
        with TestClient(
            create_app(
                settings, remote_sampler, self.database, self.registry, self.manager()
            ),
            client=("192.0.2.10", 50000),
        ) as client:
            rejected = client.post("/api/v1/video-jobs", json=self.payload("api-2"))
            self.assertEqual(rejected.status_code, 422)
            self.assertEqual(rejected.json()["error"]["code"], "loopback_required")

    def test_batch_submit_is_atomic_idempotent_and_reports_queue_summary(self) -> None:
        manager = self.manager()
        jobs, created = manager.submit_batch(self.batch_payload("atomic", 3))
        repeated, created_again = manager.submit_batch(self.batch_payload("atomic", 3))

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual([job["id"] for job in jobs], [job["id"] for job in repeated])
        self.assertEqual([job["batch_index"] for job in jobs], [1, 2, 3])
        self.assertTrue(all(job["batch_id"] == jobs[0]["batch_id"] for job in jobs))
        self.assertTrue(all(job["batch_size"] == 3 for job in jobs))
        self.assertEqual(self.database.video_job_queue_summary(), {
            "queued_segments": 3, "active_segments": 0, "nonterminal_segments": 3,
        })

    async def test_explicit_batch_releases_each_segment_and_callbacks_once(self) -> None:
        ninfer = CountingNInfer()
        comfy = CompletedComfy()
        callback = RecordingCallback()
        manager = self.manager(ninfer=ninfer, comfy=comfy, callback=callback)
        jobs, _ = manager.submit_batch(self.batch_payload("batch-a", 3))

        await manager._process(self.database.next_video_job())

        finished_a = self.database.get_video_job(jobs[0]["id"])
        self.assertEqual(finished_a["status"], "succeeded")
        self.assertEqual(self.registry.active_scene()["id"], "b" * 32)
        self.assertEqual(comfy.release_calls, 1)
        self.assertEqual(ninfer.calls, 1)
        self.assertEqual(callback.messages, [])
        self.assertIsNone(self.database.resource_lease_owner("gpu:4090"))

        await manager._process(self.database.next_video_job())

        finished_b = self.database.get_video_job(jobs[1]["id"])
        self.assertEqual(finished_b["status"], "succeeded")
        self.assertEqual(finished_b["original_scene_id"], "a" * 32)
        self.assertEqual(comfy.release_calls, 2)
        self.assertEqual(ninfer.calls, 1)
        self.assertEqual(callback.messages, [])

        await manager._process(self.database.next_video_job())

        finished_c = self.database.get_video_job(jobs[2]["id"])
        self.assertEqual(finished_c["status"], "succeeded")
        self.assertEqual(self.registry.active_scene()["id"], "a" * 32)
        self.assertEqual(comfy.release_calls, 3)
        self.assertEqual(ninfer.calls, 1)
        self.assertEqual(len(callback.messages), 1)
        self.assertIn("3 段", callback.messages[0])
        self.assertEqual(callback.messages[0].count("http://127.0.0.1:18765/"), 3)
        self.assertIn("已恢复场景", callback.messages[0])
        self.assertIsNone(self.database.resource_lease_owner("gpu:4090"))

    async def test_failed_batch_aborts_remaining_jobs_and_restores_once(self) -> None:
        comfy = CompletedComfy()
        callback = RecordingCallback()
        manager = self.manager(comfy=comfy, callback=callback)
        jobs, _ = manager.submit_batch(self.batch_payload("fail-a", 3))

        await manager._process(self.database.next_video_job())
        self.assertEqual(self.registry.active_scene()["id"], "b" * 32)
        self.assertEqual(callback.messages, [])

        blocked = self.root / "outputs" / jobs[1]["id"] / "result.mp4"
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_bytes(b"video")

        await manager._process(self.database.next_video_job())

        finished_b = self.database.get_video_job(jobs[1]["id"])
        self.assertEqual(finished_b["status"], "failed")
        self.assertEqual(finished_b["error_code"], "output_exists")
        self.assertEqual(self.registry.active_scene()["id"], "a" * 32)
        self.assertEqual(comfy.release_calls, 2)
        self.assertEqual(self.database.get_video_job(jobs[2]["id"])["status"], "cancelled")
        self.assertEqual(len(callback.messages), 1)
        self.assertIn("第 2 / 3 段失败", callback.messages[0])

    async def test_restart_continues_explicit_batch_from_database(self) -> None:
        comfy = CompletedComfy()
        callback = RecordingCallback()
        manager = self.manager(comfy=comfy, callback=callback)
        jobs, _ = manager.submit_batch(self.batch_payload("restart-a", 2))

        await manager._process(self.database.next_video_job())

        self.assertEqual(self.registry.active_scene()["id"], "b" * 32)

        restarted_manager = self.manager(comfy=comfy, callback=callback)
        await restarted_manager._process(self.database.next_video_job())

        finished_b = self.database.get_video_job(jobs[1]["id"])
        self.assertEqual(finished_b["status"], "succeeded")
        self.assertEqual(finished_b["original_scene_id"], "a" * 32)
        self.assertEqual(self.registry.active_scene()["id"], "a" * 32)
        self.assertEqual(comfy.release_calls, 2)
        self.assertEqual(comfy.submit_calls, 2)
