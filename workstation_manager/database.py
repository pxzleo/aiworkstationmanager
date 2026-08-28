from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .redaction import redact_value


SCHEMA_VERSION = 14


class DatabaseError(RuntimeError):
    """数据库初始化或读写失败。"""


class OperationBusyError(DatabaseError):
    """数据库中已经存在尚未结束的服务或场景操作。"""


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
        resource_history_retention_minutes: int = 1440,
    ) -> None:
        self.path = Path(path)
        self.audit_retention_max_events = audit_retention_max_events
        self.audit_retention_days = audit_retention_days
        self.login_failure_max_rows = login_failure_max_rows
        self.operation_retention_max = operation_retention_max
        self.resource_history_retention_minutes = resource_history_retention_minutes
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
            connection.execute("PRAGMA busy_timeout = 10000")
        except sqlite3.Error as exc:
            raise DatabaseError(f"无法打开数据库 {self.path}: {exc}") from exc
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        try:
            with self.connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
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
                        3: self._no_op_migration,
                        4: self._migrate_to_4,
                        5: self._migrate_to_5,
                        6: self._migrate_to_6,
                        7: self._no_op_migration,
                        8: self._no_op_migration,
                        9: self._no_op_migration,
                        10: self._migrate_to_10,
                        11: self._migrate_to_11,
                        12: self._migrate_to_12,
                        13: self._migrate_to_13,
                        14: self._migrate_to_14,
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
        connection.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events(created_at DESC)"
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
    def _migrate_to_10(connection: sqlite3.Connection) -> None:
        connection.execute("DROP TABLE IF EXISTS control_recovery_items")
        connection.execute("DROP TABLE IF EXISTS control_recovery_lock")
        connection.execute("DROP TABLE IF EXISTS control_operation_lease")
        connection.execute("DROP TABLE IF EXISTS scan_runs")
        connection.execute("DROP TABLE IF EXISTS discovered_entries")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS registered_services (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                script_path TEXT NOT NULL,
                gpu_label TEXT NOT NULL DEFAULT '',
                port INTEGER,
                ui_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS scenes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS scene_services (
                scene_id TEXT NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
                service_id TEXT NOT NULL REFERENCES registered_services(id) ON DELETE CASCADE,
                start_order INTEGER NOT NULL,
                PRIMARY KEY(scene_id, service_id),
                UNIQUE(scene_id, start_order)
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_registered_services_name ON registered_services(name)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_scene_services_order ON scene_services(scene_id, start_order)"
        )

    @classmethod
    def _migrate_to_11(cls, connection: sqlite3.Connection) -> None:
        cls._ensure_column(connection, "scenes", "display_order", "INTEGER NOT NULL DEFAULT 0")
        rows = connection.execute(
            "SELECT id FROM scenes ORDER BY name COLLATE NOCASE, id"
        ).fetchall()
        for display_order, row in enumerate(rows):
            connection.execute(
                "UPDATE scenes SET display_order=? WHERE id=?", (display_order, row["id"])
            )

    @classmethod
    def _migrate_to_12(cls, connection: sqlite3.Connection) -> None:
        cls._ensure_column(
            connection, "registered_services", "recorded_state",
            "TEXT NOT NULL DEFAULT 'unknown'",
        )
        cls._ensure_column(connection, "registered_services", "state_updated_at", "TEXT")
        cls._ensure_column(connection, "registered_services", "state_error", "TEXT")

    @classmethod
    def _migrate_to_13(cls, connection: sqlite3.Connection) -> None:
        cls._migrate_to_2(connection)
        cls._migrate_to_5(connection)
        connection.execute("DELETE FROM sessions")
        connection.execute(
            """CREATE TABLE admin_user_v13 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash BLOB NOT NULL,
                password_salt BLOB NOT NULL,
                iterations INTEGER NOT NULL DEFAULT 310000,
                created_at TEXT NOT NULL DEFAULT ''
            )"""
        )
        connection.execute(
            """INSERT INTO admin_user_v13(
                   id,username,password_hash,password_salt,iterations,created_at
               )
               SELECT id,username,password_hash,password_salt,iterations,created_at
               FROM admin_user"""
        )
        connection.execute("DROP TABLE admin_user")
        connection.execute("ALTER TABLE admin_user_v13 RENAME TO admin_user")

    @staticmethod
    def _migrate_to_14(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS resource_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sampled_at TEXT NOT NULL UNIQUE,
                cpu_load_percent REAL,
                cpu_temperature_c REAL,
                memory_percent REAL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS resource_gpu_samples (
                sample_id INTEGER NOT NULL
                    REFERENCES resource_samples(id) ON DELETE CASCADE,
                gpu_key TEXT NOT NULL,
                uuid TEXT,
                gpu_index INTEGER,
                name TEXT,
                load_percent REAL,
                memory_used_mib REAL,
                memory_total_mib REAL,
                memory_percent REAL,
                temperature_c REAL,
                PRIMARY KEY(sample_id, gpu_key)
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_resource_samples_time ON resource_samples(sampled_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_resource_gpu_sample ON resource_gpu_samples(sample_id)"
        )

    @staticmethod
    def _no_op_migration(_: sqlite3.Connection) -> None:
        """保留历史版本号，使旧数据库可以按顺序升级到当前结构。"""

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def append_resource_sample(self, sample: dict[str, Any]) -> None:
        try:
            parsed_at = datetime.fromisoformat(str(sample["sampled_at"]))
            if parsed_at.tzinfo is None:
                raise ValueError("sampled_at 必须包含时区")
            sampled_at = parsed_at.astimezone(timezone.utc).isoformat()
            with self.connect() as connection:
                with connection:
                    connection.execute(
                        """INSERT INTO resource_samples(
                               sampled_at,cpu_load_percent,cpu_temperature_c,memory_percent
                           ) VALUES (?,?,?,?)
                           ON CONFLICT(sampled_at) DO UPDATE SET
                               cpu_load_percent=excluded.cpu_load_percent,
                               cpu_temperature_c=excluded.cpu_temperature_c,
                               memory_percent=excluded.memory_percent""",
                        (sampled_at, sample.get("cpu_load_percent"),
                         sample.get("cpu_temperature_c"), sample.get("memory_percent")),
                    )
                    row = connection.execute(
                        "SELECT id FROM resource_samples WHERE sampled_at=?", (sampled_at,)
                    ).fetchone()
                    if row is None:
                        raise DatabaseError("资源采样写入后无法读取")
                    sample_id = int(row["id"])
                    connection.execute(
                        "DELETE FROM resource_gpu_samples WHERE sample_id=?", (sample_id,)
                    )
                    occurrences: dict[str, int] = {}
                    for position, gpu in enumerate(sample.get("gpus", [])):
                        uuid = str(gpu.get("uuid") or "").strip()
                        gpu_index = gpu.get("index")
                        base_key = f"uuid:{uuid}" if uuid else \
                            f"index:{gpu_index}" if isinstance(gpu_index, int) else \
                            f"position:{position}"
                        occurrence = occurrences.get(base_key, 0)
                        occurrences[base_key] = occurrence + 1
                        gpu_key = f"{base_key}#{occurrence}"
                        connection.execute(
                            """INSERT INTO resource_gpu_samples(
                                   sample_id,gpu_key,uuid,gpu_index,name,load_percent,
                                   memory_used_mib,memory_total_mib,memory_percent,temperature_c
                               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (sample_id, gpu_key, uuid or None, gpu_index, gpu.get("name"),
                             gpu.get("load_percent"), gpu.get("memory_used_mib"),
                             gpu.get("memory_total_mib"), gpu.get("memory_percent"),
                             gpu.get("temperature_c")),
                        )
                    cutoff = (
                        datetime.now(timezone.utc)
                        - timedelta(minutes=self.resource_history_retention_minutes)
                    ).isoformat()
                    connection.execute(
                        "DELETE FROM resource_samples WHERE sampled_at < ?", (cutoff,)
                    )
        except DatabaseError:
            raise
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            raise DatabaseError(f"写入资源历史失败: {exc}") from exc

    def query_resource_history(
        self, window_minutes: int, bucket_seconds: int = 0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise DatabaseError("资源历史查询时间必须包含时区")
        cutoff = (
            current_time.astimezone(timezone.utc) - timedelta(minutes=window_minutes)
        ).isoformat()
        try:
            with self.connect() as connection:
                summary = connection.execute(
                    """SELECT COUNT(*) AS sample_count, MIN(sampled_at) AS stored_since,
                              MAX(sampled_at) AS stored_until FROM resource_samples"""
                ).fetchone()
                if bucket_seconds > 0:
                    host_rows = connection.execute(
                        """SELECT CAST(strftime('%s',sampled_at) AS INTEGER)/? AS bucket,
                                  MAX(sampled_at) AS sampled_at,
                                  AVG(cpu_load_percent) AS cpu_load_percent,
                                  AVG(cpu_temperature_c) AS cpu_temperature_c,
                                  AVG(memory_percent) AS memory_percent
                           FROM resource_samples WHERE sampled_at>=?
                           GROUP BY bucket ORDER BY bucket""",
                        (bucket_seconds, cutoff),
                    ).fetchall()
                    gpu_rows = connection.execute(
                        """SELECT CAST(strftime('%s',s.sampled_at) AS INTEGER)/? AS bucket,
                                  g.gpu_key,MAX(g.uuid) AS uuid,MAX(g.gpu_index) AS gpu_index,
                                  MAX(g.name) AS name,AVG(g.load_percent) AS load_percent,
                                  AVG(g.memory_used_mib) AS memory_used_mib,
                                  AVG(g.memory_total_mib) AS memory_total_mib,
                                  AVG(g.memory_percent) AS memory_percent,
                                  AVG(g.temperature_c) AS temperature_c
                           FROM resource_gpu_samples g
                           JOIN resource_samples s ON s.id=g.sample_id
                           WHERE s.sampled_at>=? GROUP BY bucket,g.gpu_key
                           ORDER BY bucket,g.gpu_index,g.gpu_key""",
                        (bucket_seconds, cutoff),
                    ).fetchall()
                    samples = self._assemble_resource_history(host_rows, gpu_rows, "bucket")
                else:
                    host_rows = connection.execute(
                        """SELECT id,sampled_at,cpu_load_percent,cpu_temperature_c,memory_percent
                           FROM resource_samples WHERE sampled_at>=? ORDER BY sampled_at""",
                        (cutoff,),
                    ).fetchall()
                    gpu_rows = connection.execute(
                        """SELECT g.*,s.sampled_at FROM resource_gpu_samples g
                           JOIN resource_samples s ON s.id=g.sample_id
                           WHERE s.sampled_at>=? ORDER BY s.sampled_at,g.gpu_index,g.gpu_key""",
                        (cutoff,),
                    ).fetchall()
                    samples = self._assemble_resource_history(host_rows, gpu_rows, "sample_id")
            return {
                "samples": samples,
                "bucket_seconds": bucket_seconds,
                "retention_minutes": self.resource_history_retention_minutes,
                "stored_sample_count": int(summary["sample_count"]),
                "stored_since": summary["stored_since"],
                "stored_until": summary["stored_until"],
            }
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise DatabaseError(f"读取资源历史失败: {exc}") from exc

    @staticmethod
    def _assemble_resource_history(
        host_rows: list[sqlite3.Row], gpu_rows: list[sqlite3.Row], key_name: str,
    ) -> list[dict[str, Any]]:
        samples: dict[int, dict[str, Any]] = {}
        for row in host_rows:
            key = int(row[key_name] if key_name == "bucket" else row["id"])
            samples[key] = {
                "sampled_at": row["sampled_at"],
                "cpu_load_percent": row["cpu_load_percent"],
                "cpu_temperature_c": row["cpu_temperature_c"],
                "memory_percent": row["memory_percent"],
                "gpus": [],
            }
        for row in gpu_rows:
            key = int(row[key_name])
            if key not in samples:
                continue
            samples[key]["gpus"].append({
                "uuid": row["uuid"], "index": row["gpu_index"], "name": row["name"],
                "load_percent": row["load_percent"],
                "memory_used_mib": row["memory_used_mib"],
                "memory_total_mib": row["memory_total_mib"],
                "memory_percent": row["memory_percent"],
                "temperature_c": row["temperature_c"],
            })
        return list(samples.values())

    def append_audit(
        self, source_ip: str, event: str, result: str, summary: dict[str, Any]
    ) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    self.insert_audit(connection, source_ip, event, result, summary)
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise DatabaseError(f"写入审计事件失败: {exc}") from exc

    def insert_audit(
        self,
        connection: sqlite3.Connection,
        source_ip: str,
        event: str,
        result: str,
        summary: dict[str, Any],
    ) -> None:
        payload = json.dumps(redact_value(summary), ensure_ascii=False, sort_keys=True)
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

    def create_operation(
        self, operation_id: str, kind: str, target_id: str, action: str,
        requested_by: str, source_ip: str,
    ) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if connection.execute(
                        "SELECT 1 FROM operations WHERE status IN ('queued','running') LIMIT 1"
                    ).fetchone() is not None:
                        raise OperationBusyError("已有服务或场景操作正在执行")
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

    def has_active_operation(self) -> bool:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM operations WHERE status IN ('queued','running') LIMIT 1"
                ).fetchone()
            return row is not None
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取活动操作状态失败: {exc}") from exc

    def update_operation(self, operation_id: str, **fields: Any) -> None:
        allowed = {
            "status", "before_state", "after_state", "result", "error_summary",
            "started_at", "finished_at", "audit_event_id",
        }
        if not fields or set(fields) - allowed:
            raise DatabaseError("操作更新字段不受支持")
        safe = redact_value(fields)
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

    def request_scene_operation_cancel(
        self, operation_id: str, username: str, source_ip: str,
    ) -> str:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT kind,status,result FROM operations WHERE id=?", (operation_id,)
                    ).fetchone()
                    if row is None:
                        return "missing"
                    if row["kind"] != "scene":
                        return "not_scene"
                    if row["status"] not in {"queued", "running"}:
                        return "finished"
                    if row["result"] == "cancel_requested":
                        return "already_requested"
                    connection.execute(
                        "UPDATE operations SET result='cancel_requested' WHERE id=?", (operation_id,)
                    )
                    self.insert_audit(
                        connection, source_ip, "management.scene.cancel", "success",
                        {"operation_id": operation_id, "requested_by": username},
                    )
                    return "accepted"
        except sqlite3.Error as exc:
            raise DatabaseError(f"记录场景终止请求失败: {exc}") from exc

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
        safe_error = redact_value(error_summary)
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
    ) -> None:
        safe_error = redact_value(error_summary)
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
                        connection, row["source_ip"], f"management.{row['kind']}",
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

    def list_registered_services(self) -> list[dict[str, Any]]:
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM registered_services ORDER BY name COLLATE NOCASE, id"
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取已登记服务失败: {exc}") from exc

    def get_registered_service(self, service_id: str) -> dict[str, Any] | None:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM registered_services WHERE id=?", (service_id,)
                ).fetchone()
            return dict(row) if row is not None else None
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取已登记服务失败: {exc}") from exc

    def create_registered_service(self, item: dict[str, Any]) -> None:
        now = utc_now()
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute(
                        """INSERT INTO registered_services(
                               id,name,description,script_path,gpu_label,port,ui_url,
                               created_at,updated_at
                           ) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (item["id"], item["name"], item["description"], item["script_path"],
                         item["gpu_label"], item["port"], item["ui_url"], now, now),
                    )
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("服务名称或 ID 已存在") from exc
        except (sqlite3.Error, KeyError) as exc:
            raise DatabaseError(f"创建已登记服务失败: {exc}") from exc

    def update_registered_service(self, service_id: str, item: dict[str, Any]) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        """UPDATE registered_services SET name=?,description=?,script_path=?,
                               gpu_label=?,port=?,ui_url=?,
                               recorded_state=CASE WHEN script_path<>? THEN 'unknown' ELSE recorded_state END,
                               state_updated_at=CASE WHEN script_path<>? THEN NULL ELSE state_updated_at END,
                               state_error=CASE WHEN script_path<>? THEN NULL ELSE state_error END,
                               updated_at=? WHERE id=?""",
                        (item["name"], item["description"], item["script_path"],
                         item["gpu_label"], item["port"], item["ui_url"],
                         item["script_path"], item["script_path"], item["script_path"],
                         utc_now(), service_id),
                    )
                    return cursor.rowcount == 1
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("服务名称已存在") from exc
        except (sqlite3.Error, KeyError) as exc:
            raise DatabaseError(f"更新已登记服务失败: {exc}") from exc

    def update_registered_service_status(
        self, service_id: str, state: str, error: str | None
    ) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        """UPDATE registered_services
                           SET recorded_state=?,state_updated_at=?,state_error=? WHERE id=?""",
                        (state, utc_now(), error, service_id),
                    )
                    return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise DatabaseError(f"保存服务状态失败: {exc}") from exc

    def delete_registered_service(self, service_id: str) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        "DELETE FROM registered_services WHERE id=?", (service_id,)
                    )
                    return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise DatabaseError(f"删除已登记服务失败: {exc}") from exc

    def list_scenes(self) -> list[dict[str, Any]]:
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM scenes ORDER BY display_order, id"
                ).fetchall()
                result: list[dict[str, Any]] = []
                for row in rows:
                    item = dict(row)
                    services = connection.execute(
                        """SELECT ss.service_id,rs.name FROM scene_services ss
                           JOIN registered_services rs ON rs.id=ss.service_id
                           WHERE ss.scene_id=? ORDER BY ss.start_order""",
                        (row["id"],),
                    ).fetchall()
                    item["service_ids"] = [service["service_id"] for service in services]
                    item["service_names"] = [service["name"] for service in services]
                    result.append(item)
            return result
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取场景失败: {exc}") from exc

    def get_scene(self, scene_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list_scenes() if item["id"] == scene_id), None)

    def create_scene(self, item: dict[str, Any]) -> None:
        now = utc_now()
        try:
            with self.connect() as connection:
                with connection:
                    row = connection.execute(
                        "SELECT COALESCE(MAX(display_order), -1) + 1 AS next_order FROM scenes"
                    ).fetchone()
                    connection.execute(
                        """INSERT INTO scenes(
                               id,name,description,display_order,created_at,updated_at
                           ) VALUES (?,?,?,?,?,?)""",
                        (item["id"], item["name"], item["description"],
                         row["next_order"], now, now),
                    )
                    self._replace_scene_services(connection, item["id"], item["service_ids"])
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("场景名称、ID 或服务列表无效") from exc
        except (sqlite3.Error, KeyError) as exc:
            raise DatabaseError(f"创建场景失败: {exc}") from exc

    def update_scene(self, scene_id: str, item: dict[str, Any]) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        "UPDATE scenes SET name=?,description=?,updated_at=? WHERE id=?",
                        (item["name"], item["description"], utc_now(), scene_id),
                    )
                    if cursor.rowcount != 1:
                        return False
                    self._replace_scene_services(connection, scene_id, item["service_ids"])
                    return True
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("场景名称或服务列表无效") from exc
        except (sqlite3.Error, KeyError) as exc:
            raise DatabaseError(f"更新场景失败: {exc}") from exc

    def delete_scene(self, scene_id: str) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute("DELETE FROM scenes WHERE id=?", (scene_id,))
                    return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise DatabaseError(f"删除场景失败: {exc}") from exc

    def reorder_scenes(self, scene_ids: list[str], username: str, source_ip: str) -> None:
        if len(scene_ids) != len(set(scene_ids)):
            raise DatabaseError("场景排序包含重复 ID")
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    known = {
                        row["id"] for row in connection.execute("SELECT id FROM scenes").fetchall()
                    }
                    if set(scene_ids) != known:
                        raise DatabaseError("场景排序必须包含全部现有场景")
                    now = utc_now()
                    for display_order, scene_id in enumerate(scene_ids):
                        connection.execute(
                            "UPDATE scenes SET display_order=?,updated_at=? WHERE id=?",
                            (display_order, now, scene_id),
                        )
                    self.insert_audit(
                        connection, source_ip, "management.scene.reorder", "success",
                        {"scene_ids": scene_ids, "requested_by": username},
                    )
        except sqlite3.Error as exc:
            raise DatabaseError(f"保存场景排序失败: {exc}") from exc

    @staticmethod
    def _replace_scene_services(
        connection: sqlite3.Connection, scene_id: str, service_ids: list[str]
    ) -> None:
        connection.execute("DELETE FROM scene_services WHERE scene_id=?", (scene_id,))
        for index, service_id in enumerate(service_ids):
            connection.execute(
                "INSERT INTO scene_services(scene_id,service_id,start_order) VALUES (?,?,?)",
                (scene_id, service_id, index),
            )

    def interrupt_simple_operations(self) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    rows = connection.execute(
                        "SELECT id,kind,target_id,action,source_ip,requested_by FROM operations "
                        "WHERE status IN ('queued','running')"
                    ).fetchall()
                    now = utc_now()
                    for row in rows:
                        connection.execute(
                            """UPDATE operation_steps SET status='interrupted',
                                   result='interrupted',error_summary='管理器重启，操作已中断',
                                   finished_at=? WHERE operation_id=?
                                   AND status IN ('queued','running')""",
                            (now, row["id"]),
                        )
                        self.insert_audit(
                            connection, row["source_ip"], f"management.{row['kind']}",
                            "failure", {"operation_id": row["id"], "target_id": row["target_id"],
                                        "action": row["action"], "requested_by": row["requested_by"],
                                        "result": "interrupted"},
                        )
                        audit_id = connection.execute(
                            "SELECT last_insert_rowid() AS id"
                        ).fetchone()["id"]
                        connection.execute(
                            """UPDATE operations SET status='interrupted',result='interrupted',
                                   error_summary='管理器重启，操作已中断',finished_at=?,audit_event_id=?
                               WHERE id=?""",
                            (now, audit_id, row["id"]),
                        )
        except sqlite3.Error as exc:
            raise DatabaseError(f"恢复遗留服务操作失败: {exc}") from exc
