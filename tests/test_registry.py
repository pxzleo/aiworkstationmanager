from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from workstation_manager.app import create_app
from workstation_manager.auth import AuthService
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


class BlockingActionRunner(FakeRunner):
    def __init__(self) -> None:
        super().__init__()
        self.action_started = threading.Event()
        self.release_action = threading.Event()

    def run(self, script_path: str, action: str) -> ScriptResult:
        if action in {"start", "stop", "restart"}:
            self.action_started.set()
            if not self.release_action.wait(2):
                return ScriptResult(1, "", "action test timeout")
        return super().run(script_path, action)


class DatabaseRegistryTests(unittest.TestCase):
    def test_schema_twelve_crud_and_service_delete_cascades_scene_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "manager.db")
            self.assertEqual(SCHEMA_VERSION, 13)
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
            stored = database.get_registered_service(service["id"])
            self.assertEqual(stored["recorded_state"], "unknown")
            self.assertIsNone(stored["state_updated_at"])
            database.create_scene({"id": "b" * 32, "name": "场景 A", "description": "",
                                   "service_ids": [service["id"]]})
            self.assertEqual(database.list_scenes()[0]["service_ids"], [service["id"]])
            self.assertTrue(database.delete_registered_service(service["id"]))
            self.assertEqual(database.list_scenes()[0]["service_ids"], [])

    def test_new_scenes_append_and_reorder_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "manager.db")
            first = {"id": "1" * 32, "name": "Z 场景", "description": "", "service_ids": []}
            second = {"id": "2" * 32, "name": "A 场景", "description": "", "service_ids": []}
            database.create_scene(first)
            database.create_scene(second)
            self.assertEqual([item["id"] for item in database.list_scenes()],
                             [first["id"], second["id"]])

            database.reorder_scenes([second["id"], first["id"]], "admin", "local")

            third = {"id": "3" * 32, "name": "M 场景", "description": "", "service_ids": []}
            database.create_scene(third)

            self.assertEqual([item["id"] for item in database.list_scenes()],
                             [second["id"], first["id"], third["id"]])
            self.assertEqual([item["display_order"] for item in database.list_scenes()], [0, 1, 2])

    def test_scene_reorder_rolls_back_when_audit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "manager.db")
            first = {"id": "1" * 32, "name": "场景一", "description": "", "service_ids": []}
            second = {"id": "2" * 32, "name": "场景二", "description": "", "service_ids": []}
            database.create_scene(first)
            database.create_scene(second)

            with patch.object(
                database, "insert_audit", side_effect=sqlite3.OperationalError("审计写入失败")
            ):
                with self.assertRaisesRegex(DatabaseError, "保存场景排序失败"):
                    database.reorder_scenes(
                        [second["id"], first["id"]], "admin", "local"
                    )

            self.assertEqual(
                [item["id"] for item in database.list_scenes()], [first["id"], second["id"]]
            )

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
            self.assertEqual(version, 13)
            self.assertEqual(username, "admin")
            self.assertFalse({"discovered_entries", "scan_runs", "control_operation_lease",
                              "control_recovery_lock", "control_recovery_items"} & tables)

    def test_schema_ten_upgrade_preserves_current_scene_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                connection.execute("INSERT INTO schema_version VALUES (10)")
                connection.execute(
                    "CREATE TABLE scenes(id TEXT PRIMARY KEY,name TEXT,description TEXT,created_at TEXT,updated_at TEXT)"
                )
                connection.execute(
                    "CREATE TABLE registered_services(id TEXT PRIMARY KEY,name TEXT)"
                )
                connection.execute(
                    """CREATE TABLE scene_services(
                           scene_id TEXT,service_id TEXT,start_order INTEGER
                       )"""
                )
                connection.execute("INSERT INTO scenes VALUES ('b','B 场景','','','')")
                connection.execute("INSERT INTO scenes VALUES ('a','A 场景','','','')")
                connection.commit()
            finally:
                connection.close()

            database = Database(path)

            self.assertEqual([item["id"] for item in database.list_scenes()], ["a", "b"])
            self.assertEqual([item["display_order"] for item in database.list_scenes()], [0, 1])

    def test_schema_thirteen_supports_multiple_users(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "manager.db")
            auth = AuthService(database, 3600)

            auth.setup("admin", "1234", "127.0.0.1")
            created = auth.create_user("zzq", "5678", "127.0.0.1")
            token, _, _ = auth.login("zzq", "5678", "127.0.0.1")

            self.assertEqual(SCHEMA_VERSION, 13)
            self.assertEqual(created["username"], "zzq")
            self.assertEqual(auth.authenticate(token).username, "zzq")
            with database.connect() as connection:
                users = connection.execute(
                    "SELECT username FROM admin_user ORDER BY id"
                ).fetchall()
                foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual([row["username"] for row in users], ["admin", "zzq"])
            self.assertEqual(foreign_key_errors, [])

    def test_schema_twelve_upgrade_preserves_admin_and_invalidates_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                connection.execute("INSERT INTO schema_version VALUES (12)")
                connection.execute(
                    """CREATE TABLE admin_user(
                           id INTEGER PRIMARY KEY CHECK(id=1),username TEXT UNIQUE,
                           password_hash BLOB,password_salt BLOB,iterations INTEGER,created_at TEXT
                       )"""
                )
                connection.execute(
                    "INSERT INTO admin_user VALUES (1,'admin',?,?,310000,'now')",
                    (b"x" * 32, b"y" * 32),
                )
                connection.execute(
                    """CREATE TABLE sessions(
                           token_hash TEXT PRIMARY KEY,admin_id INTEGER REFERENCES admin_user(id),
                           csrf_hash TEXT,created_at TEXT,expires_at TEXT,source_ip TEXT
                       )"""
                )
                connection.execute(
                    "INSERT INTO sessions VALUES ('token',1,'csrf','now','later','local')"
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(path)

            with database.connect() as connection:
                users = connection.execute("SELECT id,username FROM admin_user").fetchall()
                session_count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                connection.execute(
                    """INSERT INTO admin_user(
                           username,password_hash,password_salt,iterations,created_at
                       ) VALUES ('zzq',?,?,310000,'now')""",
                    (b"z" * 32, b"w" * 32),
                )
                foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual([(row["id"], row["username"]) for row in users], [(1, "admin")])
            self.assertEqual(session_count, 0)
            self.assertEqual(foreign_key_errors, [])


class ScriptRunnerTests(unittest.TestCase):
    def test_script_contract_uses_fixed_action_and_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "manage.ps1"
            script.write_text("param($action)", encoding="utf-8")
            completed = type("Completed", (), {"returncode": 0})()

            def complete_with_output(*args, **kwargs):
                kwargs["stdout"].write(b"running\n")
                kwargs["stdout"].flush()
                return completed

            with patch("workstation_manager.registry.shutil.which", return_value="powershell.exe"), \
                    patch("workstation_manager.registry.subprocess.run",
                          side_effect=complete_with_output) as run:
                result = ScriptRunner().run(str(script), "status")
            self.assertEqual(result.stdout, "running")
            args, kwargs = run.call_args
            self.assertEqual(args[0][-2:], [str(script.resolve()), "status"])
            self.assertEqual(kwargs["cwd"], script.parent.resolve())
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["timeout"], 3.0)
            self.assertNotIn("capture_output", kwargs)

    def test_output_tail_read_is_bounded(self) -> None:
        class BoundedStream:
            def __init__(self) -> None:
                self.position = 0

            def flush(self) -> None:
                pass

            def seek(self, offset: int, whence: int = 0) -> int:
                if whence == os.SEEK_END:
                    self.position = 1_000_000
                else:
                    self.position = offset
                return self.position

            def read(self, size: int = -1) -> bytes:
                self.assert_size = size
                if size != ScriptRunner.OUTPUT_READ_BYTES:
                    raise AssertionError(f"读取大小不受限: {size}")
                return b"x" * (size - 8) + b"running\n"

        stream = BoundedStream()
        output = ScriptRunner._read_output_tail(stream)
        self.assertEqual(stream.assert_size, ScriptRunner.OUTPUT_READ_BYTES)
        self.assertTrue(output.endswith("running"))
        self.assertLessEqual(len(output), ScriptRunner.OUTPUT_LIMIT)

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
        self.manager = RegisteredServiceManager(self.database, self.runner)

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
        self.assertEqual(service["status"]["state"], "unknown")
        self.assertEqual(self.runner.calls, [])
        operation = self.manager.submit_service_action(service["id"], "start", "admin", "local")
        result = await self.wait_operation(operation)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.manager.list_services()[0]["status"]["state"], "running")
        self.assertEqual(self.runner.calls, [("推理服务.ps1", "start")])
        restarted = RegisteredServiceManager(self.database, self.runner)
        self.assertEqual(restarted.list_services()[0]["status"]["state"], "running")
        self.assertEqual(self.database.list_audit(1)[0]["event"], "management.service")
        self.manager.delete_service(service["id"], "admin", "local")
        self.assertEqual(self.manager.list_services(), [])

    async def test_scene_stops_unselected_then_starts_selected_in_order(self) -> None:
        first = await self.add_service("A")
        second = await self.add_service("B")
        third = await self.add_service("C")
        stopped = await self.add_service("D")
        for service in (first, second, third):
            self.runner.states[service["script_path"]] = "running"
        for service in (first, second, third, stopped):
            await self.manager.refresh_status(service)
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
        self.assertNotIn(("D.ps1", "stop"), action_calls)
        self.assertEqual(self.manager.list_scenes()[0]["state"], "active")

    async def test_scene_does_not_stop_unhealthy_or_unknown_services(self) -> None:
        target = await self.add_service("目标")
        unhealthy = await self.add_service("异常")
        unknown = await self.add_service("未知")
        self.runner.states[unhealthy["script_path"]] = "unhealthy"
        self.runner.states[unknown["script_path"]] = "unknown"
        await self.manager.refresh_status(unhealthy)
        await self.manager.refresh_status(unknown)
        scene = self.manager.create_scene(
            {"name": "目标场景", "description": "", "service_ids": [target["id"]]},
            "admin", "local",
        )
        self.runner.calls.clear()

        result = await self.wait_operation(
            self.manager.submit_scene_activation(scene["id"], "admin", "local")
        )
        action_calls = [call for call in self.runner.calls if call[1] in {"start", "stop"}]

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(action_calls, [("目标.ps1", "start")])
        self.assertNotIn(("异常.ps1", "stop"), action_calls)
        self.assertNotIn(("未知.ps1", "stop"), action_calls)
        self.assertEqual(self.manager.list_scenes()[0]["state"], "active")

    async def test_scene_exposes_service_status_and_ui(self) -> None:
        service = await self.manager.create_service(
            {"name": "带界面服务", "description": "", "script_path": str(self.make_script("带界面服务")),
             "gpu_label": "", "port": 8080, "ui_url": "http://127.0.0.1:8080"},
            "admin", "local",
        )
        scene = self.manager.create_scene(
            {"name": "界面场景", "description": "", "service_ids": [service["id"]]},
            "admin", "local",
        )
        scene_service = scene["services"][0]
        self.assertEqual(scene_service["name"], "带界面服务")
        self.assertEqual(scene_service["status"]["state"], "unknown")
        self.assertEqual(scene_service["ui_url"], "http://127.0.0.1:8080")

    async def test_stop_all_services_records_each_step(self) -> None:
        first = await self.add_service("A")
        second = await self.add_service("B")
        self.runner.states[first["script_path"]] = "running"
        self.runner.states[second["script_path"]] = "running"
        await self.manager.refresh_status(first)
        await self.manager.refresh_status(second)
        self.runner.calls.clear()

        operation = self.manager.submit_stop_all("admin", "local")
        result = await self.wait_operation(operation)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["after_state"], "stopped")
        self.assertEqual([call for call in self.runner.calls if call[1] == "stop"],
                         [("A.ps1", "stop"), ("B.ps1", "stop")])
        self.assertEqual([step["phase"] for step in result["steps"]],
                         ["stop_all", "stop_all"])

    async def test_cancel_scene_stops_before_next_service(self) -> None:
        runner = BlockingActionRunner()
        manager = RegisteredServiceManager(self.database, runner)
        first = await manager.create_service(
            {"name": "A", "description": "", "script_path": str(self.make_script("取消A")),
             "gpu_label": "", "port": None, "ui_url": ""}, "admin", "local"
        )
        second = await manager.create_service(
            {"name": "B", "description": "", "script_path": str(self.make_script("取消B")),
             "gpu_label": "", "port": None, "ui_url": ""}, "admin", "local"
        )
        scene = manager.create_scene(
            {"name": "可终止场景", "description": "", "service_ids": [first["id"], second["id"]]},
            "admin", "local",
        )
        runner.calls.clear()
        operation_id = manager.submit_scene_activation(scene["id"], "admin", "local")
        self.assertTrue(await asyncio.to_thread(runner.action_started.wait, 1))

        cancel_result = manager.request_scene_cancel(operation_id, "admin", "local")
        self.assertEqual(cancel_result["status"], "cancellation_requested")
        runner.release_action.set()
        result = await self.wait_operation(operation_id)

        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["result"], "cancelled")
        self.assertIn(("取消A.ps1", "start"), runner.calls)
        self.assertNotIn(("取消B.ps1", "start"), runner.calls)
        await manager.shutdown()

    async def test_cancel_persistence_failure_does_not_stop_scene(self) -> None:
        runner = BlockingActionRunner()
        manager = RegisteredServiceManager(self.database, runner)
        first = await manager.create_service(
            {"name": "A", "description": "", "script_path": str(self.make_script("落盘A")),
             "gpu_label": "", "port": None, "ui_url": ""}, "admin", "local"
        )
        second = await manager.create_service(
            {"name": "B", "description": "", "script_path": str(self.make_script("落盘B")),
             "gpu_label": "", "port": None, "ui_url": ""}, "admin", "local"
        )
        scene = manager.create_scene(
            {"name": "落盘失败场景", "description": "", "service_ids": [first["id"], second["id"]]},
            "admin", "local",
        )
        runner.calls.clear()
        operation_id = manager.submit_scene_activation(scene["id"], "admin", "local")
        self.assertTrue(await asyncio.to_thread(runner.action_started.wait, 1))

        with patch.object(
            self.database, "request_scene_operation_cancel",
            side_effect=DatabaseError("磁盘写入失败"),
        ):
            with self.assertRaisesRegex(DatabaseError, "磁盘写入失败"):
                manager.request_scene_cancel(operation_id, "admin", "local")
        self.assertFalse(manager._cancel_requests[operation_id].is_set())
        runner.release_action.set()
        result = await self.wait_operation(operation_id)

        self.assertEqual(result["status"], "succeeded")
        self.assertIn(("落盘B.ps1", "start"), runner.calls)
        await manager.shutdown()

    async def test_stop_failure_blocks_scene_start_but_attempts_all_stops(self) -> None:
        target = await self.add_service("目标")
        old_a = await self.add_service("旧A")
        old_b = await self.add_service("旧B")
        self.runner.states[old_a["script_path"]] = "running"
        self.runner.states[old_b["script_path"]] = "running"
        await self.manager.refresh_status(old_a)
        await self.manager.refresh_status(old_b)
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
        second = RegisteredServiceManager(self.database, self.runner)
        with self.assertRaisesRegex(RegistryError, "已有服务或场景操作"):
            second.submit_service_action(service["id"], "stop", "admin", "local")
        self.assertEqual((await self.wait_operation(operation))["status"], "succeeded")

    async def test_status_probe_and_action_are_serialized_per_service(self) -> None:
        runner = BlockingStatusRunner()
        manager = RegisteredServiceManager(self.database, runner)
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

    async def test_only_current_service_is_busy_during_global_operation(self) -> None:
        runner = BlockingActionRunner()
        manager = RegisteredServiceManager(self.database, runner)
        first_script = self.make_script("操作目标")
        second_script = self.make_script("其他服务")
        first = await manager.create_service(
            {"name": "操作目标", "description": "", "script_path": str(first_script),
             "gpu_label": "", "port": None, "ui_url": ""}, "admin", "local"
        )
        second = await manager.create_service(
            {"name": "其他服务", "description": "", "script_path": str(second_script),
             "gpu_label": "", "port": None, "ui_url": ""}, "admin", "local"
        )
        operation = manager.submit_service_action(first["id"], "start", "admin", "local")
        self.assertTrue(await asyncio.to_thread(runner.action_started.wait, 1))
        services = {item["id"]: item for item in manager.list_services()}
        self.assertTrue(services[first["id"]]["busy"])
        self.assertFalse(services[second["id"]]["busy"])
        self.assertTrue(services[first["id"]]["operation_pending"])
        self.assertTrue(services[second["id"]]["operation_pending"])
        runner.release_action.set()
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

    async def test_manual_status_check_runs_once_and_persists(self) -> None:
        service = await self.add_service("手动检查")
        self.runner.states[service["script_path"]] = "running"
        self.runner.calls.clear()

        status = await self.manager.check_service_status(service["id"])

        self.assertEqual(status["state"], "running")
        self.assertEqual(self.runner.calls, [("手动检查.ps1", "status")])
        restarted = RegisteredServiceManager(self.database, self.runner)
        self.assertEqual(restarted.list_services()[0]["status"]["state"], "running")

    async def test_status_error_is_redacted_before_storage_and_response(self) -> None:
        service = await self.add_service("脱敏检查")
        with patch.object(
            self.runner,
            "run",
            return_value=ScriptResult(
                1, "", "Authorization: Bearer top-secret password=hunter2"
            ),
        ):
            status = await self.manager.check_service_status(service["id"])

        self.assertNotIn("top-secret", status["error"])
        self.assertNotIn("hunter2", status["error"])
        self.assertIn("<redacted>", status["error"])
        stored = self.database.get_registered_service(service["id"])
        self.assertEqual(stored["state_error"], status["error"])

    async def test_changing_script_path_resets_stored_state(self) -> None:
        service = await self.add_service("替换脚本")
        operation = self.manager.submit_service_action(
            service["id"], "start", "admin", "local"
        )
        self.assertEqual((await self.wait_operation(operation))["status"], "succeeded")
        replacement = self.make_script("替换后的脚本")

        updated = await self.manager.update_service(
            service["id"],
            {"name": service["name"], "description": service["description"],
             "script_path": str(replacement), "gpu_label": service["gpu_label"],
             "port": service["port"], "ui_url": service["ui_url"]},
            "admin", "local",
        )

        self.assertEqual(updated["status"]["state"], "unknown")
        restarted = RegisteredServiceManager(self.database, self.runner)
        self.assertEqual(restarted.list_services()[0]["status"]["state"], "unknown")

    async def test_second_started_manager_cannot_interrupt_first_instance_operation(self) -> None:
        await self.manager.start()
        operation_id = "c" * 32
        self.database.create_operation(
            operation_id, "service", "d" * 32, "start", "admin", "local"
        )
        second = RegisteredServiceManager(self.database, self.runner)
        with self.assertRaisesRegex(RegistryError, "已有管理器实例"):
            await second.start()
        self.assertEqual(self.database.get_operation(operation_id)["status"], "queued")

    async def test_manager_start_does_not_probe_service_status(self) -> None:
        runner = BlockingStatusRunner()
        manager = RegisteredServiceManager(self.database, runner)
        script = self.make_script("关闭等待")
        await manager.create_service(
            {"name": "关闭等待", "description": "", "script_path": str(script),
             "gpu_label": "", "port": None, "ui_url": ""}, "admin", "local"
        )
        runner.calls.clear()
        await manager.start()
        runner.block_status = True
        await asyncio.sleep(0.05)
        self.assertFalse(runner.status_started.is_set())
        self.assertEqual(runner.calls, [])
        await manager.shutdown()


class ApiRegistryTests(unittest.TestCase):
    def test_authenticated_service_and_scene_crud(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "manage.ps1"
            script.write_text("param($action)", encoding="utf-8")
            settings = Settings(database_path=root / "manager.db", manager_log_path=root / "manager.log",
                                sample_interval_seconds=60)
            database = Database(settings.database_path)
            registry = RegisteredServiceManager(database, FakeRunner())
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
                reordered = client.post(
                    "/api/v1/scenes/reorder", headers=headers, json={"scene_ids": [scene.json()["id"]]}
                )
                self.assertEqual(reordered.status_code, 200, reordered.text)
                scene_service = client.get("/api/v1/scenes").json()["scenes"][0]["services"][0]
                self.assertEqual(scene_service["name"], "API 服务")
                self.assertEqual(scene_service["ui_url"], "http://127.0.0.1:8080")
                stop_all = client.post(
                    "/api/v1/registered-services/actions/stop-all", headers=headers
                )
                self.assertEqual(stop_all.status_code, 202, stop_all.text)
                operation_id = stop_all.json()["operation_id"]
                for _ in range(100):
                    operation = client.get(f"/api/v1/operations/{operation_id}").json()
                    if operation["status"] not in {"queued", "running"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(operation["status"], "succeeded")
                service_list = client.get("/api/v1/services").json()
                self.assertEqual(service_list["status_mode"], "stored")
                self.assertNotIn("poll_interval_seconds", service_list)
                checked = client.post(
                    f"/api/v1/registered-services/{service_id}/status", headers=headers
                )
                self.assertEqual(checked.status_code, 200, checked.text)
                deleted = client.delete(f"/api/v1/registered-services/{service_id}", headers=headers)
                self.assertEqual(deleted.status_code, 204)
                self.assertEqual(client.get("/api/v1/scenes").json()["scenes"][0]["service_ids"], [])


if __name__ == "__main__":
    unittest.main()
