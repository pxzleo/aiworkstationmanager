from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from workstation_manager.app import create_app
from workstation_manager.config import Settings
from workstation_manager.database import Database, DatabaseError, SCHEMA_VERSION
from workstation_manager.history import Sampler
from workstation_manager.registry import (
    RegisteredServiceManager,
    RegistryError,
    ScriptResult,
    ScriptRunner,
)


class FakeRunner:
    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.failures: set[tuple[str, str]] = set()

    @staticmethod
    def validate_path(value: str) -> Path:
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise RegistryError(422, "script_not_found", "管理脚本不存在")
        return path.resolve()

    def run(self, script_path: str, action: str) -> ScriptResult:
        key = str(Path(script_path).resolve())
        self.calls.append((Path(key).name, action))
        if (Path(key).name, action) in self.failures:
            return ScriptResult(1, "", f"{action} failed")
        if action == "start" or action == "restart":
            self.states[key] = "running"
            return ScriptResult(0, "ok", "")
        if action == "stop":
            self.states[key] = "stopped"
            return ScriptResult(0, "ok", "")
        return ScriptResult(0, self.states.get(key, "stopped"), "")


class BlockingStatusRunner(FakeRunner):
    def __init__(self) -> None:
        super().__init__()
        self.block_status = False
        self.status_started = threading.Event()
        self.release_status = threading.Event()

    def run(self, script_path: str, action: str) -> ScriptResult:
        if action == "status" and self.block_status:
            self.status_started.set()
            if not self.release_status.wait(2):
                return ScriptResult(1, "", "status test timeout")
        return super().run(script_path, action)


class DatabaseRegistryTests(unittest.TestCase):
    def test_schema_ten_crud_and_service_delete_cascades_scene_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "manager.db")
            self.assertEqual(SCHEMA_VERSION, 10)
            with database.connect() as connection:
                tables = {row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertTrue({"registered_services", "scenes", "scene_services"} <= tables)
            self.assertFalse({"discovered_entries", "scan_runs", "control_operation_lease"} & tables)
            service = {"id": "a" * 32, "name": "服务 A", "description": "说明",
                       "script_path": "D:/a.ps1", "gpu_label": "RTX 4090",
                       "port": 8080, "ui_url": "http://127.0.0.1:8080"}
            database.create_registered_service(service)
            database.create_scene({"id": "b" * 32, "name": "场景 A", "description": "",
                                   "service_ids": [service["id"]]})
            self.assertEqual(database.list_scenes()[0]["service_ids"], [service["id"]])
            self.assertTrue(database.delete_registered_service(service["id"]))
            self.assertEqual(database.list_scenes()[0]["service_ids"], [])

    def test_schema_nine_upgrade_preserves_core_data_and_removes_legacy_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                connection.execute("INSERT INTO schema_version VALUES (9)")
                connection.execute("CREATE TABLE admin_user(id INTEGER PRIMARY KEY, username TEXT)")
                connection.execute("INSERT INTO admin_user VALUES (1, 'admin')")
                for table in ("discovered_entries", "scan_runs", "control_operation_lease",
                              "control_recovery_lock", "control_recovery_items"):
                    connection.execute(f"CREATE TABLE {table}(id INTEGER)")
                connection.commit()
            finally:
                connection.close()
            database = Database(path)
            with database.connect() as connection:
                version = connection.execute("SELECT version FROM schema_version").fetchone()["version"]
                username = connection.execute("SELECT username FROM admin_user WHERE id=1").fetchone()["username"]
                tables = {row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertEqual(version, 10)
            self.assertEqual(username, "admin")
            self.assertFalse({"discovered_entries", "scan_runs", "control_operation_lease",
                              "control_recovery_lock", "control_recovery_items"} & tables)


class ScriptRunnerTests(unittest.TestCase):
    def test_script_contract_uses_fixed_action_and_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "manage.ps1"
            script.write_text("param($action)", encoding="utf-8")
            completed = type("Completed", (), {"returncode": 0, "stdout": "running\n", "stderr": ""})()
            with patch("workstation_manager.registry.shutil.which", return_value="powershell.exe"), \
                    patch("workstation_manager.registry.subprocess.run", return_value=completed) as run:
                result = ScriptRunner().run(str(script), "status")
            self.assertEqual(result.stdout, "running")
            args, kwargs = run.call_args
            self.assertEqual(args[0][-2:], [str(script.resolve()), "status"])
            self.assertEqual(kwargs["cwd"], script.parent.resolve())
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["timeout"], 3.0)

    def test_rejects_missing_relative_and_unsupported_scripts(self) -> None:
        with self.assertRaises(RegistryError):
            ScriptRunner.validate_path("relative.ps1")
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "manage.exe"
            script.write_bytes(b"")
            with self.assertRaises(RegistryError):
                ScriptRunner.validate_path(str(script))


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "manager.db")
        self.runner = FakeRunner()
        self.manager = RegisteredServiceManager(self.database, self.runner, 5.0)

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.temp.cleanup()

    def make_script(self, name: str) -> Path:
        script = self.root / f"{name}.ps1"
        script.write_text("param($action)", encoding="utf-8")
        return script

    async def add_service(self, name: str, gpu: str = "") -> dict:
        return await self.manager.create_service(
            {"name": name, "description": f"{name}说明",
             "script_path": str(self.make_script(name)), "gpu_label": gpu,
             "port": None, "ui_url": ""}, "admin", "127.0.0.1"
        )

    async def wait_operation(self, operation_id: str) -> dict:
        for _ in range(100):
            item = self.database.get_operation(operation_id)
            if item and item["status"] not in {"queued", "running"}:
                return item
            await asyncio.sleep(0.01)
        self.fail("操作没有结束")

    async def test_service_crud_action_status_and_audit(self) -> None:
        service = await self.add_service("推理服务", "RTX 4090")
        self.assertEqual(service["status"]["state"], "stopped")
        operation = self.manager.submit_service_action(service["id"], "start", "admin", "local")
        result = await self.wait_operation(operation)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.manager.list_services()[0]["status"]["state"], "running")
        self.assertEqual(self.database.list_audit(1)[0]["event"], "management.service")
        self.manager.delete_service(service["id"], "admin", "local")
        self.assertEqual(self.manager.list_services(), [])

    async def test_scene_stops_unselected_then_starts_selected_in_order(self) -> None:
        first = await self.add_service("A")
        second = await self.add_service("B")
        third = await self.add_service("C")
        for service in (first, second, third):
            self.runner.states[service["script_path"]] = "running"
        await self.manager.refresh_all_statuses()
        scene = self.manager.create_scene(
            {"name": "目标", "description": "", "service_ids": [third["id"], second["id"]]},
            "admin", "local",
        )
        self.runner.calls.clear()
        operation = self.manager.submit_scene_activation(scene["id"], "admin", "local")
        result = await self.wait_operation(operation)
        self.assertEqual(result["status"], "succeeded")
        action_calls = [call for call in self.runner.calls if call[1] in {"start", "stop"}]
        self.assertEqual(action_calls, [("A.ps1", "stop"), ("C.ps1", "start"),
                                        ("B.ps1", "start")])
        self.assertEqual(self.manager.list_scenes()[0]["state"], "active")

    async def test_stop_failure_blocks_scene_start_but_attempts_all_stops(self) -> None:
        target = await self.add_service("目标")
        old_a = await self.add_service("旧A")
        old_b = await self.add_service("旧B")
        self.runner.failures.add(("旧A.ps1", "stop"))
        scene = self.manager.create_scene(
            {"name": "目标场景", "description": "", "service_ids": [target["id"]]},
            "admin", "local",
        )
        self.runner.calls.clear()
        result = await self.wait_operation(
            self.manager.submit_scene_activation(scene["id"], "admin", "local")
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn(("旧A.ps1", "stop"), self.runner.calls)
        self.assertIn(("旧B.ps1", "stop"), self.runner.calls)
        self.assertNotIn(("目标.ps1", "start"), self.runner.calls)

    async def test_crud_and_second_manager_are_blocked_while_operation_is_queued(self) -> None:
        service = await self.add_service("互斥服务")
        operation = self.manager.submit_service_action(service["id"], "start", "admin", "local")
        with self.assertRaisesRegex(RegistryError, "已有服务或场景操作"):
            self.manager.delete_service(service["id"], "admin", "local")
        second = RegisteredServiceManager(self.database, self.runner, 5.0)
        with self.assertRaisesRegex(RegistryError, "已有服务或场景操作"):
            second.submit_service_action(service["id"], "stop", "admin", "local")
        self.assertEqual((await self.wait_operation(operation))["status"], "succeeded")

    async def test_status_probe_and_action_are_serialized_per_service(self) -> None:
        runner = BlockingStatusRunner()
        manager = RegisteredServiceManager(self.database, runner, 5.0)
        script = self.make_script("串行状态")
        service = await manager.create_service(
            {"name": "串行状态", "description": "", "script_path": str(script),
             "gpu_label": "", "port": None, "ui_url": ""}, "admin", "local"
        )
        runner.calls.clear()
        runner.block_status = True
        refresh = asyncio.create_task(manager.refresh_status(service))
        self.assertTrue(await asyncio.to_thread(runner.status_started.wait, 1))
        operation = manager.submit_service_action(service["id"], "start", "admin", "local")
        await asyncio.sleep(0.05)
        self.assertFalse(any(action == "start" for _, action in runner.calls))
        runner.release_status.set()
        await refresh
        self.assertEqual((await self.wait_operation(operation))["status"], "succeeded")
        await manager.shutdown()

    async def test_finalization_failure_keeps_manager_fail_closed(self) -> None:
        service = await self.add_service("落盘失败")
        with patch.object(
            self.database, "finish_operation_with_audit",
            side_effect=DatabaseError("磁盘写入失败"),
        ):
            self.manager.submit_service_action(service["id"], "start", "admin", "local")
            for _ in range(100):
                if self.manager.last_operation_error:
                    break
                await asyncio.sleep(0.01)
        self.assertIn("无法持久化操作终态", self.manager.last_operation_error or "")
        self.assertTrue(self.manager._operation_pending)

    async def test_status_output_is_exact_lowercase(self) -> None:
        service = await self.add_service("严格状态")
        self.runner.states[service["script_path"]] = "RUNNING"
        status = await self.manager.refresh_status(service)
        self.assertEqual(status["state"], "unknown")

    async def test_second_started_manager_cannot_interrupt_first_instance_operation(self) -> None:
        await self.manager.start()
        operation_id = "c" * 32
        self.database.create_operation(
            operation_id, "service", "d" * 32, "start", "admin", "local"
        )
        second = RegisteredServiceManager(self.database, self.runner, 5.0)
        with self.assertRaisesRegex(RegistryError, "已有管理器实例"):
            await second.start()
        self.assertEqual(self.database.get_operation(operation_id)["status"], "queued")

    async def test_shutdown_waits_for_inflight_status_probe(self) -> None:
        runner = BlockingStatusRunner()
        manager = RegisteredServiceManager(self.database, runner, 0.01)
        script = self.make_script("关闭等待")
        await manager.create_service(
            {"name": "关闭等待", "description": "", "script_path": str(script),
             "gpu_label": "", "port": None, "ui_url": ""}, "admin", "local"
        )
        await manager.start()
        runner.block_status = True
        self.assertTrue(await asyncio.to_thread(runner.status_started.wait, 1))
        shutdown = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0.05)
        self.assertFalse(shutdown.done())
        runner.release_status.set()
        await shutdown


class ApiRegistryTests(unittest.TestCase):
    def test_authenticated_service_and_scene_crud(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "manage.ps1"
            script.write_text("param($action)", encoding="utf-8")
            settings = Settings(database_path=root / "manager.db", manager_log_path=root / "manager.log",
                                sample_interval_seconds=60)
            database = Database(settings.database_path)
            registry = RegisteredServiceManager(database, FakeRunner(), 5)
            sampler = Sampler(settings, collector=lambda _: {
                "sampled_at": "2099-01-01T00:00:00+00:00", "host": {"cpu": {}, "memory": {}, "disks": []},
                "gpus": [], "docker": {"containers": []}, "ports": [], "collector_errors": [],
            })
            with TestClient(create_app(settings, sampler, database, registry),
                            client=("127.0.0.1", 50000)) as client:
                setup = client.post("/api/v1/auth/setup", json={"username": "admin", "password": "password-1234"})
                self.assertEqual(setup.status_code, 201)
                headers = {"X-CSRF-Token": setup.json()["csrf_token"]}
                created = client.post("/api/v1/registered-services", headers=headers, json={
                    "name": "API 服务", "description": "说明", "script_path": str(script),
                    "gpu_label": "RTX 3090", "port": 8080, "ui_url": "http://127.0.0.1:8080",
                })
                self.assertEqual(created.status_code, 201, created.text)
                service_id = created.json()["id"]
                scene = client.post("/api/v1/scenes", headers=headers, json={
                    "name": "API 场景", "description": "", "service_ids": [service_id],
                })
                self.assertEqual(scene.status_code, 201, scene.text)
                self.assertEqual(client.get("/api/v1/services").json()["poll_interval_seconds"], 5)
                deleted = client.delete(f"/api/v1/registered-services/{service_id}", headers=headers)
                self.assertEqual(deleted.status_code, 204)
                self.assertEqual(client.get("/api/v1/scenes").json()["scenes"][0]["service_ids"], [])


if __name__ == "__main__":
    unittest.main()
