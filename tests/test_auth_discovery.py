from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from workstation_manager.app import create_app
from workstation_manager.auth import AuthError, AuthService
from workstation_manager.config import ConfigError, Settings
from workstation_manager.database import SCHEMA_VERSION, Database, DatabaseError
from workstation_manager.discovery import ScriptDiscovery, read_shortcut
from workstation_manager import discovery as discovery_module
from workstation_manager.history import Sampler


def fake_snapshot(_: Settings) -> dict:
    return {
        "sampled_at": "2099-01-01T00:00:00+00:00",
        "host": {"cpu": {}, "memory": {}},
        "gpus": [],
        "docker": {"containers": []},
        "ports": [],
        "collector_errors": [],
    }


class AuthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(
            sample_interval_seconds=60,
            database_path=self.root / "manager.db",
            discovery_scripts_path=self.root / "scripts",
            scan_scripts_on_startup=False,
        )
        sampler = Sampler(self.settings, collector=fake_snapshot)
        self.context = TestClient(
            create_app(self.settings, sampler), client=("127.0.0.1", 50000)
        )
        self.client = self.context.__enter__()

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def setup_admin(self) -> tuple[str, str]:
        response = self.client.post(
            "/api/v1/auth/setup",
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["csrf_token"], response.headers["set-cookie"]

    def test_setup_cookie_hashes_session_and_requires_csrf(self) -> None:
        csrf, cookie = self.setup_admin()
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        raw_token = self.client.cookies.get("wm_session")
        connection = sqlite3.connect(self.settings.database_path)
        try:
            stored_token, stored_csrf = connection.execute(
                "SELECT token_hash, csrf_hash FROM sessions"
            ).fetchone()
        finally:
            connection.close()
        self.assertNotEqual(stored_token, raw_token)
        self.assertNotEqual(stored_csrf, csrf)
        self.assertNotIn(raw_token, self.settings.database_path.read_bytes().decode("latin1"))

        rejected = self.client.post("/api/v1/discovery/scripts/scan")
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json()["error"]["code"], "invalid_csrf")
        accepted = self.client.post(
            "/api/v1/discovery/scripts/scan", headers={"X-CSRF-Token": csrf}
        )
        self.assertEqual(accepted.status_code, 200)

    def test_route_access_matrix_before_and_after_setup(self) -> None:
        self.assertEqual(self.client.get("/api/v1/snapshot").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/discovery/scripts").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/audit").status_code, 401)
        self.setup_admin()
        self.assertIsNone(self.client.get("/api/v1/discovery/scripts").json()["latest_scan"])
        anonymous = TestClient(self.client.app, client=("127.0.0.1", 50001))
        self.assertEqual(anonymous.get("/api/v1/health").status_code, 200)
        self.assertEqual(anonymous.get("/api/v1/auth/status").status_code, 200)
        for path in (
            "/api/v1/snapshot",
            "/api/v1/history",
            "/api/v1/services",
            "/api/v1/discovery/scripts",
            "/api/v1/audit",
            "/api/v1/auth/me",
        ):
            with self.subTest(path=path):
                self.assertEqual(anonymous.get(path).status_code, 401)
        self.assertEqual(anonymous.post("/api/v1/auth/logout").status_code, 401)

    def test_login_logout_and_protected_routes(self) -> None:
        csrf, _ = self.setup_admin()
        me = self.client.get("/api/v1/auth/me").json()
        self.assertEqual(me["username"], "admin")
        self.assertNotEqual(me["csrf_token"], csrf)
        logout = self.client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": me["csrf_token"]}
        )
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/snapshot").status_code, 401)

        failure = self.client.post(
            "/api/v1/auth/login", json={"username": "unknown", "password": "wrong"}
        )
        self.assertEqual(failure.status_code, 401)
        self.assertEqual(failure.json()["error"]["message"], "用户名或密码错误")
        login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/snapshot").status_code, 200)

    def test_auth_me_adds_csrf_for_multiple_tabs_without_storing_plaintext(self) -> None:
        old_csrf, _ = self.setup_admin()
        me = self.client.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 200)
        new_csrf = me.json()["csrf_token"]
        self.assertNotEqual(new_csrf, old_csrf)
        database_bytes = self.settings.database_path.read_bytes()
        self.assertNotIn(old_csrf.encode(), database_bytes)
        self.assertNotIn(new_csrf.encode(), database_bytes)

        first_tab = self.client.post(
            "/api/v1/discovery/scripts/scan", headers={"X-CSRF-Token": old_csrf}
        )
        self.assertEqual(first_tab.status_code, 200)
        second_tab = self.client.post(
            "/api/v1/discovery/scripts/scan", headers={"X-CSRF-Token": new_csrf}
        )
        self.assertEqual(second_tab.status_code, 200)

        connection = sqlite3.connect(self.settings.database_path)
        try:
            connection.execute(
                "UPDATE sessions SET expires_at = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 401)

    def test_ninth_csrf_token_evicts_oldest_and_logout_cascades(self) -> None:
        oldest, _ = self.setup_admin()
        issued = [oldest]
        for _ in range(8):
            issued.append(self.client.get("/api/v1/auth/me").json()["csrf_token"])
        connection = sqlite3.connect(self.settings.database_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM session_csrf_tokens").fetchone()[0],
                8,
            )
        finally:
            connection.close()
        rejected = self.client.post(
            "/api/v1/discovery/scripts/scan", headers={"X-CSRF-Token": issued[0]}
        )
        self.assertEqual(rejected.status_code, 403)
        accepted = self.client.post(
            "/api/v1/discovery/scripts/scan", headers={"X-CSRF-Token": issued[1]}
        )
        self.assertEqual(accepted.status_code, 200)
        logout = self.client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": issued[-1]}
        )
        self.assertEqual(logout.status_code, 200)
        connection = sqlite3.connect(self.settings.database_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM session_csrf_tokens").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_expired_session_is_rejected(self) -> None:
        self.setup_admin()
        connection = sqlite3.connect(self.settings.database_path)
        try:
            connection.execute(
                "UPDATE sessions SET expires_at = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
            )
            connection.commit()
        finally:
            connection.close()
        response = self.client.get("/api/v1/snapshot")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_session")
        connection = sqlite3.connect(self.settings.database_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM session_csrf_tokens").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_audit_is_bounded_and_contains_no_secrets(self) -> None:
        password = "correct horse battery staple"
        csrf, _ = self.setup_admin()
        self.client.post("/api/v1/auth/login", json={"username": "admin", "password": "bad"})
        response = self.client.get("/api/v1/audit?limit=500")
        self.assertEqual(response.status_code, 422)
        response = self.client.get("/api/v1/audit?limit=20")
        self.assertEqual(response.status_code, 200)
        serialized = json.dumps(response.json(), ensure_ascii=False)
        self.assertNotIn(password, serialized)
        self.assertNotIn(csrf, serialized)
        self.assertNotIn(self.client.cookies.get("wm_session"), serialized)

    def test_database_state_survives_app_recreation(self) -> None:
        self.setup_admin()
        second_settings = self.settings
        sampler = Sampler(second_settings, collector=fake_snapshot)
        with TestClient(
            create_app(second_settings, sampler), client=("127.0.0.1", 50000)
        ) as second:
            status = second.get("/api/v1/auth/status")
            self.assertTrue(status.json()["configured"])
            self.assertEqual(second.get("/api/v1/snapshot").status_code, 401)

    def test_setup_rolls_back_when_success_audit_cannot_be_written(self) -> None:
        with patch.object(
            self.client.app.state.database,
            "insert_audit",
            side_effect=sqlite3.OperationalError("audit unavailable"),
        ):
            response = self.client.post(
                "/api/v1/auth/setup",
                json={"username": "admin", "password": "correct horse battery staple"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertFalse(self.client.get("/api/v1/auth/status").json()["configured"])

    def test_login_failures_are_rate_limited(self) -> None:
        csrf, _ = self.setup_admin()
        self.client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
        for _ in range(5):
            response = self.client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
            )
            self.assertEqual(response.status_code, 401)
        limited = self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
        )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["error"]["code"], "rate_limited")

    def test_concurrent_login_failures_are_counted_atomically(self) -> None:
        self.setup_admin()
        barrier = threading.Barrier(6)

        def fail_login(_: int) -> int:
            barrier.wait()
            try:
                self.client.app.state.auth.login("admin", "wrong", "10.0.0.8")
            except AuthError as exc:
                return exc.status_code
            return 200

        with ThreadPoolExecutor(max_workers=6) as executor:
            statuses = list(executor.map(fail_login, range(6)))
        self.assertEqual(statuses.count(401), 5)
        self.assertEqual(statuses.count(429), 1)

    def test_setup_rejects_non_loopback_and_secure_cookie_is_configurable(self) -> None:
        other_root = self.root / "remote"
        settings = Settings(
            sample_interval_seconds=60,
            database_path=other_root / "manager.db",
            discovery_scripts_path=other_root / "scripts",
            cookie_secure=True,
            scan_scripts_on_startup=False,
        )
        sampler = Sampler(settings, collector=fake_snapshot)
        app = create_app(settings, sampler)
        with TestClient(app, client=("192.168.1.25", 50000)) as remote:
            self.assertEqual(remote.get("/api/v1/health").status_code, 200)
            self.assertEqual(remote.get("/api/v1/snapshot").status_code, 403)
            denied = remote.post(
                "/api/v1/auth/setup", json={"username": "admin", "password": "long enough password"}
            )
            self.assertEqual(denied.status_code, 403)
        with TestClient(app, client=("127.0.0.1", 50000)) as local:
            accepted = local.post(
                "/api/v1/auth/setup", json={"username": "admin", "password": "long enough password"}
            )
            self.assertEqual(accepted.status_code, 201)
            self.assertIn("Secure", accepted.headers["set-cookie"])

    def test_uninitialized_database_cannot_bind_lan(self) -> None:
        settings = Settings(
            host="0.0.0.0",
            database_path=self.root / "lan" / "manager.db",
            discovery_scripts_path=self.root / "scripts",
            scan_scripts_on_startup=False,
        )
        with self.assertRaisesRegex(ConfigError, "loopback"):
            create_app(settings, Sampler(settings, collector=fake_snapshot))

    def test_extra_fields_and_chunked_oversized_body_are_rejected(self) -> None:
        extra = self.client.post(
            "/api/v1/auth/setup",
            json={
                "username": "admin",
                "password": "correct horse battery staple",
                "unexpected": True,
            },
        )
        self.assertEqual(extra.status_code, 422)

        settings = Settings(
            sample_interval_seconds=60,
            database_path=self.root / "body" / "manager.db",
            discovery_scripts_path=self.root / "body" / "scripts",
            request_body_max_bytes=32,
            scan_scripts_on_startup=False,
        )
        sampler = Sampler(settings, collector=fake_snapshot)
        with TestClient(
            create_app(settings, sampler), client=("127.0.0.1", 50003)
        ) as client:
            chunks = iter([b'{"username":"admin",', b'"password":"' + b"x" * 100 + b'"}'])
            response = client.post(
                "/api/v1/auth/setup",
                content=chunks,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json()["error"]["code"], "request_body_too_large")

    def test_pbkdf2_and_scan_run_outside_event_loop(self) -> None:
        original_setup = self.client.app.state.auth.setup

        def checked_setup(*args) -> tuple[str, str, str]:
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return original_setup(*args)

        with patch.object(self.client.app.state.auth, "setup", side_effect=checked_setup):
            response = self.client.post(
                "/api/v1/auth/setup",
                json={"username": "admin", "password": "correct horse battery staple"},
            )
        self.assertEqual(response.status_code, 201)
        csrf = response.json()["csrf_token"]
        original_scan = self.client.app.state.discovery.scan

        def checked_scan() -> dict:
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return original_scan()

        with patch.object(self.client.app.state.discovery, "scan", side_effect=checked_scan):
            scan = self.client.post(
                "/api/v1/discovery/scripts/scan", headers={"X-CSRF-Token": csrf}
            )
            self.assertEqual(scan.status_code, 200)

    def test_discovery_api_redacts_configured_path_and_database_errors(self) -> None:
        settings = Settings(
            sample_interval_seconds=60,
            database_path=self.root / "api-redaction" / "manager.db",
            manager_log_path=self.root / "api-redaction" / "manager.log",
            discovery_scripts_path=self.root / "token=DIRECTORYSECRET" / "scripts",
            scan_scripts_on_startup=False,
        )
        sampler = Sampler(settings, collector=fake_snapshot)
        with TestClient(
            create_app(settings, sampler), client=("127.0.0.1", 50004)
        ) as client:
            setup = client.post(
                "/api/v1/auth/setup",
                json={"username": "admin", "password": "correct horse battery staple"},
            )
            self.assertEqual(setup.status_code, 201)
            response = client.get("/api/v1/discovery/scripts")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("DIRECTORYSECRET", response.text)
            with patch.object(
                client.app.state.database,
                "list_discovered",
                side_effect=DatabaseError(r"cannot open D:\token=DBSECRET\manager.db prefix Cookie: api-cookie-secret"),
            ):
                failed = client.get("/api/v1/discovery/scripts")
            self.assertEqual(failed.status_code, 500)
            self.assertNotIn("DBSECRET", failed.text)
            self.assertNotIn("api-cookie-secret", failed.text)
            for handler in client.app.state.manager_logger.handlers:
                handler.flush()
            self.assertNotIn("api-cookie-secret", settings.manager_log_path.read_text(encoding="utf-8"))

    def test_concurrent_setup_has_one_success_and_one_conflict(self) -> None:
        database = Database(self.root / "setup-race" / "manager.db")
        auth = AuthService(database, 3600)
        barrier = threading.Barrier(2)

        def setup(index: int) -> int:
            barrier.wait()
            try:
                auth.setup(f"admin{index}", "correct horse battery staple", "127.0.0.1")
                return 201
            except AuthError as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(setup, range(2)))
        self.assertEqual(sorted(statuses), [201, 409])
        events = database.list_audit(10)
        setup_events = [event for event in events if event["event"] == "auth.setup"]
        self.assertEqual({event["result"] for event in setup_events}, {"success", "failure"})

    def test_expired_sessions_are_cleaned_and_active_sessions_bounded(self) -> None:
        database = Database(self.root / "sessions" / "manager.db")
        auth = AuthService(database, 3600, session_max_active=2)
        auth.setup("admin", "correct horse battery staple", "127.0.0.1")
        auth.login("admin", "correct horse battery staple", "127.0.0.1")
        auth.login("admin", "correct horse battery staple", "127.0.0.1")
        connection = sqlite3.connect(database.path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)
            connection.execute(
                "UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'"
            )
            connection.commit()
        finally:
            connection.close()
        auth.login("admin", "correct horse battery staple", "127.0.0.1")
        connection = sqlite3.connect(database.path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        finally:
            connection.close()

    def test_valid_authentication_is_read_only_and_expired_delete_is_token_scoped(self) -> None:
        self.setup_admin()
        token = self.client.cookies.get("wm_session")
        statements: list[str] = []
        original_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch("workstation_manager.database.sqlite3.connect", side_effect=traced_connect):
            session = self.client.app.state.auth.authenticate(token)
        self.assertEqual(session.username, "admin")
        self.assertFalse(any(statement.lstrip().upper().startswith(("DELETE", "BEGIN")) for statement in statements))

        connection = sqlite3.connect(self.settings.database_path)
        try:
            connection.execute(
                "UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE token_hash = ?",
                (session.token_hash,),
            )
            connection.commit()
        finally:
            connection.close()
        statements.clear()
        with patch("workstation_manager.database.sqlite3.connect", side_effect=traced_connect):
            with self.assertRaises(AuthError):
                self.client.app.state.auth.authenticate(token)
        deletes = [statement for statement in statements if statement.lstrip().upper().startswith("DELETE")]
        self.assertGreaterEqual(len(deletes), 1)
        self.assertTrue(all("WHERE token_hash" in statement for statement in deletes))

    def test_empty_and_missing_scans_persist_latest_run_across_restart(self) -> None:
        csrf, _ = self.setup_admin()
        missing = self.client.post(
            "/api/v1/discovery/scripts/scan", headers={"X-CSRF-Token": csrf}
        )
        self.assertFalse(missing.json()["directory_exists"])
        self.settings.discovery_scripts_path.mkdir()
        empty = self.client.post(
            "/api/v1/discovery/scripts/scan", headers={"X-CSRF-Token": csrf}
        )
        self.assertTrue(empty.json()["directory_exists"])
        latest = self.client.get("/api/v1/discovery/scripts").json()["latest_scan"]
        self.assertEqual(latest["entry_count"], 0)
        self.assertEqual(latest["error_count"], 0)
        connection = sqlite3.connect(self.settings.database_path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0], 2)
        finally:
            connection.close()

        sampler = Sampler(self.settings, collector=fake_snapshot)
        with TestClient(
            create_app(self.settings, sampler), client=("127.0.0.1", 50002)
        ) as restarted:
            login = restarted.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "correct horse battery staple"},
            )
            self.assertEqual(login.status_code, 200)
            restored = restarted.get("/api/v1/discovery/scripts").json()["latest_scan"]
            self.assertEqual(restored["scan_id"], latest["scan_id"])


class DatabaseMigrationTests(unittest.TestCase):
    def test_version_four_csrf_hash_is_migrated_and_cascades(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "v4.db"
            Database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP TABLE session_csrf_tokens")
                connection.execute("UPDATE schema_version SET version = 4")
                connection.execute(
                    "INSERT INTO admin_user(id, username, password_hash, password_salt, iterations, created_at) VALUES (1, 'admin', X'01', X'02', 1, 'now')"
                )
                connection.execute(
                    "INSERT INTO sessions(token_hash, admin_id, csrf_hash, created_at, expires_at, source_ip) VALUES ('session-hash', 1, 'legacy-csrf-hash', 'now', '2099-01-01T00:00:00+00:00', '127.0.0.1')"
                )
                connection.commit()
            finally:
                connection.close()
            Database(path)
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                row = connection.execute(
                    "SELECT session_token_hash, csrf_hash FROM session_csrf_tokens"
                ).fetchone()
                self.assertEqual(row, ("session-hash", "legacy-csrf-hash"))
                connection.execute("DELETE FROM sessions WHERE token_hash = 'session-hash'")
                connection.commit()
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM session_csrf_tokens").fetchone()[0],
                    0,
                )
            finally:
                connection.close()

    def test_legacy_schema_is_migrated_without_losing_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "old.db"
            connection = sqlite3.connect(path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE schema_version(version INTEGER NOT NULL);
                    INSERT INTO schema_version VALUES (0);
                    CREATE TABLE admin_user(
                        id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                        password_hash BLOB NOT NULL
                    );
                    INSERT INTO admin_user VALUES (1, 'legacy', X'01');
                    CREATE TABLE audit_events(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                        source_ip TEXT NOT NULL, event TEXT NOT NULL, result TEXT NOT NULL
                    );
                    INSERT INTO audit_events(created_at, source_ip, event, result)
                    VALUES ('2020-01-01T00:00:00+00:00', '127.0.0.1', 'legacy', 'success');
                    CREATE TABLE discovered_entries(
                        path TEXT PRIMARY KEY, name TEXT NOT NULL, entry_type TEXT NOT NULL,
                        mtime TEXT NOT NULL, sha256 TEXT NOT NULL, details_json TEXT NOT NULL
                    );
                    INSERT INTO discovered_entries VALUES(
                        'D:\\old.cmd', 'old.cmd', 'cmd', 'old-time', 'old-hash',
                        '{"name":"old.cmd"}'
                    );
                    """
                )
                connection.commit()
            finally:
                connection.close()

            Database(path)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(connection.execute("SELECT username FROM admin_user").fetchone()[0], "legacy")
                self.assertEqual(connection.execute("SELECT event FROM audit_events").fetchone()[0], "legacy")
                self.assertEqual(
                    connection.execute("SELECT name FROM discovered_entries").fetchone()[0], "old.cmd"
                )
                tables = {
                    row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                self.assertIn("scan_runs", tables)
                self.assertIn("login_failures", tables)
                self.assertIn("session_csrf_tokens", tables)
                admin_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(admin_user)")
                }
                self.assertIn("iterations", admin_columns)
                self.assertIn("created_at", admin_columns)
                self.assertIn("password_salt", admin_columns)
            finally:
                connection.close()

    def test_failed_migration_rolls_back_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "retry.db"
            Database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DROP TABLE scan_runs")
                connection.execute("DROP TABLE login_failures")
                connection.execute("DROP TABLE session_csrf_tokens")
                connection.execute("UPDATE schema_version SET version = 2")
                connection.commit()
            finally:
                connection.close()

            def fail_migration(connection: sqlite3.Connection) -> None:
                connection.execute("CREATE TABLE migration_probe(value INTEGER)")
                raise sqlite3.OperationalError("simulated migration failure")

            with patch.object(Database, "_migrate_to_3", side_effect=fail_migration):
                with self.assertRaises(DatabaseError):
                    Database(path)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone()[0], 2)
                probe = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_probe'"
                ).fetchone()
                self.assertIsNone(probe)
            finally:
                connection.close()
            Database(path)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone()[0], SCHEMA_VERSION)
            finally:
                connection.close()


class DatabaseRetentionTests(unittest.TestCase):
    def test_login_failures_and_audit_events_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(
                Path(temporary) / "retention.db",
                audit_retention_max_events=5,
                audit_retention_days=30,
                login_failure_max_rows=7,
            )
            for index in range(20):
                database.append_audit("127.0.0.1", "test", "success", {"index": index})
            since = "2000-01-01T00:00:00+00:00"
            for _ in range(20):
                database.record_login_failure_atomic("127.0.0.1", since, 0)
            connection = sqlite3.connect(database.path)
            try:
                self.assertLessEqual(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 5)
                self.assertLessEqual(connection.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0], 7)
                connection.execute(
                    "INSERT INTO login_failures(source_ip, created_at) VALUES (?, ?)",
                    ("old", "1999-01-01T00:00:00+00:00"),
                )
                connection.commit()
            finally:
                connection.close()

    def test_database_redacts_raw_discovery_and_audit_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "redacted.db")
            scan = {
                "scan_id": "scan-1",
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "directory": r"D:\root\token=DIRECTORYSECRET\scripts",
                "directory_exists": True,
                "entries": [
                    {
                        "path": r"D:\root\token=PATHSECRET\run.cmd",
                        "name": "run.cmd",
                        "type": "cmd",
                        "mtime": "now",
                        "sha256": "hash",
                        "errors": [],
                        "working_directories": [r"D:\work\password=WORKSECRET\models"],
                    }
                ],
                "errors": [],
            }
            database.replace_discovered_with_audit(
                scan,
                "127.0.0.1",
                "success",
                {"note": "--api-key AUDITSECRET prefix Cookie: audit-cookie-secret", "cookie": "json-cookie-secret"},
            )
            persisted = database.path.read_bytes()
            for secret in (b"DIRECTORYSECRET", b"PATHSECRET", b"WORKSECRET", b"AUDITSECRET", b"audit-cookie-secret", b"json-cookie-secret"):
                self.assertNotIn(secret, persisted)
            self.assertIn("scripts", database.latest_scan_run()["directory"])
            self.assertIn("run.cmd", database.list_discovered()[0]["path"])
            recent_since = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            database.record_login_failure_atomic("127.0.0.1", recent_since, 999)
            connection = sqlite3.connect(database.path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM login_failures WHERE source_ip='old'").fetchone()[0],
                    0,
                )
            finally:
                connection.close()

class DiscoveryTests(unittest.TestCase):
    def test_eof_and_parse_deadline_overruns_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "slow.cmd"
            script.write_text("echo ok", encoding="utf-8")
            file_stat = script.stat()
            clock = [0.0]

            class SlowEofStream(io.BytesIO):
                def fileno(self) -> int:
                    return 123

                def read(self, size: int = -1) -> bytes:
                    clock[0] = 0.5
                    return b""

            with patch.object(
                Path, "open", autospec=True, return_value=SlowEofStream()
            ), patch(
                "workstation_manager.discovery.os.fstat", return_value=file_stat
            ), patch(
                "workstation_manager.discovery.time.monotonic", side_effect=lambda: clock[0]
            ):
                with self.assertRaises(TimeoutError):
                    discovery_module._read_bounded_file(script, 1024, deadline=0.5)

            clock[0] = 0.0

            def slow_parse(_: str) -> dict:
                clock[0] = 0.5
                return {}

            with patch(
                "workstation_manager.discovery.time.monotonic", side_effect=lambda: clock[0]
            ), patch("workstation_manager.discovery.parse_script", side_effect=slow_parse):
                result = ScriptDiscovery(root, 1, total_timeout_seconds=0.5).scan()
            self.assertEqual(result["entries"][0]["errors"][0]["error_type"], "TimeoutError")
            self.assertEqual(result["errors"][0]["error_type"], "TimeoutError")

    def test_missing_directory_status_check_honors_total_deadline(self) -> None:
        clock = [0.0]

        def slow_missing(_: Path) -> bool:
            clock[0] = 0.5
            return False

        with patch(
            "workstation_manager.discovery.time.monotonic", side_effect=lambda: clock[0]
        ), patch.object(Path, "exists", autospec=True, side_effect=slow_missing):
            result = ScriptDiscovery(
                Path("C:/missing"), 1, total_timeout_seconds=0.5
            ).scan()
        self.assertFalse(result["directory_exists"])
        self.assertEqual(result["errors"][0]["error_type"], "TimeoutError")

    def test_file_growth_is_bounded_and_script_is_opened_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "grow.cmd"
            script.write_bytes(b"x")
            original_stat = script.stat()

            class GrowingStream(io.BytesIO):
                def fileno(self) -> int:
                    return 123

            with patch.object(
                Path,
                "open",
                autospec=True,
                return_value=GrowingStream(b"x" * 9),
            ) as file_open, patch("workstation_manager.discovery.os.fstat", return_value=original_stat):
                result = ScriptDiscovery(root, 1, max_file_bytes=8).scan()
            self.assertEqual(result["entries"][0]["errors"][0]["error_type"], "DiscoveryLimitError")
            self.assertEqual(file_open.call_count, 1)

            stable = root / "stable.cmd"
            stable.write_text("tool --port 8123", encoding="utf-8")
            script.unlink()
            with patch.object(
                Path,
                "open",
                autospec=True,
                side_effect=lambda path, *args, **kwargs: io.open(path, *args, **kwargs),
            ) as stable_open:
                stable_result = ScriptDiscovery(root, 1).scan()["entries"][0]
            self.assertEqual(stable_open.call_count, 1)
            self.assertEqual(stable_result["sha256"], hashlib.sha256(stable.read_bytes()).hexdigest())
            self.assertEqual(stable_result["ports"], [8123])

    def test_scan_resource_limits_are_structured_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oversized = root / "large.cmd"
            oversized.write_bytes(b"x" * 32)
            with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read")) as read_bytes:
                result = ScriptDiscovery(root, 1, max_file_bytes=8).scan()
            read_bytes.assert_not_called()
            self.assertEqual(result["entries"][0]["errors"][0]["error_type"], "DiscoveryLimitError")

            for index in range(4):
                (root / f"{index}.bat").write_text("echo ok", encoding="utf-8")
            limited = ScriptDiscovery(root, 1, max_entries=2).scan()
            self.assertEqual(len(limited["entries"]), 2)
            self.assertEqual(limited["errors"][0]["error_type"], "DiscoveryLimitError")

    def test_shortcut_count_and_total_timeout_limit_com_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "1.lnk").write_bytes(b"one")
            (root / "2.lnk").write_bytes(b"two")
            timeouts: list[float] = []

            def link_reader(_: Path, timeout: float) -> dict[str, str]:
                timeouts.append(timeout)
                return {"target_path": "", "arguments": "", "working_directory": ""}

            result = ScriptDiscovery(
                root,
                5,
                link_reader=link_reader,
                max_shortcuts=1,
                total_timeout_seconds=0.25,
            ).scan()
            self.assertEqual(len(timeouts), 1)
            self.assertGreater(timeouts[0], 0)
            self.assertLessEqual(timeouts[0], 0.25)
            self.assertEqual(result["entries"][1]["errors"][0]["error_type"], "DiscoveryLimitError")

            script_root = root / "timed"
            script_root.mkdir()
            (script_root / "1.cmd").write_text("echo 1", encoding="utf-8")
            (script_root / "2.cmd").write_text("echo 2", encoding="utf-8")
            moments = iter([0.0, 0.0, 0.0, 0.0, 0.0, 1.0])
            with patch(
                "workstation_manager.discovery.time.monotonic",
                side_effect=lambda: next(moments, 1.0),
            ):
                timed = ScriptDiscovery(script_root, 1, total_timeout_seconds=0.5).scan()
            self.assertEqual(len(timed["entries"]), 1)
            self.assertEqual(timed["errors"][0]["error_type"], "TimeoutError")

    def test_shortcut_timeout_uses_budget_remaining_after_file_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shortcut = root / "slow.lnk"
            shortcut.write_bytes(b"shortcut")
            clock = [0.0]
            observed: list[float] = []

            original_read = discovery_module._read_bounded_file

            def slow_read(*args, **kwargs):
                result = original_read(*args, **kwargs)
                clock[0] = 0.2
                return result

            def link_reader(_: Path, timeout: float) -> dict[str, str]:
                observed.append(timeout)
                return {"target_path": "", "arguments": "", "working_directory": ""}

            with patch(
                "workstation_manager.discovery.time.monotonic", side_effect=lambda: clock[0]
            ), patch(
                "workstation_manager.discovery._read_bounded_file", side_effect=slow_read
            ):
                ScriptDiscovery(
                    root,
                    5,
                    link_reader=link_reader,
                    total_timeout_seconds=0.5,
                ).scan()
            self.assertEqual(len(observed), 1)
            self.assertAlmostEqual(observed[0], 0.3)

    def test_shortcut_stderr_and_stdout_secrets_are_redacted_before_return_and_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secret.lnk").write_bytes(b"shortcut")
            completed = unittest.mock.Mock(
                returncode=1,
                stderr="Authorization: Bearer STDERRSECRET --auth-key AUTHSECRET",
                stdout="token=OUTSECRET",
            )
            with patch(
                "workstation_manager.discovery.shutil.which", return_value="powershell.exe"
            ), patch("workstation_manager.discovery.subprocess.run", return_value=completed):
                result = ScriptDiscovery(root, 1).scan()

            serialized = json.dumps(result, ensure_ascii=False)
            for secret in ("STDERRSECRET", "AUTHSECRET", "OUTSECRET"):
                self.assertNotIn(secret, serialized)
            self.assertIn("<redacted>", serialized)

            database = Database(root / "manager.db")
            database.replace_discovered_with_audit(
                result,
                "127.0.0.1",
                "partial",
                {"entry_count": 1, "entry_error_count": 1},
            )
            database_bytes = database.path.read_bytes()
            for secret in (b"STDERRSECRET", b"AUTHSECRET", b"OUTSECRET"):
                self.assertNotIn(secret, database_bytes)
            latest = json.dumps(database.latest_scan_run(), ensure_ascii=False)
            for secret in ("STDERRSECRET", "AUTHSECRET", "OUTSECRET"):
                self.assertNotIn(secret, latest)

    def test_shortcut_reader_uses_fixed_non_shell_command_and_timeout(self) -> None:
        completed = unittest.mock.Mock(
            returncode=0,
            stdout='{"TargetPath":"D:\\\\run.bat","Arguments":"--safe","WorkingDirectory":"D:\\\\"}',
            stderr="",
        )
        shortcut = Path("C:/safe path/name'; Write-Host PWN; #.lnk")
        with patch("workstation_manager.discovery.shutil.which", return_value="powershell.exe"), patch(
            "workstation_manager.discovery.subprocess.run", return_value=completed
        ) as process_run:
            result = read_shortcut(shortcut, 2.5)
        command = process_run.call_args.args[0]
        options = process_run.call_args.kwargs
        self.assertEqual(command[0:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertEqual(len(command), 5)
        self.assertNotIn(str(shortcut), " ".join(command))
        self.assertEqual(options["env"]["WM_LNK_PATH"], str(shortcut))
        self.assertNotIn("PATH", options["env"])
        self.assertFalse(options["shell"])
        self.assertEqual(options["timeout"], 2.5)
        self.assertEqual(result["target_path"], r"D:\run.bat")

    def test_shortcut_paths_are_redacted_before_return_and_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shortcut_file = root / "monitor.lnk"
            shortcut_file.write_bytes(b"shortcut")

            def link_reader(_: Path, __: float) -> dict[str, str]:
                return {
                    "target_path": r"\\user:UNCPASS@server\share\run.bat",
                    "arguments": "--safe",
                    "working_directory": r"D:\work\token=PATHSECRET\models",
                }

            result = ScriptDiscovery(root, 1, link_reader=link_reader).scan()
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("UNCPASS", serialized)
            self.assertNotIn("PATHSECRET", serialized)
            self.assertIn("models", serialized)
            self.assertTrue(result["entries"][0]["sensitive_values_detected"])
            database = Database(root / "manager.db")
            database.replace_discovered_with_audit(
                result, "127.0.0.1", "success", {"entry_count": 1}
            )
            data = database.path.read_bytes()
            self.assertNotIn(b"UNCPASS", data)
            self.assertNotIn(b"PATHSECRET", data)

    def test_shortcut_uses_raw_sensitive_target_path_only_for_internal_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target_directory = root / "targets"
            target_directory.mkdir()
            target = target_directory / "token=LEGITIMATE.cmd"
            target.write_text("tool --port 9123", encoding="utf-8")
            (root / "monitor.lnk").write_bytes(b"shortcut")

            def link_reader(_: Path, __: float) -> dict[str, str]:
                return {
                    "target_path": str(target),
                    "arguments": "--safe",
                    "working_directory": str(target_directory),
                }

            result = ScriptDiscovery(root, 1, link_reader=link_reader).scan()
            entry = result["entries"][0]
            self.assertEqual(entry["ports"], [9123])
            self.assertIn("target_script", entry)
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("LEGITIMATE", serialized)
            self.assertIn("token=<redacted>", serialized)
            database = Database(root / "raw-target.db")
            database.replace_discovered_with_audit(
                result, "127.0.0.1", "success", {"entry_count": 1}
            )
            self.assertNotIn(b"LEGITIMATE", database.path.read_bytes())

    def test_startup_scan_failure_does_not_block_application(self) -> None:
        class BrokenDiscovery:
            def scan(self) -> dict:
                raise RuntimeError("simulated scan failure")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(
                sample_interval_seconds=60,
                database_path=root / "manager.db",
                discovery_scripts_path=root / "scripts",
                scan_scripts_on_startup=True,
            )
            sampler = Sampler(settings, collector=fake_snapshot)
            with TestClient(
                create_app(settings, sampler, discovery=BrokenDiscovery()),
                client=("127.0.0.1", 50000),
            ) as client:
                self.assertEqual(client.get("/api/v1/health").status_code, 200)
                self.assertEqual(
                    client.app.state.startup_discovery_error["error_type"], "RuntimeError"
                )
                latest = client.app.state.database.latest_scan_run()
                self.assertEqual(latest["entry_count"], 0)
                self.assertEqual(latest["error_count"], 1)

    def test_text_scan_never_executes_script_and_extracts_clues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "start.cmd"
            script.write_text(
                "@echo off\ncd /d D:\\AIWork\\ninfer\n"
                "wsl.exe -d Ubuntu-22.04 systemctl --user status ninfer.service\n"
                "set CUDA_VISIBLE_DEVICES=GPU-12345678-abcd\n"
                "docker compose -f compose.yml up\n"
                "curl 'http://127.0.0.1:8080/health?token=TOPSECRET'\n"
                "curl 'http://127.0.0.1:8080/v1?key=QUERYSECRET'\n"
                "curl 'http://127.0.0.1:8080/v1?my_api_key=NESTEDSECRET'\n"
                "tool --auth-key \"AUTH SECRET WITH SPACES\" --port 8080\n"
                "tool --client_secret CLIENTSECRET\n"
                "set API_KEY=ANOTHERSECRET\npause\n",
                encoding="utf-8",
            )
            with patch("workstation_manager.discovery.subprocess.run") as process_run:
                result = ScriptDiscovery(root, 1).scan()
            process_run.assert_not_called()
            entry = result["entries"][0]
            self.assertEqual(entry["ports"], [8080])
            self.assertEqual(entry["wsl_distributions"], ["Ubuntu-22.04"])
            self.assertIn("ninfer.service", entry["service_names"])
            self.assertTrue(entry["docker_compose"])
            self.assertTrue(entry["interactive"])
            self.assertTrue(entry["sensitive_values_detected"])
            self.assertIn(r"D:\AIWork\ninfer", entry["working_directories"])
            serialized = json.dumps(entry, ensure_ascii=False)
            self.assertNotIn("TOPSECRET", serialized)
            self.assertNotIn("QUERYSECRET", serialized)
            self.assertNotIn("AUTH SECRET WITH SPACES", serialized)
            self.assertNotIn("NESTEDSECRET", serialized)
            self.assertNotIn("CLIENTSECRET", serialized)
            self.assertNotIn("ANOTHERSECRET", serialized)
            self.assertIn("8080", serialized)
            database = Database(root / "scan.db")
            database.replace_discovered(result["scan_id"], result["entries"])
            database_bytes = database.path.read_bytes()
            for secret in (
                b"TOPSECRET", b"QUERYSECRET", b"NESTEDSECRET", b"CLIENTSECRET",
                b"AUTH SECRET WITH SPACES", b"ANOTHERSECRET"
            ):
                self.assertNotIn(secret, database_bytes)

    def test_encoding_and_shortcut_failures_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "broken.cmd").write_bytes(b"\xff")
            (root / "monitor.lnk").write_bytes(b"shortcut")

            def broken_link(_: Path, __: float) -> dict[str, str]:
                raise RuntimeError("COM unavailable")

            result = ScriptDiscovery(root, 1, link_reader=broken_link).scan()
            self.assertEqual(len(result["entries"]), 2)
            by_name = {entry["name"]: entry for entry in result["entries"]}
            self.assertEqual(by_name["broken.cmd"]["errors"][0]["error_type"], "UnicodeError")
            self.assertEqual(by_name["monitor.lnk"]["errors"][0]["error_type"], "RuntimeError")
            database = Database(root / "errors.db")
            database.replace_discovered_with_audit(
                result,
                "127.0.0.1",
                "partial",
                {"entry_count": 2, "entry_error_count": 2},
            )
            latest = database.latest_scan_run()
            self.assertEqual(latest["error_count"], 2)

    def test_shortcut_metadata_and_database_results_persist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shortcut = root / "monitor.lnk"
            shortcut.write_bytes(b"shortcut")
            target = root / "run.bat"
            target.write_text("start http://127.0.0.1:8765", encoding="utf-8")

            def read_link(_: Path, __: float) -> dict[str, str]:
                return {
                    "target_path": str(target),
                    "arguments": "--readonly --api-key super-secret "
                    "--config '{\"refresh_token\":\"JSONSECRET\"}'",
                    "working_directory": str(root),
                }

            result = ScriptDiscovery(root, 1, link_reader=read_link).scan()
            link_entry = next(entry for entry in result["entries"] if entry["name"] == "monitor.lnk")
            self.assertEqual(link_entry["shortcut"]["target_path"], str(target))
            self.assertNotIn("super-secret", link_entry["shortcut"]["arguments"])
            self.assertNotIn("JSONSECRET", link_entry["shortcut"]["arguments"])
            self.assertTrue(link_entry["shortcut"]["sensitive_values_detected"])
            self.assertIn(8765, link_entry["ports"])
            database = Database(root / "manager.db")
            database.replace_discovered(result["scan_id"], result["entries"])
            reopened = Database(root / "manager.db")
            self.assertEqual(len(reopened.list_discovered()), 2)


if __name__ == "__main__":
    unittest.main()
