from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from workstation_manager.app import create_app
from workstation_manager.config import Settings
from workstation_manager.database import Database
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


class RecordingCallback:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, job: dict, message: str) -> None:
        self.messages.append(message)


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
            "id": "a" * 32, "name": "Code", "description": "", "purpose": "code_agent",
            "service_ids": [],
        })
        self.database.create_scene({
            "id": "b" * 32, "name": "Video", "description": "", "purpose": "video_gen",
            "service_ids": [],
        })
        self.workflow = self.root / "workflow.json"
        self.workflow.write_text(json.dumps({"1": {"class_type": "Test"}}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def manager(self, *, ninfer=None, comfy=None, callback=None) -> VideoJobManager:
        return VideoJobManager(
            self.database, self.registry, comfyui_base_url="http://127.0.0.1:8189",
            ninfer_base_url="http://127.0.0.1:8080", ninfer_model_id="qwen3.8-27b",
            output_directory=self.root / "outputs", poll_interval_seconds=0,
            idle_timeout_seconds=1, scene_timeout_seconds=1, generation_timeout_seconds=1,
            ninfer=ninfer or IdleNInfer(), comfy=comfy or CompletedComfy(),
            callback=callback or RecordingCallback(),
        )

    def payload(self, key: str = "job-key") -> dict:
        return {
            "idempotency_key": key, "session_id": "ses_test",
            "workflow_path": str(self.workflow), "output_path": None,
            "callback_url": "http://127.0.0.1:61714",
            "callback_directory": str(self.root),
        }

    def test_scene_purpose_is_unique_and_user_selected(self) -> None:
        scene = self.database.get_scene_by_purpose("video_gen")
        self.assertEqual(scene["id"], "b" * 32)
        with self.assertRaises(Exception):
            self.database.create_scene({
                "id": "c" * 32, "name": "Video 2", "description": "",
                "purpose": "video_gen", "service_ids": [],
            })

    def test_database_schema_contains_no_callback_authorization_column(self) -> None:
        with self.database.connect() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(video_jobs)")
            }
        self.assertNotIn("callback_authorization", columns)

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

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["prompt_id"], "prompt-1")
        self.assertTrue(Path(finished["output_path"]).is_file())
        self.assertEqual(comfy.submit_calls, 1)
        self.assertEqual(comfy.last_workflow, {"1": {"class_type": "Test"}})
        self.assertEqual(ninfer.verify_calls, 1)
        self.assertIn("已完成", callback.messages[0])
        self.assertIsNone(self.database.resource_lease_owner("gpu:4090"))

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
        )

        await manager._process(self.database.next_video_job())

        finished = self.database.get_video_job(job["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["output_path"], str(output))
        self.assertEqual(comfy.submit_calls, 0)

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
