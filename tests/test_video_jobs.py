from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from workstation_manager.app import create_app
from workstation_manager.config import Settings
from workstation_manager.database import Database, DatabaseError
from workstation_manager.history import Sampler
from workstation_manager.registry import RegisteredServiceManager, RegistryError
from workstation_manager.video_jobs import (
    ComfyUIClient,
    NInferClient,
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

    def send(self, job: dict, message: str) -> None:
        self.messages.append(message)


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
            self.manager.cancel(self.job_id, "admin", "local")
            raise VideoJobError("opencode_callback_failed", "retry")


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

    def tearDown(self) -> None:
        self.temp.cleanup()

    def manager(
        self, *, ninfer=None, comfy=None, callback=None, resource_snapshot=None,
    ) -> VideoJobManager:
        return VideoJobManager(
            self.database, self.registry, comfyui_base_url="http://127.0.0.1:8189",
            ninfer_base_url="http://127.0.0.1:8080", ninfer_model_id="qwen3.8-27b",
            output_directory=self.root / "outputs", poll_interval_seconds=0,
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
        self.assertTrue({"batch_id", "batch_index", "batch_size"}.issubset(columns))

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
            "width": None, "height": None, "frames": None,
            "fps": None, "duration_seconds": None, "steps": None,
        })

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
        self.assertIsNone(self.database.resource_lease_owner("gpu:4090"))

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

    async def test_restart_after_output_download_does_not_download_again(self) -> None:
        comfy = CompletedComfy()
        manager = self.manager(comfy=comfy)
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
        self.assertEqual(comfy.submit_calls, 0)
        self.assertEqual(comfy.release_calls, 1)

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

    async def test_cancel_during_callback_retry_changes_terminal_result(self) -> None:
        callback = CancelDuringCallback()
        manager = self.manager(callback=callback)
        callback.manager = manager
        job, _ = manager.submit(self.payload("callback-cancel-key"))
        callback.job_id = job["id"]
        with patch("workstation_manager.video_jobs.asyncio.sleep", new=AsyncMock()):
            await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "cancelled")
        self.assertEqual(len(callback.messages), 2)
        self.assertIn("已取消", callback.messages[1])

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
