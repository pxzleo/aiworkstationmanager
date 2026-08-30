from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from workstation_manager.app import create_app
from workstation_manager.auth import SESSION_COOKIE, AuthError, AuthService
from workstation_manager.config import Settings
from workstation_manager.database import Database, DatabaseError, SCHEMA_VERSION
from workstation_manager.history import Sampler
from workstation_manager.registry import (
    HealthProbeResult,
    HttpHealthProbe,
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


class FakeHealthProbe:
    def __init__(self) -> None:
        self.results: dict[str, HealthProbeResult] = {}
        self.calls: list[tuple[str, str]] = []

    def probe(self, url: str, expected_text: str) -> HealthProbeResult:
        self.calls.append((url, expected_text))
        return self.results.get(
            url, HealthProbeResult("stopped", None, False)
        )


class DatabaseRegistryTests(unittest.TestCase):
    def test_schema_eighteen_upgrade_keeps_existing_scenes_non_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                connection.execute("INSERT INTO schema_version(version) VALUES (18)")
                connection.execute(
                    """CREATE TABLE scenes(
                           id TEXT PRIMARY KEY,name TEXT,description TEXT,display_order INTEGER,
                           created_at TEXT,updated_at TEXT
                       )"""
                )
                connection.execute(
                    "INSERT INTO scenes VALUES (?,?,?,?,?,?)",
                    ("a" * 32, "旧场景", "", 0, "created", "updated"),
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(path)
            with database.connect() as connection:
                version = connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()["version"]
                scene = connection.execute(
                    "SELECT is_default FROM scenes WHERE id=?", ("a" * 32,)
                ).fetchone()
                index = connection.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type='index' AND name='idx_scenes_single_default'"""
                ).fetchone()

            self.assertEqual(version, 19)
            self.assertEqual(scene["is_default"], 0)
            self.assertIsNotNone(index)

    def test_schema_seventeen_service_state_migrates_to_dual_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                connection.execute("INSERT INTO schema_version(version) VALUES (17)")
                connection.execute(
                    """CREATE TABLE registered_services(
                           id TEXT PRIMARY KEY,name TEXT,description TEXT,script_path TEXT,
                           gpu_label TEXT,port INTEGER,ui_url TEXT,created_at TEXT,updated_at TEXT,
                           recorded_state TEXT,state_updated_at TEXT,state_error TEXT
                       )"""
                )
                connection.execute(
                    """INSERT INTO registered_services VALUES(
                           ?,?,?,?,?,?,?,?,?,?,?,?
                       )""",
                    ("a" * 32, "迁移服务", "", "D:/a.ps1", "", 8080, "", "now",
                     "now", "running", "checked", None),
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(path)
            service = database.get_registered_service("a" * 32)

            self.assertEqual(service["desired_state"], "running")
            self.assertEqual(service["observed_state"], "running")
            self.assertEqual(service["observed_at"], "checked")
            self.assertEqual(service["health_url"], "")

    def test_schema_twelve_crud_and_service_delete_cascades_scene_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "manager.db")
            self.assertEqual(SCHEMA_VERSION, 19)
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
            self.assertEqual(stored["desired_state"], "unknown")
            self.assertEqual(stored["observed_state"], "unknown")
            self.assertEqual(stored["health_url"], "")
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

    def test_default_scene_is_unique_and_deleting_it_clears_the_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "manager.db")
            first = {"id": "1" * 32, "name": "场景一", "description": "", "service_ids": []}
            second = {"id": "2" * 32, "name": "场景二", "description": "", "service_ids": []}
            database.create_scene(first)
            database.create_scene(second)

            self.assertIsNone(database.get_default_scene())
            self.assertTrue(database.set_default_scene(first["id"], True))
            self.assertEqual(database.get_default_scene()["id"], first["id"])
            self.assertTrue(database.set_default_scene(second["id"], True))
            scenes = {item["id"]: item for item in database.list_scenes()}
            self.assertEqual(scenes[first["id"]]["is_default"], 0)
            self.assertEqual(scenes[second["id"]]["is_default"], 1)

            self.assertTrue(database.delete_scene(second["id"]))
            self.assertIsNone(database.get_default_scene())

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
                self.assertEqual(version, 19)
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

            self.assertEqual(SCHEMA_VERSION, 19)
            self.assertEqual(created["username"], "zzq")
            self.assertEqual(auth.authenticate(token).username, "zzq")
            with database.connect() as connection:
                users = connection.execute(
                    "SELECT username FROM admin_user ORDER BY id"
                ).fetchall()
                foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual([row["username"] for row in users], ["admin", "zzq"])
            self.assertEqual(foreign_key_errors, [])

    def test_schema_seventeen_persists_prunes_and_aggregates_resource_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager.db"
            database = Database(path, resource_history_retention_minutes=1440)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            bucket_start = now - timedelta(seconds=int(now.timestamp()) % 15)

            def history_sample(offset_seconds: int, cpu: float, gpu: float) -> dict[str, Any]:
                return {
                    "sampled_at": (bucket_start + timedelta(seconds=offset_seconds)).isoformat(),
                    "cpu_load_percent": cpu,
                    "cpu_temperature_c": 40 + cpu,
                    "memory_percent": 50 + cpu,
                    "memory_used_bytes": (8 + cpu / 10) * 1024 ** 3,
                    "memory_total_bytes": 64 * 1024 ** 3,
                    "cpu_frequency_mhz": 3000 + cpu,
                    "commit_used_bytes": (20 + cpu / 10) * 1024 ** 3,
                    "commit_limit_bytes": 96 * 1024 ** 3,
                    "swap_used_bytes": cpu * 1024 ** 2,
                    "swap_total_bytes": 32 * 1024 ** 3,
                    "network_received_bytes_per_second": cpu * 1000,
                    "network_sent_bytes_per_second": cpu * 500,
                    "wsl_memory_used_bytes": cpu * 1024 ** 3,
                    "wsl_swap_used_bytes": cpu * 1024 ** 2,
                    "disks": [{
                        "name": "PhysicalDrive0",
                        "read_bytes_per_second": cpu * 100,
                        "write_bytes_per_second": cpu * 200,
                        "latency_ms": cpu / 10,
                    }],
                    "gpus": [{
                        "uuid": "GPU-a", "index": 0, "name": "RTX",
                        "load_percent": gpu, "memory_used_mib": 100 + gpu,
                        "memory_total_mib": 1000, "memory_percent": gpu / 2,
                        "temperature_c": 60 + gpu / 10,
                        "graphics_clock_mhz": 2000 + gpu,
                        "power_w": 100 + gpu,
                        "memory_utilization_percent": gpu / 4,
                        "encoder_percent": gpu / 5,
                        "decoder_percent": gpu / 10,
                    }],
                }

            database.append_resource_sample(history_sample(1, 10, 40))
            database.append_resource_sample(history_sample(6, 20, 60))
            database.append_resource_sample(history_sample(16, 30, 80))
            stale = history_sample(0, 99, 99)
            stale["sampled_at"] = (now - timedelta(hours=25)).isoformat()
            database.append_resource_sample(stale)

            reopened = Database(path, resource_history_retention_minutes=1440)
            result = reopened.query_resource_history(
                60, bucket_seconds=15, now=now + timedelta(seconds=30)
            )

            self.assertEqual(SCHEMA_VERSION, 19)
            self.assertEqual(result["stored_sample_count"], 3)
            self.assertEqual(len(result["samples"]), 2)
            self.assertEqual(result["samples"][0]["cpu_load_percent"], 15)
            self.assertEqual(result["samples"][0]["memory_used_bytes"], 9.5 * 1024 ** 3)
            self.assertEqual(result["samples"][0]["memory_total_bytes"], 64 * 1024 ** 3)
            self.assertEqual(result["samples"][0]["cpu_frequency_mhz"], 3015)
            self.assertEqual(result["samples"][0]["disks"][0]["read_bytes_per_second"], 1500)
            self.assertEqual(result["samples"][0]["gpus"][0]["load_percent"], 50)
            self.assertEqual(result["samples"][0]["gpus"][0]["graphics_clock_mhz"], 2050)
            self.assertEqual(result["samples"][0]["gpus"][0]["power_w"], 150)
            self.assertEqual(
                result["samples"][0]["gpus"][0]["memory_utilization_percent"], 12.5
            )
            self.assertEqual(result["samples"][1]["cpu_load_percent"], 30)
            with reopened.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_schema_fourteen_history_rows_survive_current_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                connection.execute("INSERT INTO schema_version VALUES (14)")
                connection.execute(
                    """CREATE TABLE resource_gpu_samples (
                        sample_id INTEGER NOT NULL,
                        gpu_key TEXT NOT NULL,
                        temperature_c REAL,
                        PRIMARY KEY(sample_id, gpu_key)
                    )"""
                )
                connection.execute(
                    "INSERT INTO resource_gpu_samples VALUES (1, 'GPU-a', 62)"
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(path)
            with database.connect() as connection:
                version = connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()["version"]
                row = connection.execute(
                    "SELECT temperature_c,power_w,graphics_clock_mhz "
                    "FROM resource_gpu_samples WHERE sample_id=1"
                ).fetchone()

            self.assertEqual(version, 19)
            self.assertEqual(row["temperature_c"], 62)
            self.assertIsNone(row["power_w"])
            self.assertIsNone(row["graphics_clock_mhz"])

    def test_schema_fifteen_history_rows_survive_current_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                connection.execute("INSERT INTO schema_version VALUES (15)")
                connection.execute(
                    """CREATE TABLE resource_samples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sampled_at TEXT NOT NULL UNIQUE,
                        cpu_load_percent REAL,
                        cpu_temperature_c REAL,
                        memory_percent REAL
                    )"""
                )
                connection.execute(
                    "INSERT INTO resource_samples(sampled_at,memory_percent) VALUES (?,?)",
                    ("2026-08-28T09:00:00+00:00", 50),
                )
                connection.commit()
            finally:
                connection.close()

            database = Database(path)
            with database.connect() as connection:
                version = connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()["version"]
                row = connection.execute(
                    "SELECT memory_percent,memory_used_bytes,memory_total_bytes "
                    "FROM resource_samples"
                ).fetchone()

            self.assertEqual(version, 19)
            self.assertEqual(row["memory_percent"], 50)
            self.assertIsNone(row["memory_used_bytes"])
            self.assertIsNone(row["memory_total_bytes"])

    def test_resource_history_normalizes_equivalent_timestamps_to_utc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "manager.db")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            sampled_at = now - timedelta(minutes=1)
            first = {
                "sampled_at": sampled_at.astimezone(
                    timezone(timedelta(hours=8))
                ).isoformat(),
                "cpu_load_percent": 10,
                "gpus": [],
            }
            equivalent = {
                "sampled_at": sampled_at.isoformat().replace("+00:00", "Z"),
                "cpu_load_percent": 20,
                "gpus": [],
            }

            database.append_resource_sample(first)
            database.append_resource_sample(equivalent)
            result = database.query_resource_history(
                15, now=now
            )

            self.assertEqual(result["stored_sample_count"], 1)
            self.assertEqual(result["samples"][0]["sampled_at"], sampled_at.isoformat())
            self.assertEqual(result["samples"][0]["cpu_load_percent"], 20)

    def test_user_management_lists_resets_and_deletes_users(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "manager.db")
            auth = AuthService(database, 3600)
            admin_token, _, _ = auth.setup("admin", "1234", "127.0.0.1")
            created = auth.create_user("zzq", "5678", "127.0.0.1")
            zzq_token, _, _ = auth.login("zzq", "5678", "127.0.0.1")

            users = auth.list_users()
            self.assertEqual([user["username"] for user in users], ["admin", "zzq"])
            self.assertEqual([user["active_sessions"] for user in users], [1, 1])

            with self.assertRaisesRegex(AuthError, "至少需要 4"):
                auth.update_user_password(created["id"], "123", "admin", "127.0.0.1")

            updated = auth.update_user_password(
                created["id"], "8765", "admin", "127.0.0.1"
            )
            self.assertFalse(updated["current_session_invalidated"])
            with self.assertRaisesRegex(AuthError, "会话无效"):
                auth.authenticate(zzq_token)
            auth.login("zzq", "8765", "127.0.0.1")

            with self.assertRaisesRegex(AuthError, "当前登录用户"):
                auth.delete_user(1, "admin", "127.0.0.1")
            deleted = auth.delete_user(created["id"], "admin", "127.0.0.1")
            self.assertEqual(deleted["username"], "zzq")
            self.assertEqual([user["username"] for user in auth.list_users()], ["admin"])
            self.assertEqual(auth.authenticate(admin_token).username, "admin")
            with self.assertRaisesRegex(AuthError, "最后一个用户"):
                auth.delete_user(1, "other", "127.0.0.1")

            failures = {
                (item["event"], item["summary"].get("reason"))
                for item in database.list_audit(50) if item["result"] == "failure"
            }
            self.assertIn(("auth.user.password_update", "weak_password"), failures)
            self.assertIn(("auth.user.delete", "cannot_delete_current_user"), failures)
            self.assertIn(("auth.user.delete", "cannot_delete_last_user"), failures)

            self_update = auth.update_user_password(
                1, "4321", "admin", "127.0.0.1"
            )
            self.assertTrue(self_update["current_session_invalidated"])
            with self.assertRaisesRegex(AuthError, "会话无效"):
                auth.authenticate(admin_token)
            auth.login("admin", "4321", "127.0.0.1")

    def test_create_user_remains_loopback_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "manager.db")
            auth = AuthService(database, 3600)
            auth.setup("admin", "1234", "127.0.0.1")

            with self.assertRaisesRegex(AuthError, "仅允许从本机"):
                auth.create_user("zzq", "5678", "192.168.100.20")

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

    def test_http_health_probe_requires_expected_identity_text(self) -> None:
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"data":[{"id":"expected-model"}]}'
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        with patch("workstation_manager.registry.build_opener", return_value=opener):
            result = HttpHealthProbe().probe(
                "http://127.0.0.1:8000/v1/models", "expected-model"
            )
            mismatch = HttpHealthProbe().probe(
                "http://127.0.0.1:8000/v1/models", "other-model"
            )

        self.assertEqual(result.state, "running")
        self.assertEqual(mismatch.state, "unhealthy")
        self.assertTrue(mismatch.reachable)


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "manager.db")
        self.runner = FakeRunner()
        self.health_probe = FakeHealthProbe()
        self.manager = RegisteredServiceManager(
            self.database, self.runner, self.health_probe,
            health_interval_seconds=0.01,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        self.temp.cleanup()

    def make_script(self, name: str) -> Path:
        script = self.root / f"{name}.ps1"
        script.write_text("param($action)", encoding="utf-8")
        return script

    async def add_service(
        self, name: str, gpu: str = "", health_url: str = "",
        health_expect: str = "",
    ) -> dict:
        return await self.manager.create_service(
            {"name": name, "description": f"{name}说明",
             "script_path": str(self.make_script(name)), "gpu_label": gpu,
             "port": None, "ui_url": "", "health_url": health_url,
             "health_expect": health_expect}, "admin", "127.0.0.1"
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
        self.assertEqual(action_calls, [("A.ps1", "stop")])
        self.assertNotIn(("D.ps1", "stop"), action_calls)
        self.assertEqual(self.manager.list_scenes()[0]["state"], "active")

    async def test_default_scene_uses_the_normal_scene_activation_operation(self) -> None:
        service = await self.add_service("默认服务")
        scene = self.manager.create_scene(
            {"name": "默认工作", "description": "", "service_ids": [service["id"]]},
            "admin", "local",
        )

        self.assertIsNone(self.manager.submit_default_scene_activation())
        updated = self.manager.set_default_scene(scene["id"], True, "admin", "local")
        self.assertEqual(updated["is_default"], 1)
        operation_id = self.manager.submit_default_scene_activation()
        self.assertIsNotNone(operation_id)
        operation = await self.wait_operation(operation_id)

        self.assertEqual(operation["status"], "succeeded")
        self.assertEqual(operation["requested_by"], "system")
        self.assertEqual(operation["source_ip"], "startup")
        self.assertEqual(self.runner.calls[-1], ("默认服务.ps1", "start"))

        cleared = self.manager.set_default_scene(scene["id"], False, "admin", "local")
        self.assertEqual(cleared["is_default"], 0)
        self.assertIsNone(self.manager.submit_default_scene_activation())

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
        self.assertEqual(scene["state"], "inactive")

    async def test_scene_is_inactive_when_no_target_service_is_running(self) -> None:
        target = await self.add_service("目标")
        unrelated = await self.add_service("其他")
        self.runner.states[unrelated["script_path"]] = "running"
        await self.manager.refresh_status(unrelated)
        scene = self.manager.create_scene(
            {"name": "未启动场景", "description": "", "service_ids": [target["id"]]},
            "admin", "local",
        )

        self.assertEqual(scene["state"], "inactive")

    async def test_scene_is_partial_when_some_target_services_are_running(self) -> None:
        running = await self.add_service("已启动")
        stopped = await self.add_service("未启动")
        self.runner.states[running["script_path"]] = "running"
        await self.manager.refresh_status(running)
        scene = self.manager.create_scene(
            {"name": "部分场景", "description": "", "service_ids": [running["id"], stopped["id"]]},
            "admin", "local",
        )

        self.assertEqual(scene["state"], "partial")

    async def test_scene_is_partial_when_unrelated_service_is_running(self) -> None:
        target = await self.add_service("目标")
        unrelated = await self.add_service("其他")
        self.runner.states[target["script_path"]] = "running"
        self.runner.states[unrelated["script_path"]] = "running"
        await self.manager.refresh_status(target)
        await self.manager.refresh_status(unrelated)
        scene = self.manager.create_scene(
            {"name": "存在额外服务", "description": "", "service_ids": [target["id"]]},
            "admin", "local",
        )

        self.assertEqual(scene["state"], "partial")

    async def test_empty_scene_keeps_existing_state_rules(self) -> None:
        running = await self.add_service("运行中")
        scene = self.manager.create_scene(
            {"name": "空场景", "description": "", "service_ids": []},
            "admin", "local",
        )
        self.assertEqual(scene["state"], "active")

        self.runner.states[running["script_path"]] = "running"
        await self.manager.refresh_status(running)

        self.assertEqual(self.manager.list_scenes()[0]["state"], "partial")

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

    async def test_health_probe_tracks_observed_state_without_running_status_script(self) -> None:
        health_url = "http://127.0.0.1:18030/health"
        service = await self.add_service("自动健康", health_url=health_url)
        self.health_probe.results[health_url] = HealthProbeResult("running", None, True)

        await self.manager.refresh_all_health()

        item = self.manager.list_services()[0]
        self.assertEqual(item["status"]["state"], "running")
        self.assertEqual(item["status"]["source"], "health")
        self.assertEqual(item["desired_state"], "unknown")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.health_probe.calls, [(health_url, "")])

        self.health_probe.results[health_url] = HealthProbeResult(
            "stopped", None, False
        )
        await self.manager.refresh_all_health()
        self.assertEqual(self.manager.list_services()[0]["status"]["state"], "running")
        await self.manager.refresh_all_health()
        self.assertEqual(self.manager.list_services()[0]["status"]["state"], "stopped")

    async def test_action_records_desired_state_and_immediately_verifies_health(self) -> None:
        health_url = "http://127.0.0.1:8000/v1/models"
        service = await self.add_service(
            "健康动作", health_url=health_url, health_expect="expected-model"
        )
        self.health_probe.results[health_url] = HealthProbeResult("running", None, True)

        operation_id = self.manager.submit_service_action(
            service["id"], "start", "admin", "local"
        )

        self.assertEqual((await self.wait_operation(operation_id))["status"], "succeeded")
        item = self.manager.list_services()[0]
        self.assertEqual(item["desired_state"], "running")
        self.assertEqual(item["status"]["state"], "running")
        self.assertEqual(self.runner.calls, [("健康动作.ps1", "start")])
        self.assertEqual(self.health_probe.calls, [(health_url, "expected-model")])

    async def test_unreachable_timeout_uses_desired_state_without_false_alarm(self) -> None:
        health_url = "http://127.0.0.1:18090/health"
        service = await self.add_service("转发超时", health_url=health_url)
        self.health_probe.results[health_url] = HealthProbeResult(
            "unknown", "健康接口响应超时", False
        )

        self.assertTrue(
            self.database.update_registered_service_desired_state(service["id"], "stopped")
        )
        await self.manager.refresh_all_health()
        await self.manager.refresh_all_health()
        stopped = self.manager.list_services()[0]
        self.assertEqual(stopped["status"]["state"], "stopped")
        self.assertIsNone(stopped["status"]["error"])

        self.assertTrue(
            self.database.update_registered_service_desired_state(service["id"], "running")
        )
        await self.manager.refresh_all_health()
        await self.manager.refresh_all_health()
        running = self.manager.list_services()[0]
        self.assertEqual(running["status"]["state"], "unhealthy")
        self.assertEqual(running["status"]["error"], "健康接口响应超时")

    async def test_shared_port_identity_mismatch_is_stopped_when_peer_is_running(self) -> None:
        first_url = "http://127.0.0.1:8000/v1/models"
        second_url = "http://127.0.0.1:8000/system_stats"
        first = await self.add_service("vLLM", health_url=first_url, health_expect="model")
        second = await self.add_service("ComfyUI", health_url=second_url, health_expect="RTX")
        self.health_probe.results[first_url] = HealthProbeResult("running", None, True)
        self.health_probe.results[second_url] = HealthProbeResult(
            "unhealthy", "健康接口响应与服务身份不匹配", True
        )

        await self.manager.refresh_service_health(first, immediate=True)
        await self.manager.refresh_service_health(second, immediate=True)

        states = {item["name"]: item["status"]["state"] for item in self.manager.list_services()}
        self.assertEqual(states, {"ComfyUI": "stopped", "vLLM": "running"})

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

    async def test_manager_background_health_check_never_runs_status_script(self) -> None:
        runner = BlockingStatusRunner()
        probe = FakeHealthProbe()
        manager = RegisteredServiceManager(
            self.database, runner, probe, health_interval_seconds=0.01
        )
        script = self.make_script("后台健康")
        health_url = "http://127.0.0.1:18090/health"
        probe.results[health_url] = HealthProbeResult("running", None, True)
        await manager.create_service(
            {"name": "后台健康", "description": "", "script_path": str(script),
             "gpu_label": "", "port": 18090, "ui_url": "",
             "health_url": health_url, "health_expect": ""}, "admin", "local"
        )
        await manager.start()
        for _ in range(100):
            if probe.calls:
                break
            await asyncio.sleep(0.01)

        self.assertTrue(probe.calls)
        self.assertEqual(runner.calls, [])
        await manager.shutdown()


class ApiRegistryTests(unittest.TestCase):
    def test_application_startup_checks_for_a_default_scene_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(
                database_path=root / "manager.db", manager_log_path=root / "manager.log",
                sample_interval_seconds=60,
            )
            database = Database(settings.database_path)
            registry = RegisteredServiceManager(database, FakeRunner(), FakeHealthProbe())
            sampler = Sampler(settings, collector=lambda _: {
                "sampled_at": "2099-01-01T00:00:00+00:00",
                "host": {"cpu": {}, "memory": {}, "disks": []}, "gpus": [],
                "docker": {"containers": []}, "ports": [], "collector_errors": [],
            })

            with patch.object(
                registry, "submit_default_scene_activation",
                wraps=registry.submit_default_scene_activation,
            ) as trigger:
                with TestClient(create_app(settings, sampler, database, registry)):
                    trigger.assert_called_once_with()

    def test_authenticated_service_and_scene_crud(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "manage.ps1"
            script.write_text("param($action)", encoding="utf-8")
            settings = Settings(database_path=root / "manager.db", manager_log_path=root / "manager.log",
                                sample_interval_seconds=60)
            database = Database(settings.database_path)
            registry = RegisteredServiceManager(database, FakeRunner(), FakeHealthProbe())
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
                    "health_url": "http://127.0.0.1:8080/v1/models",
                    "health_expect": "api-model",
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
                defaulted = client.put(
                    f"/api/v1/scenes/{scene.json()['id']}/default", headers=headers
                )
                self.assertEqual(defaulted.status_code, 200, defaulted.text)
                self.assertEqual(defaulted.json()["is_default"], 1)
                cleared = client.delete(
                    f"/api/v1/scenes/{scene.json()['id']}/default", headers=headers
                )
                self.assertEqual(cleared.status_code, 200, cleared.text)
                self.assertEqual(cleared.json()["is_default"], 0)
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
                self.assertEqual(service_list["status_mode"], "health")
                self.assertNotIn("poll_interval_seconds", service_list)
                checked = client.post(
                    f"/api/v1/registered-services/{service_id}/status", headers=headers
                )
                self.assertEqual(checked.status_code, 200, checked.text)
                deleted = client.delete(f"/api/v1/registered-services/{service_id}", headers=headers)
                self.assertEqual(deleted.status_code, 204)
                self.assertEqual(client.get("/api/v1/scenes").json()["scenes"][0]["service_ids"], [])

    def test_authenticated_user_management(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(
                database_path=root / "manager.db", manager_log_path=root / "manager.log",
                sample_interval_seconds=60,
            )
            database = Database(settings.database_path)
            sampler = Sampler(settings, collector=lambda _: {
                "sampled_at": "2099-01-01T00:00:00+00:00",
                "host": {"cpu": {}, "memory": {}, "disks": []}, "gpus": [],
                "docker": {"containers": []}, "ports": [], "collector_errors": [],
            })
            with TestClient(create_app(settings, sampler, database),
                            client=("127.0.0.1", 50000)) as client:
                self.assertEqual(client.get("/api/v1/users").status_code, 401)
                setup = client.post(
                    "/api/v1/auth/setup", json={"username": "admin", "password": "1234"}
                )
                headers = {"X-CSRF-Token": setup.json()["csrf_token"]}

                initial = client.get("/api/v1/users")
                self.assertEqual(initial.status_code, 200)
                self.assertTrue(initial.json()["users"][0]["is_current"])
                self.assertEqual(client.post(
                    "/api/v1/users", json={"username": "none", "password": "1234"}
                ).status_code, 403)
                self.assertEqual(client.post(
                    "/api/v1/users", headers={"X-CSRF-Token": "wrong"},
                    json={"username": "none", "password": "1234"},
                ).status_code, 403)
                created = client.post(
                    "/api/v1/users", headers=headers,
                    json={"username": "zzq", "password": "5678"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                user_id = created.json()["id"]
                self.assertEqual(
                    [user["username"] for user in client.get("/api/v1/users").json()["users"]],
                    ["admin", "zzq"],
                )

                changed = client.put(
                    f"/api/v1/users/{user_id}/password", headers=headers,
                    json={"password": "8765"},
                )
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertFalse(changed.json()["current_session_invalidated"])
                self.assertEqual(
                    client.delete("/api/v1/users/1", headers=headers).status_code, 409
                )
                self.assertEqual(
                    client.delete(f"/api/v1/users/{user_id}", headers=headers).status_code, 204
                )
                self.assertEqual(len(client.get("/api/v1/users").json()["users"]), 1)

    def test_remote_user_creation_is_rejected_by_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(
                database_path=root / "manager.db", manager_log_path=root / "manager.log",
                sample_interval_seconds=60,
            )
            database = Database(settings.database_path)
            AuthService(database, 3600).setup("admin", "1234", "127.0.0.1")
            sampler = Sampler(settings, collector=lambda _: {
                "sampled_at": "2099-01-01T00:00:00+00:00",
                "host": {"cpu": {}, "memory": {}, "disks": []}, "gpus": [],
                "docker": {"containers": []}, "ports": [], "collector_errors": [],
            })
            with TestClient(create_app(settings, sampler, database),
                            client=("192.168.100.20", 50000)) as client:
                login = client.post(
                    "/api/v1/auth/login", json={"username": "admin", "password": "1234"}
                )
                response = client.post(
                    "/api/v1/users", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                    json={"username": "zzq", "password": "5678"},
                )

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "loopback_required")

    def test_delete_user_serializes_with_login(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(
                database_path=root / "manager.db", manager_log_path=root / "manager.log",
                sample_interval_seconds=60,
            )
            database = Database(settings.database_path)
            sampler = Sampler(settings, collector=lambda _: {
                "sampled_at": "2099-01-01T00:00:00+00:00",
                "host": {"cpu": {}, "memory": {}, "disks": []}, "gpus": [],
                "docker": {"containers": []}, "ports": [], "collector_errors": [],
            })
            app = create_app(settings, sampler, database)
            auth = AuthService(database, settings.session_ttl_seconds)
            admin_token, csrf_token, _ = auth.setup("admin", "1234", "127.0.0.1")
            created = auth.create_user("zzq", "5678", "127.0.0.1")
            original_delete = app.state.auth.delete_user
            delete_started = threading.Event()
            release_delete = threading.Event()

            def blocking_delete(*args: Any, **kwargs: Any) -> dict[str, Any]:
                delete_started.set()
                if not release_delete.wait(2):
                    raise RuntimeError("delete concurrency test timeout")
                return original_delete(*args, **kwargs)

            app.state.auth.delete_user = blocking_delete

            async def exercise_concurrency() -> tuple[Any, Any]:
                async with app.router.lifespan_context(app):
                    transport = ASGITransport(app=app, client=("127.0.0.1", 50000))
                    async with AsyncClient(
                        transport=transport, base_url="http://testserver",
                        cookies={SESSION_COOKIE: admin_token},
                    ) as admin_client, AsyncClient(
                        transport=transport, base_url="http://testserver"
                    ) as login_client:
                        delete_task = asyncio.create_task(admin_client.delete(
                            f"/api/v1/users/{created['id']}",
                            headers={"X-CSRF-Token": csrf_token},
                        ))
                        self.assertTrue(await asyncio.to_thread(delete_started.wait, 1))
                        login_task = asyncio.create_task(login_client.post(
                            "/api/v1/auth/login",
                            json={"username": "zzq", "password": "5678"},
                        ))
                        await asyncio.sleep(0.05)
                        self.assertFalse(login_task.done())
                        release_delete.set()
                        return await delete_task, await login_task

            deleted, login = asyncio.run(exercise_concurrency())
            self.assertEqual(deleted.status_code, 204, deleted.text)
            self.assertEqual(login.status_code, 401, login.text)


if __name__ == "__main__":
    unittest.main()
