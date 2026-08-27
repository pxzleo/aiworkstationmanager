from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .discovery import redact_discovery_value


SCHEMA_VERSION = 9


class DatabaseError(RuntimeError):
    """数据库初始化或读写失败。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(
        self,
        path: Path,
        audit_retention_max_events: int = 10_000,
        audit_retention_days: int = 90,
        login_failure_max_rows: int = 10_000,
        operation_retention_max: int = 1000,
    ) -> None:
        self.path = Path(path)
        self.audit_retention_max_events = audit_retention_max_events
        self.audit_retention_days = audit_retention_days
        self.login_failure_max_rows = login_failure_max_rows
        self.operation_retention_max = operation_retention_max
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DatabaseError(f"无法创建数据目录 {self.path.parent}: {exc}") from exc
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise DatabaseError(f"无法打开数据库 {self.path}: {exc}") from exc
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    # sqlite3 不会仅因 DDL 自动开启事务；显式开启可保证迁移中途失败时
                    # 表、索引、列和版本号作为一个整体回滚。
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
                    )
                    row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
                    if row is None:
                        version = 0
                        connection.execute("INSERT INTO schema_version(version) VALUES (0)")
                    else:
                        version = int(row["version"])
                    if version > SCHEMA_VERSION:
                        raise DatabaseError(
                            f"数据库版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}"
                        )
                    migrations = {
                        1: self._migrate_to_1,
                        2: self._migrate_to_2,
                        3: self._migrate_to_3,
                        4: self._migrate_to_4,
                        5: self._migrate_to_5,
                        6: self._migrate_to_6,
                        7: self._migrate_to_7,
                        8: self._migrate_to_8,
                        9: self._migrate_to_9,
                    }
                    while version < SCHEMA_VERSION:
                        next_version = version + 1
                        migrations[next_version](connection)
                        connection.execute(
                            "UPDATE schema_version SET version = ?", (next_version,)
                        )
                        version = next_version
        except sqlite3.Error as exc:
            raise DatabaseError(f"数据库迁移失败: {exc}") from exc

    @staticmethod
    def _migrate_to_1(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS admin_user (
                id INTEGER PRIMARY KEY CHECK (id = 1), username TEXT NOT NULL UNIQUE,
                password_hash BLOB NOT NULL, password_salt BLOB NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL REFERENCES admin_user(id) ON DELETE CASCADE,
                csrf_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                source_ip TEXT NOT NULL, event TEXT NOT NULL, result TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS discovered_entries (
                path TEXT PRIMARY KEY, name TEXT NOT NULL, entry_type TEXT NOT NULL,
                mtime TEXT NOT NULL, sha256 TEXT NOT NULL, details_json TEXT NOT NULL
            )"""
        )

    @classmethod
    def _migrate_to_2(cls, connection: sqlite3.Connection) -> None:
        # 版本 2 为旧表补齐认证参数、来源 IP 及发现状态列。
        cls._migrate_to_1(connection)
        cls._ensure_column(connection, "admin_user", "username", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "admin_user", "password_hash", "BLOB NOT NULL DEFAULT X''")
        cls._ensure_column(connection, "admin_user", "password_salt", "BLOB NOT NULL DEFAULT X''")
        cls._ensure_column(connection, "admin_user", "iterations", "INTEGER NOT NULL DEFAULT 310000")
        cls._ensure_column(connection, "admin_user", "created_at", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "sessions", "admin_id", "INTEGER NOT NULL DEFAULT 1")
        cls._ensure_column(connection, "sessions", "csrf_hash", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "sessions", "created_at", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "sessions", "expires_at", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "sessions", "source_ip", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "audit_events", "created_at", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "audit_events", "source_ip", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "audit_events", "event", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "audit_events", "result", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "audit_events", "summary_json", "TEXT NOT NULL DEFAULT '{}'")
        cls._ensure_column(connection, "discovered_entries", "name", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "discovered_entries", "entry_type", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "discovered_entries", "mtime", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "discovered_entries", "sha256", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "discovered_entries", "details_json", "TEXT NOT NULL DEFAULT '{}'")
        cls._ensure_column(connection, "discovered_entries", "scan_id", "TEXT NOT NULL DEFAULT ''")
        cls._ensure_column(connection, "discovered_entries", "active", "INTEGER NOT NULL DEFAULT 1")
        cls._ensure_column(connection, "discovered_entries", "updated_at", "TEXT NOT NULL DEFAULT ''")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events(created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_discovered_entries_active ON discovered_entries(active, name)"
        )

    @staticmethod
    def _migrate_to_3(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS scan_runs (
                scan_id TEXT PRIMARY KEY, scanned_at TEXT NOT NULL, directory TEXT NOT NULL,
                directory_exists INTEGER NOT NULL, entry_count INTEGER NOT NULL,
                error_count INTEGER NOT NULL, errors_json TEXT NOT NULL,
                source_ip TEXT NOT NULL, recorded_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_scan_runs_scanned_at ON scan_runs(scanned_at DESC, recorded_at DESC)"
        )

    @staticmethod
    def _migrate_to_4(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS login_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_ip TEXT NOT NULL, created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_failures_source_time ON login_failures(source_ip, created_at)"
        )

    @staticmethod
    def _migrate_to_5(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS session_csrf_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_token_hash TEXT NOT NULL
                    REFERENCES sessions(token_hash) ON DELETE CASCADE,
                csrf_hash TEXT NOT NULL UNIQUE,
                issued_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO session_csrf_tokens(
                   session_token_hash, csrf_hash, issued_at
               )
               SELECT token_hash, csrf_hash, created_at FROM sessions
               WHERE csrf_hash <> ''"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_session_csrf_tokens_session_time
               ON session_csrf_tokens(session_token_hash, issued_at DESC, id DESC)"""
        )

    @staticmethod
    def _migrate_to_6(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_id TEXT NOT NULL,
                action TEXT NOT NULL, requested_by TEXT NOT NULL, source_ip TEXT NOT NULL,
                status TEXT NOT NULL, before_state TEXT, after_state TEXT,
                result TEXT, error_summary TEXT, created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, audit_event_id INTEGER
                    REFERENCES audit_events(id)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS operation_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL
                    REFERENCES operations(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL, phase TEXT NOT NULL, target_id TEXT NOT NULL,
                action TEXT NOT NULL, status TEXT NOT NULL, before_state TEXT,
                after_state TEXT, result TEXT, error_summary TEXT,
                started_at TEXT NOT NULL, finished_at TEXT,
                UNIQUE(operation_id, sequence)
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_operations_created ON operations(created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_operation_steps_operation ON operation_steps(operation_id, sequence)"
        )

    @staticmethod
    def _migrate_to_7(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS control_operation_lease (
                id INTEGER PRIMARY KEY CHECK (id = 1), owner_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL
            )"""
        )

    @staticmethod
    def _migrate_to_8(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS control_recovery_lock (
                id INTEGER PRIMARY KEY CHECK (id = 1), operation_id TEXT NOT NULL,
                environment_id TEXT NOT NULL, expected_state TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL
            )"""
        )

    @staticmethod
    def _migrate_to_9(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS control_recovery_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL, environment_id TEXT NOT NULL,
                expected_state TEXT NOT NULL, reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(operation_id, environment_id)
            )"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO control_recovery_items(
                   operation_id, environment_id, expected_state, reason, created_at
               ) SELECT operation_id, environment_id, expected_state, reason, created_at
                 FROM control_recovery_lock WHERE id=1"""
        )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def append_audit(
        self, source_ip: str, event: str, result: str, summary: dict[str, Any]
    ) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    self.insert_audit(connection, source_ip, event, result, summary)
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise DatabaseError(f"写入审计事件失败: {exc}") from exc

    def interrupt_incomplete_operations(self) -> None:
        """管理器进程不会恢复旧后台任务；启动时明确终结遗留状态。"""
        try:
            with self.connect() as connection:
                with connection:
                    rows = connection.execute(
                        """SELECT id, kind, target_id, action, source_ip, requested_by, before_state
                           FROM operations WHERE status IN ('queued','running')"""
                    ).fetchall()
                    now = utc_now()
                    for row in rows:
                        recovery_created = False
                        if row["kind"] == "environment":
                            step = connection.execute(
                                """SELECT before_state FROM operation_steps
                                   WHERE operation_id=? ORDER BY sequence LIMIT 1""",
                                (row["id"],),
                            ).fetchone()
                            before = step["before_state"] if step is not None else None
                            expected = before if before in {"running", "stopped"} else "stopped"
                            cursor = connection.execute(
                                """INSERT OR IGNORE INTO control_recovery_lock(
                                       id, operation_id, environment_id, expected_state, reason, created_at
                                   ) VALUES (1, ?, ?, ?, ?, ?)""",
                                (row["id"], row["target_id"], expected,
                                 "管理器重启时发现未可靠最终化的环境操作", now),
                            )
                            recovery_created = cursor.rowcount == 1
                            if recovery_created:
                                connection.execute(
                                    """INSERT OR IGNORE INTO control_recovery_items(
                                           operation_id, environment_id, expected_state, reason, created_at
                                       ) VALUES (?, ?, ?, ?, ?)""",
                                    (row["id"], row["target_id"], expected,
                                     "管理器重启时发现未可靠最终化的环境操作", now),
                                )
                        elif row["kind"] == "scene":
                            expected_by_environment: dict[str, str] = {}
                            try:
                                payload = json.loads(row["before_state"] or "{}")
                                statuses = payload.get("statuses", payload)
                                if isinstance(statuses, dict):
                                    for environment_id, state in statuses.items():
                                        if isinstance(environment_id, str):
                                            expected_by_environment[environment_id] = (
                                                state if state in {"running", "stopped"} else "stopped")
                            except (TypeError, ValueError):
                                expected_by_environment = {}
                            if not expected_by_environment:
                                steps = connection.execute(
                                    """SELECT target_id,before_state FROM operation_steps
                                       WHERE operation_id=? ORDER BY sequence""",
                                    (row["id"],),
                                ).fetchall()
                                for step in steps:
                                    expected_by_environment.setdefault(
                                        step["target_id"], step["before_state"]
                                        if step["before_state"] in {"running", "stopped"}
                                        else "stopped")
                            if expected_by_environment:
                                first_environment, first_expected = next(iter(
                                    expected_by_environment.items()))
                                cursor = connection.execute(
                                    """INSERT OR IGNORE INTO control_recovery_lock(
                                           id, operation_id, environment_id, expected_state, reason, created_at
                                       ) VALUES (1, ?, ?, ?, ?, ?)""",
                                    (row["id"], first_environment, first_expected,
                                     "管理器重启时发现未可靠最终化的场景操作", now),
                                )
                                recovery_created = cursor.rowcount == 1
                                if recovery_created:
                                    for environment_id, expected in expected_by_environment.items():
                                        connection.execute(
                                            """INSERT OR IGNORE INTO control_recovery_items(
                                                   operation_id, environment_id, expected_state, reason, created_at
                                               ) VALUES (?, ?, ?, ?, ?)""",
                                            (row["id"], environment_id, expected,
                                             "管理器重启时发现未可靠最终化的场景操作", now),
                                        )
                        connection.execute(
                            """UPDATE operation_steps SET status='interrupted',
                                   error_summary='管理器重启，步骤已中断', finished_at=?
                               WHERE operation_id=? AND status IN ('queued','running')""",
                            (now, row["id"]),
                        )
                        self.insert_audit(
                            connection, row["source_ip"], "control.recovery", "failure",
                            {"operation_id": row["id"], "kind": row["kind"],
                             "target_id": row["target_id"], "action": row["action"],
                             "requested_by": row["requested_by"],
                             "result": "recovery_required" if recovery_created else "interrupted"},
                        )
                        audit_id = connection.execute(
                            "SELECT last_insert_rowid() AS id"
                        ).fetchone()["id"]
                        connection.execute(
                            """UPDATE operations SET status='interrupted', result=?,
                                   error_summary='管理器重启，操作已中断', finished_at=?,
                                   audit_event_id=? WHERE id=?""",
                            ("recovery_required" if recovery_created else "interrupted",
                             now, audit_id, row["id"]),
                        )
        except sqlite3.Error as exc:
            raise DatabaseError(f"恢复遗留操作状态失败: {exc}") from exc

    def acquire_control_lease(self, owner_id: str) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if connection.execute(
                        "SELECT 1 FROM control_operation_lease WHERE id=1"
                    ).fetchone() is not None:
                        return False
                    connection.execute(
                        "INSERT INTO control_operation_lease(id, owner_id, acquired_at) VALUES (1, ?, ?)",
                        (owner_id, utc_now()),
                    )
                    return True
        except sqlite3.Error as exc:
            raise DatabaseError(f"取得控制操作租约失败: {exc}") from exc

    def release_control_lease(self, owner_id: str) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute(
                        "DELETE FROM control_operation_lease WHERE id=1 AND owner_id=?",
                        (owner_id,),
                    )
        except sqlite3.Error as exc:
            raise DatabaseError(f"释放控制操作租约失败: {exc}") from exc

    def clear_stale_control_lease(self) -> None:
        """仅允许持有进程级文件锁的调用方清理崩溃遗留租约。"""
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("DELETE FROM control_operation_lease WHERE id=1")
        except sqlite3.Error as exc:
            raise DatabaseError(f"清理遗留控制租约失败: {exc}") from exc

    def control_lease_owner(self) -> str | None:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT owner_id FROM control_operation_lease WHERE id=1"
                ).fetchone()
            return str(row["owner_id"]) if row is not None else None
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取控制操作租约失败: {exc}") from exc

    def control_recovery_lock(self) -> dict[str, Any] | None:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT operation_id,environment_id,expected_state,reason,created_at "
                    "FROM control_recovery_lock WHERE id=1"
                ).fetchone()
                if row is None:
                    return None
                items = connection.execute(
                    """SELECT environment_id,expected_state,reason,created_at
                       FROM control_recovery_items WHERE operation_id=? ORDER BY id""",
                    (row["operation_id"],),
                ).fetchall()
            result = dict(row)
            result["items"] = [dict(item) for item in items] or [{
                "environment_id": result["environment_id"],
                "expected_state": result["expected_state"],
                "reason": result["reason"], "created_at": result["created_at"],
            }]
            return result
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取控制恢复锁失败: {exc}") from exc

    def resolve_control_recovery(self, recovery_key: str, username: str,
                                 source_ip: str) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT operation_id,environment_id,expected_state FROM control_recovery_lock WHERE id=1"
                    ).fetchone()
                    if row is None or recovery_key not in {row["operation_id"], row["environment_id"]}:
                        return False
                    self.insert_audit(
                        connection, source_ip, "control.recovery.resolve", "success",
                        {"operation_id": row["operation_id"], "environment_id": row["environment_id"],
                         "expected_state": row["expected_state"], "requested_by": username},
                    )
                    connection.execute(
                        "DELETE FROM control_recovery_items WHERE operation_id=?",
                        (row["operation_id"],),
                    )
                    connection.execute("DELETE FROM control_recovery_lock WHERE id=1")
                    return True
        except sqlite3.Error as exc:
            raise DatabaseError(f"解除控制恢复锁失败: {exc}") from exc

    def insert_audit(
        self,
        connection: sqlite3.Connection,
        source_ip: str,
        event: str,
        result: str,
        summary: dict[str, Any],
    ) -> None:
        payload = json.dumps(
            redact_discovery_value(summary), ensure_ascii=False, sort_keys=True
        )
        now = utc_now()
        connection.execute(
            """INSERT INTO audit_events(created_at, source_ip, event, result, summary_json)
               VALUES (?, ?, ?, ?, ?)""",
            (now, source_ip, event, result, payload),
        )
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.audit_retention_days)
        ).isoformat()
        connection.execute(
            """DELETE FROM audit_events WHERE created_at < ? AND id NOT IN (
                   SELECT audit_event_id FROM operations WHERE audit_event_id IS NOT NULL
               )""",
            (cutoff,),
        )
        connection.execute(
            """DELETE FROM audit_events WHERE id IN (
                   SELECT id FROM audit_events WHERE id NOT IN (
                       SELECT audit_event_id FROM operations WHERE audit_event_id IS NOT NULL
                   ) ORDER BY id DESC LIMIT -1 OFFSET ?
               )""",
            (self.audit_retention_max_events,),
        )

    def list_audit(self, limit: int) -> list[dict[str, Any]]:
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    """SELECT id, created_at, source_ip, event, result, summary_json
                       FROM audit_events ORDER BY id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "source_ip": row["source_ip"],
                    "event": row["event"],
                    "result": row["result"],
                    "summary": json.loads(row["summary_json"]),
                }
                for row in rows
            ]
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise DatabaseError(f"读取审计事件失败: {exc}") from exc

    def is_login_rate_limited(self, source_ip: str, since: str, limit: int) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("DELETE FROM login_failures WHERE created_at < ?", (since,))
                    row = connection.execute(
                        """SELECT COUNT(*) AS count FROM login_failures
                           WHERE source_ip = ? AND created_at >= ?""",
                        (source_ip, since),
                    ).fetchone()
            return int(row["count"]) >= limit
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取登录限速状态失败: {exc}") from exc

    def record_login_failure_atomic(self, source_ip: str, since: str, limit: int) -> bool:
        """原子计数并记录登录失败；返回本次是否应限速。"""
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("DELETE FROM login_failures WHERE created_at < ?", (since,))
                    row = connection.execute(
                        """SELECT COUNT(*) AS count FROM login_failures
                           WHERE source_ip = ? AND created_at >= ?""",
                        (source_ip, since),
                    ).fetchone()
                    limited = int(row["count"]) >= limit
                    connection.execute(
                        "INSERT INTO login_failures(source_ip, created_at) VALUES (?, ?)",
                        (source_ip, utc_now()),
                    )
                    connection.execute(
                        """DELETE FROM login_failures WHERE id IN (
                               SELECT id FROM login_failures ORDER BY id DESC LIMIT -1 OFFSET ?
                           )""",
                        (self.login_failure_max_rows,),
                    )
                    self.insert_audit(
                        connection,
                        source_ip,
                        "auth.login",
                        "failure",
                        {"reason": "rate_limited" if limited else "invalid_credentials"},
                    )
            return limited
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise DatabaseError(f"原子记录登录失败失败: {exc}") from exc

    def replace_discovered(self, scan_id: str, entries: list[dict[str, Any]]) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    self._replace_discovered_rows(connection, scan_id, entries)
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            raise DatabaseError(f"保存脚本发现结果失败: {exc}") from exc

    def replace_discovered_with_audit(
        self,
        scan: dict[str, Any],
        source_ip: str,
        result: str,
        summary: dict[str, Any],
    ) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    self._replace_discovered_rows(
                        connection, str(scan["scan_id"]), list(scan["entries"])
                    )
                    self._insert_scan_run(connection, scan, source_ip)
                    self.insert_audit(
                        connection, source_ip, "discovery.scripts.scan", result, summary
                    )
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            raise DatabaseError(f"保存扫描结果及审计失败: {exc}") from exc

    def record_failed_scan_with_audit(
        self, scan: dict[str, Any], source_ip: str, error_type: str
    ) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    self._insert_scan_run(connection, scan, source_ip)
                    self.insert_audit(
                        connection,
                        source_ip,
                        "discovery.scripts.scan",
                        "failure",
                        {"entry_count": 0, "entry_error_count": 1, "error_type": error_type},
                    )
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            raise DatabaseError(f"保存失败扫描元数据及审计失败: {exc}") from exc

    @staticmethod
    def _insert_scan_run(
        connection: sqlite3.Connection, scan: dict[str, Any], source_ip: str
    ) -> None:
        safe_scan = redact_discovery_value(scan)
        entry_errors = [
            {"path": entry.get("path"), **error}
            for entry in safe_scan.get("entries", [])
            for error in entry.get("errors", [])
        ]
        errors = [*safe_scan.get("errors", []), *entry_errors]
        connection.execute(
            """INSERT INTO scan_runs(
                   scan_id, scanned_at, directory, directory_exists, entry_count,
                   error_count, errors_json, source_ip, recorded_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                safe_scan["scan_id"], safe_scan["scanned_at"], safe_scan["directory"],
                1 if safe_scan["directory_exists"] else 0, len(safe_scan.get("entries", [])),
                len(errors), json.dumps(errors, ensure_ascii=False, sort_keys=True),
                source_ip, utc_now(),
            ),
        )

    def latest_scan_run(self) -> dict[str, Any] | None:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    """SELECT scan_id, scanned_at, directory, directory_exists,
                              entry_count, error_count, errors_json, source_ip
                       FROM scan_runs ORDER BY recorded_at DESC, rowid DESC LIMIT 1"""
                ).fetchone()
            if row is None:
                return None
            return {
                "scan_id": row["scan_id"],
                "scanned_at": row["scanned_at"],
                "directory": row["directory"],
                "directory_exists": bool(row["directory_exists"]),
                "entry_count": row["entry_count"],
                "error_count": row["error_count"],
                "errors": json.loads(row["errors_json"]),
                "source_ip": row["source_ip"],
            }
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise DatabaseError(f"读取最近扫描元数据失败: {exc}") from exc

    @staticmethod
    def _replace_discovered_rows(
        connection: sqlite3.Connection, scan_id: str, entries: list[dict[str, Any]]
    ) -> None:
        now = utc_now()
        connection.execute("UPDATE discovered_entries SET active = 0")
        for entry in entries:
            safe_entry = redact_discovery_value(entry)
            connection.execute(
                """INSERT INTO discovered_entries(
                       path, name, entry_type, mtime, sha256, details_json,
                       scan_id, active, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       name=excluded.name, entry_type=excluded.entry_type,
                       mtime=excluded.mtime, sha256=excluded.sha256,
                       details_json=excluded.details_json, scan_id=excluded.scan_id,
                       active=1, updated_at=excluded.updated_at""",
                (
                    safe_entry["path"], safe_entry["name"], safe_entry["type"], safe_entry["mtime"],
                    safe_entry["sha256"],
                    json.dumps(
                        safe_entry,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    scan_id, now,
                ),
            )

    def list_discovered(self) -> list[dict[str, Any]]:
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    """SELECT details_json FROM discovered_entries
                       WHERE active = 1 ORDER BY name COLLATE NOCASE"""
                ).fetchall()
            return [json.loads(row["details_json"]) for row in rows]
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise DatabaseError(f"读取脚本发现结果失败: {exc}") from exc

    def create_operation(
        self, operation_id: str, kind: str, target_id: str, action: str,
        requested_by: str, source_ip: str,
    ) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute(
                        """INSERT INTO operations(
                               id, kind, target_id, action, requested_by, source_ip,
                               status, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)""",
                        (operation_id, kind, target_id, action, requested_by, source_ip, utc_now()),
                    )
                    connection.execute(
                        """DELETE FROM operations WHERE id IN (
                               SELECT id FROM operations WHERE status NOT IN ('queued','running')
                               ORDER BY created_at DESC LIMIT -1 OFFSET ?
                           )""",
                        (self.operation_retention_max,),
                    )
        except sqlite3.Error as exc:
            raise DatabaseError(f"创建操作任务失败: {exc}") from exc

    def update_operation(self, operation_id: str, **fields: Any) -> None:
        allowed = {
            "status", "before_state", "after_state", "result", "error_summary",
            "started_at", "finished_at", "audit_event_id",
        }
        if not fields or set(fields) - allowed:
            raise DatabaseError("操作更新字段不受支持")
        safe = redact_discovery_value(fields)
        assignments = ", ".join(f"{name}=?" for name in safe)
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        f"UPDATE operations SET {assignments} WHERE id=?",
                        (*safe.values(), operation_id),
                    )
                    if cursor.rowcount != 1:
                        raise DatabaseError("操作任务不存在")
        except sqlite3.Error as exc:
            raise DatabaseError(f"更新操作任务失败: {exc}") from exc

    def create_operation_step(
        self, operation_id: str, sequence: int, phase: str, target_id: str,
        action: str, status: str = "running", before_state: str | None = None,
    ) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute(
                        """INSERT INTO operation_steps(
                               operation_id, sequence, phase, target_id, action, status,
                               before_state, started_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (operation_id, sequence, phase, target_id, action, status,
                         before_state, utc_now()),
                    )
        except sqlite3.Error as exc:
            raise DatabaseError(f"创建操作步骤失败: {exc}") from exc

    def finish_operation_step(
        self, operation_id: str, sequence: int, status: str, after_state: str | None,
        result: str | None = None, error_summary: str | None = None,
    ) -> None:
        safe_error = redact_discovery_value(error_summary)
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute(
                        """UPDATE operation_steps SET status=?, after_state=?, result=?,
                               error_summary=?, finished_at=?
                           WHERE operation_id=? AND sequence=?""",
                        (status, after_state, result, safe_error, utc_now(), operation_id, sequence),
                    )
        except sqlite3.Error as exc:
            raise DatabaseError(f"完成操作步骤失败: {exc}") from exc

    def finish_operation_with_audit(
        self, operation_id: str, status: str, result: str,
        before_state: str | None, after_state: str | None,
        error_summary: str | None = None,
        recovery_lock: dict[str, str] | None = None,
        recovery_items: list[dict[str, str]] | None = None,
    ) -> None:
        safe_error = redact_discovery_value(error_summary)
        try:
            with self.connect() as connection:
                with connection:
                    row = connection.execute(
                        "SELECT kind,target_id,action,source_ip,requested_by FROM operations WHERE id=?",
                        (operation_id,),
                    ).fetchone()
                    if row is None:
                        raise DatabaseError("操作任务不存在")
                    self.insert_audit(
                        connection, row["source_ip"], f"control.{row['kind']}",
                        "success" if status == "succeeded" else "failure",
                        {"operation_id": operation_id, "target_id": row["target_id"],
                         "action": row["action"], "requested_by": row["requested_by"],
                         "result": result, "error_summary": safe_error},
                    )
                    audit_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                    connection.execute(
                        """UPDATE operations SET status=?, result=?, before_state=?,
                               after_state=?, error_summary=?, finished_at=?, audit_event_id=?
                           WHERE id=?""",
                        (status, result, before_state, after_state, safe_error, utc_now(),
                         audit_id, operation_id),
                    )
                    items = recovery_items or ([recovery_lock] if recovery_lock is not None else [])
                    if items:
                        safe_items = redact_discovery_value(items)
                        safe_lock = safe_items[0]
                        connection.execute(
                            """INSERT OR REPLACE INTO control_recovery_lock(
                                   id, operation_id, environment_id, expected_state, reason, created_at
                               ) VALUES (1, ?, ?, ?, ?, ?)""",
                            (operation_id, safe_lock["environment_id"],
                             safe_lock["expected_state"], safe_lock["reason"], utc_now()),
                        )
                        connection.execute(
                            "DELETE FROM control_recovery_items WHERE operation_id=?",
                            (operation_id,),
                        )
                        for item in safe_items:
                            connection.execute(
                                """INSERT INTO control_recovery_items(
                                       operation_id, environment_id, expected_state, reason, created_at
                                   ) VALUES (?, ?, ?, ?, ?)""",
                                (operation_id, item["environment_id"], item["expected_state"],
                                 item["reason"], utc_now()),
                            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"完成操作任务及审计失败: {exc}") from exc

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        try:
            with self.connect() as connection:
                row = connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
                if row is None:
                    return None
                steps = connection.execute(
                    "SELECT * FROM operation_steps WHERE operation_id=? ORDER BY sequence",
                    (operation_id,),
                ).fetchall()
            item = dict(row)
            item["steps"] = [dict(step) for step in steps]
            return item
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取操作任务失败: {exc}") from exc

    def list_operations(self, limit: int) -> list[dict[str, Any]]:
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM operations ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
                result = []
                for row in rows:
                    item = dict(row)
                    steps = connection.execute(
                        "SELECT * FROM operation_steps WHERE operation_id=? ORDER BY sequence",
                        (row["id"],),
                    ).fetchall()
                    item["steps"] = [dict(step) for step in steps]
                    result.append(item)
            return result
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取操作任务列表失败: {exc}") from exc
