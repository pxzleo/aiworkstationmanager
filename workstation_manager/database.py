from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .redaction import redact_value


SCHEMA_VERSION = 31


class DatabaseError(RuntimeError):
    """数据库初始化或读写失败。"""


class OperationBusyError(DatabaseError):
    """数据库中已经存在尚未结束的服务或场景操作。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    AUTOMATIC_TASK_LEASE_SECONDS = 30 * 60
    VIDEO_JOB_LIST_COLUMNS = ",".join((
        "id", "idempotency_key", "payload_hash", "session_id", "workflow_path",
        "video_spec", "requested_output_path", "callback_url", "callback_directory",
        "status", "phase", "prompt_id", "output_path", "shared_output_path", "result", "progress",
        "error_code", "error_summary", "cancel_requested", "callback_attempts",
        "created_at", "updated_at", "started_at", "finished_at",
        "generation_scene_id", "generation_scene_name", "original_scene_id",
        "original_scene_name", "batch_id", "batch_index", "batch_size",
    ))

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
                        15: self._migrate_to_15,
                        16: self._migrate_to_16,
                        17: self._migrate_to_17,
                        18: self._migrate_to_18,
                        19: self._migrate_to_19,
                        20: self._migrate_to_20,
                        21: self._migrate_to_21,
                        22: self._migrate_to_22,
                        23: self._migrate_to_23,
                        24: self._migrate_to_24,
                        25: self._migrate_to_25,
                        26: self._migrate_to_26,
                        27: self._migrate_to_27,
                        28: self._migrate_to_28,
                        29: self._migrate_to_29,
                        30: self._migrate_to_30,
                        31: self._migrate_to_31,
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

    @classmethod
    def _migrate_to_15(cls, connection: sqlite3.Connection) -> None:
        cls._ensure_column(connection, "resource_gpu_samples", "power_w", "REAL")
        cls._ensure_column(connection, "resource_gpu_samples", "graphics_clock_mhz", "REAL")

    @classmethod
    def _migrate_to_16(cls, connection: sqlite3.Connection) -> None:
        cls._migrate_to_14(connection)
        cls._ensure_column(connection, "resource_samples", "memory_used_bytes", "REAL")
        cls._ensure_column(connection, "resource_samples", "memory_total_bytes", "REAL")

    @classmethod
    def _migrate_to_17(cls, connection: sqlite3.Connection) -> None:
        host_columns = {
            "cpu_frequency_mhz": "REAL",
            "memory_available_bytes": "REAL",
            "commit_used_bytes": "REAL",
            "commit_limit_bytes": "REAL",
            "swap_used_bytes": "REAL",
            "swap_total_bytes": "REAL",
            "network_received_bytes_per_second": "REAL",
            "network_sent_bytes_per_second": "REAL",
            "wsl_memory_used_bytes": "REAL",
            "wsl_swap_used_bytes": "REAL",
        }
        for column, declaration in host_columns.items():
            cls._ensure_column(connection, "resource_samples", column, declaration)
        for column in ("memory_utilization_percent", "encoder_percent", "decoder_percent"):
            cls._ensure_column(connection, "resource_gpu_samples", column, "REAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS resource_disk_samples (
                sample_id INTEGER NOT NULL
                    REFERENCES resource_samples(id) ON DELETE CASCADE,
                disk_key TEXT NOT NULL,
                name TEXT,
                read_bytes_per_second REAL,
                write_bytes_per_second REAL,
                latency_ms REAL,
                PRIMARY KEY(sample_id, disk_key)
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_resource_disk_sample "
            "ON resource_disk_samples(sample_id)"
        )

    @classmethod
    def _migrate_to_18(cls, connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='registered_services'"
        ).fetchone()
        if table is None:
            return
        cls._ensure_column(
            connection, "registered_services", "health_url", "TEXT NOT NULL DEFAULT ''"
        )
        cls._ensure_column(
            connection, "registered_services", "health_expect", "TEXT NOT NULL DEFAULT ''"
        )
        cls._ensure_column(
            connection, "registered_services", "desired_state",
            "TEXT NOT NULL DEFAULT 'unknown'",
        )
        cls._ensure_column(
            connection, "registered_services", "observed_state",
            "TEXT NOT NULL DEFAULT 'unknown'",
        )
        cls._ensure_column(connection, "registered_services", "observed_at", "TEXT")
        cls._ensure_column(connection, "registered_services", "observed_error", "TEXT")
        connection.execute(
            """UPDATE registered_services
               SET desired_state=recorded_state,
                   observed_state=recorded_state,
                   observed_at=state_updated_at,
                   observed_error=state_error"""
        )

    @classmethod
    def _migrate_to_19(cls, connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scenes'"
        ).fetchone()
        if table is None:
            return
        cls._ensure_column(connection, "scenes", "is_default", "INTEGER NOT NULL DEFAULT 0")
        connection.execute("UPDATE scenes SET is_default=0 WHERE is_default NOT IN (0, 1)")
        default_rows = connection.execute(
            "SELECT id FROM scenes WHERE is_default=1 ORDER BY updated_at DESC, id"
        ).fetchall()
        for row in default_rows[1:]:
            connection.execute("UPDATE scenes SET is_default=0 WHERE id=?", (row["id"],))
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_scenes_single_default
               ON scenes(is_default) WHERE is_default=1"""
        )

    @classmethod
    def _migrate_to_20(cls, connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scenes'"
        ).fetchone()
        if table is None:
            return
        cls._ensure_column(
            connection, "scenes", "detailed_description", "TEXT NOT NULL DEFAULT ''"
        )

    @classmethod
    def _migrate_to_21(cls, connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
        ).fetchone()
        if table is None:
            return
        cls._ensure_column(connection, "operations", "total_steps", "INTEGER")

    @classmethod
    def _migrate_to_22(cls, connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='registered_services'"
        ).fetchone()
        if table is None:
            return
        cls._ensure_column(
            connection, "registered_services", "wsl_portproxy_enabled",
            "INTEGER NOT NULL DEFAULT 0",
        )
        cls._ensure_column(
            connection, "registered_services", "wsl_distro",
            "TEXT NOT NULL DEFAULT 'Ubuntu-22.04'",
        )
        cls._ensure_column(
            connection, "registered_services", "wsl_listen_address",
            "TEXT NOT NULL DEFAULT '0.0.0.0'",
        )
        cls._ensure_column(connection, "registered_services", "wsl_listen_port", "INTEGER")
        cls._ensure_column(connection, "registered_services", "wsl_connect_port", "INTEGER")
        cls._ensure_column(connection, "registered_services", "wsl_last_address", "TEXT")

    @classmethod
    def _migrate_to_23(cls, connection: sqlite3.Connection) -> None:
        scenes_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scenes'"
        ).fetchone()
        if scenes_table is not None:
            cls._ensure_column(
                connection, "scenes", "purpose", "TEXT NOT NULL DEFAULT ''"
            )
            connection.execute(
                "UPDATE scenes SET purpose='' WHERE purpose NOT IN ('', 'code_agent', 'video_gen')"
            )
            duplicate_purposes = connection.execute(
                """SELECT purpose FROM scenes WHERE purpose <> ''
                   GROUP BY purpose HAVING COUNT(*) > 1"""
            ).fetchall()
            for duplicate in duplicate_purposes:
                rows = connection.execute(
                    "SELECT id FROM scenes WHERE purpose=? ORDER BY updated_at DESC,id",
                    (duplicate["purpose"],),
                ).fetchall()
                for row in rows[1:]:
                    connection.execute("UPDATE scenes SET purpose='' WHERE id=?", (row["id"],))
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_scenes_unique_purpose
                   ON scenes(purpose) WHERE purpose <> ''"""
            )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS video_jobs (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_hash TEXT NOT NULL,
                session_id TEXT NOT NULL,
                workflow_path TEXT NOT NULL,
                workflow_json TEXT NOT NULL,
                video_spec TEXT NOT NULL DEFAULT '{}',
                requested_output_path TEXT,
                callback_url TEXT NOT NULL,
                callback_authorization TEXT NOT NULL DEFAULT '',
                callback_directory TEXT,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                prompt_id TEXT,
                output_path TEXT,
                shared_output_path TEXT,
                result TEXT,
                progress TEXT,
                error_code TEXT,
                error_summary TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                callback_attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_jobs_status_created "
            "ON video_jobs(status,created_at)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS resource_leases (
                resource_key TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES video_jobs(id) ON DELETE CASCADE,
                acquired_at TEXT NOT NULL
            )"""
        )

    @staticmethod
    def _migrate_to_24(connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_jobs'"
        ).fetchone()
        if table is None:
            return
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(video_jobs)")
        }
        if "callback_authorization" in columns:
            connection.execute("ALTER TABLE video_jobs DROP COLUMN callback_authorization")

    @classmethod
    def _migrate_to_25(cls, connection: sqlite3.Connection) -> None:
        legacy_video = None
        legacy_code = None
        scenes_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scenes'"
        ).fetchone()
        if scenes_table is not None:
            cls._ensure_column(
                connection, "scenes", "is_default_generation", "INTEGER NOT NULL DEFAULT 0"
            )
            cls._ensure_column(
                connection, "scenes", "is_last_activated", "INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute(
                "UPDATE scenes SET is_default_generation=0 "
                "WHERE is_default_generation NOT IN (0, 1)"
            )
            legacy_video = connection.execute(
                "SELECT id,name FROM scenes WHERE purpose='video_gen' "
                "ORDER BY updated_at DESC,id LIMIT 1"
            ).fetchone()
            legacy_code = connection.execute(
                "SELECT id,name FROM scenes WHERE purpose='code_agent' "
                "ORDER BY updated_at DESC,id LIMIT 1"
            ).fetchone()
            defaults = connection.execute(
                "SELECT id FROM scenes WHERE is_default_generation=1 "
                "ORDER BY updated_at DESC,id"
            ).fetchall()
            if not defaults and legacy_video is not None:
                connection.execute(
                    "UPDATE scenes SET is_default_generation=1 WHERE id=?",
                    (legacy_video["id"],),
                )
                defaults = [legacy_video]
            for row in defaults[1:]:
                connection.execute(
                    "UPDATE scenes SET is_default_generation=0 WHERE id=?", (row["id"],)
                )
            last_activated = connection.execute(
                "SELECT id FROM scenes WHERE is_last_activated=1 ORDER BY updated_at DESC,id"
            ).fetchall()
            if not last_activated:
                operations_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
                ).fetchone()
                if operations_table is not None:
                    latest_scene = connection.execute(
                        """SELECT target_id FROM operations
                           WHERE kind='scene' AND action='activate' AND status='succeeded'
                           ORDER BY finished_at DESC,created_at DESC,id DESC LIMIT 1"""
                    ).fetchone()
                    if latest_scene is not None:
                        connection.execute(
                            "UPDATE scenes SET is_last_activated=1 WHERE id=?",
                            (latest_scene["target_id"],),
                        )
                        last_activated = [latest_scene]
            for row in last_activated[1:]:
                connection.execute(
                    "UPDATE scenes SET is_last_activated=0 WHERE id=?", (row["id"],)
                )
            connection.execute("DROP INDEX IF EXISTS idx_scenes_unique_purpose")
            connection.execute("UPDATE scenes SET purpose='' WHERE purpose <> ''")
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_scenes_single_default_generation
                   ON scenes(is_default_generation) WHERE is_default_generation=1"""
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_scenes_single_last_activated
                   ON scenes(is_last_activated) WHERE is_last_activated=1"""
            )

        video_jobs_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_jobs'"
        ).fetchone()
        if video_jobs_table is None:
            return
        for column in ("generation_scene_id", "generation_scene_name",
                       "original_scene_id", "original_scene_name"):
            cls._ensure_column(connection, "video_jobs", column, "TEXT")
        if legacy_video is not None:
            connection.execute(
                """UPDATE video_jobs
                   SET generation_scene_id=COALESCE(generation_scene_id,?),
                       generation_scene_name=COALESCE(generation_scene_name,?)""",
                (legacy_video["id"], legacy_video["name"]),
            )
        if legacy_code is not None:
            connection.execute(
                """UPDATE video_jobs
                   SET original_scene_id=COALESCE(original_scene_id,?),
                       original_scene_name=COALESCE(original_scene_name,?)""",
                (legacy_code["id"], legacy_code["name"]),
            )

    @classmethod
    def _migrate_to_26(cls, connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_jobs'"
        ).fetchone()
        if table is None:
            return
        cls._ensure_column(connection, "video_jobs", "batch_id", "TEXT")
        cls._ensure_column(connection, "video_jobs", "batch_index", "INTEGER")
        cls._ensure_column(connection, "video_jobs", "batch_size", "INTEGER")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_jobs_batch_index "
            "ON video_jobs(batch_id,batch_index)"
        )

    @classmethod
    def _migrate_to_27(cls, connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_jobs'"
        ).fetchone()
        if table is None:
            return
        cls._ensure_column(connection, "video_jobs", "video_spec", "TEXT NOT NULL DEFAULT '{}'")
        cls._backfill_video_specs(connection)

    @classmethod
    def _migrate_to_28(cls, connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_jobs'"
        ).fetchone()
        if table is None:
            return
        cls._backfill_video_specs(connection)

    @classmethod
    def _migrate_to_29(cls, connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_jobs'"
        ).fetchone()
        if table is None:
            return
        cls._ensure_column(connection, "video_jobs", "shared_output_path", "TEXT")

    @staticmethod
    def _migrate_to_30(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS automatic_tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed')),
                execution_session_id TEXT,
                execution_token TEXT,
                lease_expires_at TEXT,
                result_summary TEXT,
                error_summary TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_automatic_tasks_status_created "
            "ON automatic_tasks(status,created_at,id)"
        )

    @classmethod
    def _migrate_to_31(cls, connection: sqlite3.Connection) -> None:
        cls._ensure_column(connection, "automatic_tasks", "queue_position", "INTEGER")
        maximum = connection.execute(
            "SELECT COALESCE(MAX(queue_position),-1) FROM automatic_tasks"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT id FROM automatic_tasks WHERE queue_position IS NULL ORDER BY created_at,id"
        ).fetchall()
        connection.executemany(
            "UPDATE automatic_tasks SET queue_position=? WHERE id=?",
            [(position, row["id"]) for position, row in enumerate(rows, start=maximum + 1)],
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_automatic_tasks_status_position "
            "ON automatic_tasks(status,queue_position,id)"
        )

    @classmethod
    def _backfill_video_specs(cls, connection: sqlite3.Connection) -> None:
        cursor = connection.execute("SELECT id,workflow_json FROM video_jobs")
        while rows := cursor.fetchmany(8):
            connection.executemany(
                "UPDATE video_jobs SET video_spec=? WHERE id=?",
                [
                    (json.dumps(cls._video_spec(row["workflow_json"]), separators=(",", ":")), row["id"])
                    for row in rows
                ],
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
                               sampled_at,cpu_load_percent,cpu_temperature_c,memory_percent,
                               memory_used_bytes,memory_total_bytes,cpu_frequency_mhz,
                               memory_available_bytes,commit_used_bytes,commit_limit_bytes,
                               swap_used_bytes,swap_total_bytes,
                               network_received_bytes_per_second,network_sent_bytes_per_second,
                               wsl_memory_used_bytes,wsl_swap_used_bytes
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(sampled_at) DO UPDATE SET
                               cpu_load_percent=excluded.cpu_load_percent,
                               cpu_temperature_c=excluded.cpu_temperature_c,
                               memory_percent=excluded.memory_percent,
                               memory_used_bytes=excluded.memory_used_bytes,
                               memory_total_bytes=excluded.memory_total_bytes,
                               cpu_frequency_mhz=excluded.cpu_frequency_mhz,
                               memory_available_bytes=excluded.memory_available_bytes,
                               commit_used_bytes=excluded.commit_used_bytes,
                               commit_limit_bytes=excluded.commit_limit_bytes,
                               swap_used_bytes=excluded.swap_used_bytes,
                               swap_total_bytes=excluded.swap_total_bytes,
                               network_received_bytes_per_second=excluded.network_received_bytes_per_second,
                               network_sent_bytes_per_second=excluded.network_sent_bytes_per_second,
                               wsl_memory_used_bytes=excluded.wsl_memory_used_bytes,
                               wsl_swap_used_bytes=excluded.wsl_swap_used_bytes""",
                        (sampled_at, sample.get("cpu_load_percent"),
                         sample.get("cpu_temperature_c"), sample.get("memory_percent"),
                         sample.get("memory_used_bytes"), sample.get("memory_total_bytes"),
                         sample.get("cpu_frequency_mhz"), sample.get("memory_available_bytes"),
                         sample.get("commit_used_bytes"), sample.get("commit_limit_bytes"),
                         sample.get("swap_used_bytes"), sample.get("swap_total_bytes"),
                         sample.get("network_received_bytes_per_second"),
                         sample.get("network_sent_bytes_per_second"),
                         sample.get("wsl_memory_used_bytes"),
                         sample.get("wsl_swap_used_bytes")),
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
                                   memory_used_mib,memory_total_mib,memory_percent,temperature_c,
                                   power_w,graphics_clock_mhz,memory_utilization_percent,
                                   encoder_percent,decoder_percent
                               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (sample_id, gpu_key, uuid or None, gpu_index, gpu.get("name"),
                             gpu.get("load_percent"), gpu.get("memory_used_mib"),
                             gpu.get("memory_total_mib"), gpu.get("memory_percent"),
                             gpu.get("temperature_c"), gpu.get("power_w"),
                             gpu.get("graphics_clock_mhz"),
                             gpu.get("memory_utilization_percent"),
                             gpu.get("encoder_percent"), gpu.get("decoder_percent")),
                        )
                    connection.execute(
                        "DELETE FROM resource_disk_samples WHERE sample_id=?", (sample_id,)
                    )
                    disk_occurrences: dict[str, int] = {}
                    for position, disk in enumerate(sample.get("disks", [])):
                        name = str(disk.get("name") or "").strip()
                        base_key = name or f"position:{position}"
                        occurrence = disk_occurrences.get(base_key, 0)
                        disk_occurrences[base_key] = occurrence + 1
                        disk_key = f"{base_key}#{occurrence}"
                        connection.execute(
                            """INSERT INTO resource_disk_samples(
                                   sample_id,disk_key,name,read_bytes_per_second,
                                   write_bytes_per_second,latency_ms
                               ) VALUES (?,?,?,?,?,?)""",
                            (sample_id, disk_key, name or None,
                             disk.get("read_bytes_per_second"),
                             disk.get("write_bytes_per_second"), disk.get("latency_ms")),
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
                                  AVG(memory_percent) AS memory_percent,
                                  AVG(memory_used_bytes) AS memory_used_bytes,
                                  MAX(memory_total_bytes) AS memory_total_bytes,
                                  AVG(cpu_frequency_mhz) AS cpu_frequency_mhz,
                                  AVG(memory_available_bytes) AS memory_available_bytes,
                                  AVG(commit_used_bytes) AS commit_used_bytes,
                                  MAX(commit_limit_bytes) AS commit_limit_bytes,
                                  AVG(swap_used_bytes) AS swap_used_bytes,
                                  MAX(swap_total_bytes) AS swap_total_bytes,
                                  AVG(network_received_bytes_per_second) AS network_received_bytes_per_second,
                                  AVG(network_sent_bytes_per_second) AS network_sent_bytes_per_second,
                                  AVG(wsl_memory_used_bytes) AS wsl_memory_used_bytes,
                                  AVG(wsl_swap_used_bytes) AS wsl_swap_used_bytes
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
                                  AVG(g.temperature_c) AS temperature_c,
                                  AVG(g.power_w) AS power_w,
                                  AVG(g.graphics_clock_mhz) AS graphics_clock_mhz,
                                  AVG(g.memory_utilization_percent) AS memory_utilization_percent,
                                  AVG(g.encoder_percent) AS encoder_percent,
                                  AVG(g.decoder_percent) AS decoder_percent
                           FROM resource_gpu_samples g
                           JOIN resource_samples s ON s.id=g.sample_id
                           WHERE s.sampled_at>=? GROUP BY bucket,g.gpu_key
                           ORDER BY bucket,g.gpu_index,g.gpu_key""",
                        (bucket_seconds, cutoff),
                    ).fetchall()
                    disk_rows = connection.execute(
                        """SELECT CAST(strftime('%s',s.sampled_at) AS INTEGER)/? AS bucket,
                                  d.disk_key,MAX(d.name) AS name,
                                  AVG(d.read_bytes_per_second) AS read_bytes_per_second,
                                  AVG(d.write_bytes_per_second) AS write_bytes_per_second,
                                  AVG(d.latency_ms) AS latency_ms
                           FROM resource_disk_samples d
                           JOIN resource_samples s ON s.id=d.sample_id
                           WHERE s.sampled_at>=? GROUP BY bucket,d.disk_key
                           ORDER BY bucket,d.disk_key""",
                        (bucket_seconds, cutoff),
                    ).fetchall()
                    samples = self._assemble_resource_history(
                        host_rows, gpu_rows, disk_rows, "bucket"
                    )
                else:
                    host_rows = connection.execute(
                        """SELECT id,sampled_at,cpu_load_percent,cpu_temperature_c,memory_percent,
                                  memory_used_bytes,memory_total_bytes
                                  ,cpu_frequency_mhz,memory_available_bytes,
                                  commit_used_bytes,commit_limit_bytes,swap_used_bytes,
                                  swap_total_bytes,network_received_bytes_per_second,
                                  network_sent_bytes_per_second,wsl_memory_used_bytes,
                                  wsl_swap_used_bytes
                           FROM resource_samples WHERE sampled_at>=? ORDER BY sampled_at""",
                        (cutoff,),
                    ).fetchall()
                    gpu_rows = connection.execute(
                        """SELECT g.*,s.sampled_at FROM resource_gpu_samples g
                           JOIN resource_samples s ON s.id=g.sample_id
                           WHERE s.sampled_at>=? ORDER BY s.sampled_at,g.gpu_index,g.gpu_key""",
                        (cutoff,),
                    ).fetchall()
                    disk_rows = connection.execute(
                        """SELECT d.*,s.sampled_at FROM resource_disk_samples d
                           JOIN resource_samples s ON s.id=d.sample_id
                           WHERE s.sampled_at>=? ORDER BY s.sampled_at,d.disk_key""",
                        (cutoff,),
                    ).fetchall()
                    samples = self._assemble_resource_history(
                        host_rows, gpu_rows, disk_rows, "sample_id"
                    )
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
        host_rows: list[sqlite3.Row], gpu_rows: list[sqlite3.Row],
        disk_rows: list[sqlite3.Row], key_name: str,
    ) -> list[dict[str, Any]]:
        samples: dict[int, dict[str, Any]] = {}
        for row in host_rows:
            key = int(row[key_name] if key_name == "bucket" else row["id"])
            samples[key] = {
                "sampled_at": row["sampled_at"],
                "cpu_load_percent": row["cpu_load_percent"],
                "cpu_temperature_c": row["cpu_temperature_c"],
                "memory_percent": row["memory_percent"],
                "memory_used_bytes": row["memory_used_bytes"],
                "memory_total_bytes": row["memory_total_bytes"],
                "cpu_frequency_mhz": row["cpu_frequency_mhz"],
                "memory_available_bytes": row["memory_available_bytes"],
                "commit_used_bytes": row["commit_used_bytes"],
                "commit_limit_bytes": row["commit_limit_bytes"],
                "swap_used_bytes": row["swap_used_bytes"],
                "swap_total_bytes": row["swap_total_bytes"],
                "network_received_bytes_per_second": row[
                    "network_received_bytes_per_second"
                ],
                "network_sent_bytes_per_second": row[
                    "network_sent_bytes_per_second"
                ],
                "wsl_memory_used_bytes": row["wsl_memory_used_bytes"],
                "wsl_swap_used_bytes": row["wsl_swap_used_bytes"],
                "gpus": [],
                "disks": [],
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
                "power_w": row["power_w"],
                "graphics_clock_mhz": row["graphics_clock_mhz"],
                "memory_utilization_percent": row["memory_utilization_percent"],
                "encoder_percent": row["encoder_percent"],
                "decoder_percent": row["decoder_percent"],
            })
        for row in disk_rows:
            key = int(row[key_name])
            if key not in samples:
                continue
            samples[key]["disks"].append({
                "name": row["name"],
                "read_bytes_per_second": row["read_bytes_per_second"],
                "write_bytes_per_second": row["write_bytes_per_second"],
                "latency_ms": row["latency_ms"],
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
            "started_at", "finished_at", "audit_event_id", "total_steps",
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
                               health_url,health_expect,wsl_portproxy_enabled,wsl_distro,
                               wsl_listen_address,wsl_listen_port,wsl_connect_port,
                               wsl_last_address,created_at,updated_at
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (item["id"], item["name"], item["description"], item["script_path"],
                         item["gpu_label"], item["port"], item["ui_url"],
                         item.get("health_url", ""), item.get("health_expect", ""),
                         int(item.get("wsl_portproxy_enabled", False)),
                         item.get("wsl_distro", "Ubuntu-22.04"),
                         item.get("wsl_listen_address", "0.0.0.0"),
                         item.get("wsl_listen_port"), item.get("wsl_connect_port"),
                         item.get("wsl_last_address"), now, now),
                    )
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("服务名称或 ID 已存在") from exc
        except (sqlite3.Error, KeyError) as exc:
            raise DatabaseError(f"创建已登记服务失败: {exc}") from exc

    def update_registered_service(
        self, service_id: str, item: dict[str, Any],
        audit_source_ip: str | None = None, audit_summary: dict[str, Any] | None = None,
    ) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        """UPDATE registered_services SET name=?,description=?,script_path=?,
                               gpu_label=?,port=?,ui_url=?,health_url=?,health_expect=?,
                               wsl_portproxy_enabled=?,wsl_distro=?,wsl_listen_address=?,
                               wsl_listen_port=?,wsl_connect_port=?,
                               wsl_last_address=CASE
                                   WHEN wsl_portproxy_enabled<>? OR wsl_distro<>?
                                        OR wsl_listen_address<>?
                                        OR wsl_listen_port IS NOT ? OR wsl_connect_port IS NOT ?
                                   THEN NULL ELSE wsl_last_address END,
                               desired_state=CASE WHEN script_path<>? THEN 'unknown' ELSE desired_state END,
                               observed_state=CASE
                                   WHEN script_path<>? OR health_url<>? OR health_expect<>?
                                   THEN 'unknown' ELSE observed_state END,
                               observed_at=CASE
                                   WHEN script_path<>? OR health_url<>? OR health_expect<>?
                                   THEN NULL ELSE observed_at END,
                               observed_error=CASE
                                   WHEN script_path<>? OR health_url<>? OR health_expect<>?
                                   THEN NULL ELSE observed_error END,
                               recorded_state=CASE
                                   WHEN script_path<>? OR health_url<>? OR health_expect<>?
                                   THEN 'unknown' ELSE recorded_state END,
                               state_updated_at=CASE
                                   WHEN script_path<>? OR health_url<>? OR health_expect<>?
                                   THEN NULL ELSE state_updated_at END,
                               state_error=CASE
                                   WHEN script_path<>? OR health_url<>? OR health_expect<>?
                                   THEN NULL ELSE state_error END,
                               updated_at=? WHERE id=?""",
                        (item["name"], item["description"], item["script_path"],
                         item["gpu_label"], item["port"], item["ui_url"],
                         item["health_url"], item["health_expect"],
                         int(item["wsl_portproxy_enabled"]), item["wsl_distro"],
                         item["wsl_listen_address"], item["wsl_listen_port"],
                         item["wsl_connect_port"],
                         int(item["wsl_portproxy_enabled"]), item["wsl_distro"],
                         item["wsl_listen_address"], item["wsl_listen_port"],
                         item["wsl_connect_port"],
                         item["script_path"],
                         item["script_path"], item["health_url"], item["health_expect"],
                         item["script_path"], item["health_url"], item["health_expect"],
                         item["script_path"], item["health_url"], item["health_expect"],
                         item["script_path"], item["health_url"], item["health_expect"],
                         item["script_path"], item["health_url"], item["health_expect"],
                         item["script_path"], item["health_url"], item["health_expect"],
                         utc_now(), service_id),
                    )
                    updated = cursor.rowcount == 1
                    if updated and audit_source_ip is not None and audit_summary is not None:
                        self.insert_audit(
                            connection, audit_source_ip, "management.service.update",
                            "success", audit_summary,
                        )
                    return updated
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("服务名称已存在") from exc
        except (sqlite3.Error, KeyError) as exc:
            raise DatabaseError(f"更新已登记服务失败: {exc}") from exc

    def update_registered_service_portproxy_address(
        self, service_id: str, address: str | None
    ) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        "UPDATE registered_services SET wsl_last_address=? WHERE id=?",
                        (address, service_id),
                    )
                    return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise DatabaseError(f"保存 WSL 端口转发目标失败: {exc}") from exc

    def update_registered_service_status(
        self, service_id: str, state: str, error: str | None
    ) -> bool:
        checked_at = utc_now()
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        """UPDATE registered_services
                           SET observed_state=?,observed_at=?,observed_error=?,
                               recorded_state=?,state_updated_at=?,state_error=? WHERE id=?""",
                        (state, checked_at, error, state, checked_at, error, service_id),
                    )
                    return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise DatabaseError(f"保存服务状态失败: {exc}") from exc

    def update_registered_service_desired_state(self, service_id: str, state: str) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        "UPDATE registered_services SET desired_state=? WHERE id=?",
                        (state, service_id),
                    )
                    return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise DatabaseError(f"保存服务期望状态失败: {exc}") from exc

    def delete_registered_service(
        self, service_id: str, audit_source_ip: str | None = None,
        audit_summary: dict[str, Any] | None = None,
    ) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        "DELETE FROM registered_services WHERE id=?", (service_id,)
                    )
                    deleted = cursor.rowcount == 1
                    if deleted and audit_source_ip is not None and audit_summary is not None:
                        self.insert_audit(
                            connection, audit_source_ip, "management.service.delete",
                            "success", audit_summary,
                        )
                    return deleted
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
                    item.pop("purpose", None)
                    item.pop("is_last_activated", None)
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

    def get_default_scene(self) -> dict[str, Any] | None:
        return next((item for item in self.list_scenes() if item["is_default"] == 1), None)

    def get_default_generation_scene(self) -> dict[str, Any] | None:
        return next(
            (item for item in self.list_scenes() if item["is_default_generation"] == 1),
            None,
        )

    def get_scene_by_name(self, name: str) -> dict[str, Any] | None:
        lowered = name.casefold()
        return next(
            (item for item in self.list_scenes() if str(item["name"]).casefold() == lowered),
            None,
        )

    def get_last_activated_scene(self) -> dict[str, Any] | None:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT id FROM scenes WHERE is_last_activated=1"
                ).fetchone()
            return None if row is None else self.get_scene(row["id"])
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取最后激活场景失败: {exc}") from exc

    def set_last_activated_scene(self, scene_id: str) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if connection.execute(
                        "SELECT 1 FROM scenes WHERE id=?", (scene_id,)
                    ).fetchone() is None:
                        raise DatabaseError("场景不存在")
                    connection.execute(
                        "UPDATE scenes SET is_last_activated=0 WHERE is_last_activated=1"
                    )
                    connection.execute(
                        "UPDATE scenes SET is_last_activated=1 WHERE id=?", (scene_id,)
                    )
        except sqlite3.Error as exc:
            raise DatabaseError(f"记录最后激活场景失败: {exc}") from exc

    def set_default_scene(self, scene_id: str, enabled: bool) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    exists = connection.execute(
                        "SELECT 1 FROM scenes WHERE id=?", (scene_id,)
                    ).fetchone()
                    if exists is None:
                        return False
                    if enabled:
                        connection.execute("UPDATE scenes SET is_default=0 WHERE is_default=1")
                        connection.execute(
                            "UPDATE scenes SET is_default=1,updated_at=? WHERE id=?",
                            (utc_now(), scene_id),
                        )
                    else:
                        connection.execute(
                            "UPDATE scenes SET is_default=0,updated_at=? WHERE id=?",
                            (utc_now(), scene_id),
                        )
                    return True
        except sqlite3.Error as exc:
            raise DatabaseError(f"保存默认场景失败: {exc}") from exc

    def create_scene(self, item: dict[str, Any]) -> None:
        now = utc_now()
        try:
            with self.connect() as connection:
                with connection:
                    row = connection.execute(
                        "SELECT COALESCE(MAX(display_order), -1) + 1 AS next_order FROM scenes"
                    ).fetchone()
                    if item.get("is_default_generation"):
                        connection.execute(
                            "UPDATE scenes SET is_default_generation=0 "
                            "WHERE is_default_generation=1"
                        )
                    connection.execute(
                        """INSERT INTO scenes(
                               id,name,description,detailed_description,purpose,
                               is_default_generation,display_order,
                               created_at,updated_at
                           ) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (item["id"], item["name"], item["description"],
                         item.get("detailed_description", ""), "",
                         int(bool(item.get("is_default_generation"))),
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
                    if item.get("is_default_generation"):
                        connection.execute(
                            "UPDATE scenes SET is_default_generation=0 "
                            "WHERE is_default_generation=1 AND id<>?", (scene_id,)
                        )
                    if "detailed_description" in item and "is_default_generation" in item:
                        cursor = connection.execute(
                            """UPDATE scenes
                               SET name=?,description=?,detailed_description=?,
                                   is_default_generation=?,updated_at=?
                               WHERE id=?""",
                            (item["name"], item["description"],
                             item["detailed_description"],
                             int(bool(item["is_default_generation"])), utc_now(), scene_id),
                        )
                    elif "is_default_generation" in item:
                        cursor = connection.execute(
                            """UPDATE scenes SET name=?,description=?,is_default_generation=?,
                               updated_at=? WHERE id=?""",
                            (item["name"], item["description"],
                             int(bool(item["is_default_generation"])), utc_now(), scene_id),
                        )
                    elif "detailed_description" in item:
                        cursor = connection.execute(
                            """UPDATE scenes
                               SET name=?,description=?,detailed_description=?,updated_at=?
                               WHERE id=?""",
                            (item["name"], item["description"],
                             item["detailed_description"], utc_now(), scene_id),
                        )
                    else:
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
    def automatic_task_title(content: str, maximum: int = 48) -> str:
        normalized = " ".join(content.split())
        normalized = re.sub(r"^(?:[#>*-]+|\d+[.)、])\s*", "", normalized).strip()
        sentence = re.split(r"(?<=[。！？!?；;])\s*", normalized, maxsplit=1)[0].strip()
        title = sentence or "自动任务"
        return title if len(title) <= maximum else f"{title[:maximum - 1].rstrip()}…"

    def create_automatic_task(
        self, task_id: str, content: str, username: str, source_ip: str,
    ) -> dict[str, Any]:
        now = utc_now()
        title = self.automatic_task_title(content)
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute(
                        """INSERT INTO automatic_tasks(
                               id,title,content,status,queue_position,created_at,updated_at
                           ) VALUES (?,?,?,'pending',
                               (SELECT COALESCE(MAX(queue_position),-1)+1 FROM automatic_tasks),?,?)""",
                        (task_id, title, content, now, now),
                    )
                    self.insert_audit(
                        connection, source_ip, "management.automatic_task.create", "success",
                        {"task_id": task_id, "title": title, "requested_by": username},
                    )
                    row = connection.execute(
                        "SELECT * FROM automatic_tasks WHERE id=?", (task_id,),
                    ).fetchone()
            if row is None:
                raise DatabaseError("创建自动任务后无法读回记录")
            return dict(row)
        except sqlite3.IntegrityError as exc:
            raise DatabaseError(f"自动任务标识冲突: {exc}") from exc
        except sqlite3.Error as exc:
            raise DatabaseError(f"创建自动任务失败: {exc}") from exc

    def list_automatic_tasks(self, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM automatic_tasks ORDER BY created_at,id LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取自动任务列表失败: {exc}") from exc

    def automatic_task_summary(self) -> dict[str, int]:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    """SELECT
                           SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                           SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                           COUNT(*) AS total
                       FROM automatic_tasks"""
                ).fetchone()
            return {
                "pending": int(row["pending"] or 0),
                "running": int(row["running"] or 0),
                "total": int(row["total"] or 0),
            }
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取自动任务统计失败: {exc}") from exc

    def update_automatic_task(
        self, task_id: str, content: str, username: str, source_ip: str,
    ) -> tuple[str, dict[str, Any] | None]:
        now = utc_now()
        title = self.automatic_task_title(content)
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    existing = connection.execute(
                        "SELECT status FROM automatic_tasks WHERE id=?", (task_id,),
                    ).fetchone()
                    if existing is None:
                        return "missing", None
                    if existing["status"] == "running":
                        return "running", None
                    connection.execute(
                        """UPDATE automatic_tasks SET title=?,content=?,status='pending',
                               execution_session_id=NULL,execution_token=NULL,lease_expires_at=NULL,
                               result_summary=NULL,error_summary=NULL,
                               queue_position=CASE WHEN status='pending' THEN queue_position ELSE
                                   (SELECT COALESCE(MAX(queue_position),-1)+1 FROM automatic_tasks) END,
                               updated_at=?,started_at=NULL,finished_at=NULL WHERE id=?""",
                        (title, content, now, task_id),
                    )
                    self.insert_audit(
                        connection, source_ip, "management.automatic_task.update", "success",
                        {"task_id": task_id, "title": title, "requested_by": username},
                    )
                    row = connection.execute(
                        "SELECT * FROM automatic_tasks WHERE id=?", (task_id,),
                    ).fetchone()
            return "updated", None if row is None else dict(row)
        except sqlite3.Error as exc:
            raise DatabaseError(f"更新自动任务失败: {exc}") from exc

    def delete_automatic_task(
        self, task_id: str, username: str, source_ip: str,
    ) -> str:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT title,status FROM automatic_tasks WHERE id=?", (task_id,),
                    ).fetchone()
                    if row is None:
                        return "missing"
                    if row["status"] == "running":
                        return "running"
                    connection.execute("DELETE FROM automatic_tasks WHERE id=?", (task_id,))
                    self.insert_audit(
                        connection, source_ip, "management.automatic_task.delete", "success",
                        {"task_id": task_id, "title": row["title"], "requested_by": username},
                    )
                    return "deleted"
        except sqlite3.Error as exc:
            raise DatabaseError(f"删除自动任务失败: {exc}") from exc

    def reorder_automatic_tasks(
        self, previous_task_ids: list[str], task_ids: list[str],
        username: str, source_ip: str,
    ) -> str:
        if (len(previous_task_ids) != len(set(previous_task_ids))
                or len(task_ids) != len(set(task_ids))
                or set(task_ids) != set(previous_task_ids)):
            return "invalid"
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    pending = [
                        row["id"] for row in connection.execute(
                            """SELECT id FROM automatic_tasks WHERE status='pending'
                               ORDER BY queue_position,created_at,id"""
                        ).fetchall()
                    ]
                    if previous_task_ids != pending:
                        return "changed"
                    if set(task_ids) != set(pending):
                        return "invalid"
                    now = utc_now()
                    connection.executemany(
                        "UPDATE automatic_tasks SET queue_position=?,updated_at=? WHERE id=?",
                        [(position, now, task_id) for position, task_id in enumerate(task_ids)],
                    )
                    self.insert_audit(
                        connection, source_ip, "management.automatic_task.reorder", "success",
                        {"task_ids": task_ids, "requested_by": username},
                    )
            return "reordered"
        except sqlite3.Error as exc:
            raise DatabaseError(f"保存自动任务顺序失败: {exc}") from exc

    def claim_automatic_task(self, session_id: str) -> tuple[str, dict[str, Any] | None]:
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        lease_expires_at = (
            now_value + timedelta(seconds=self.AUTOMATIC_TASK_LEASE_SECONDS)
        ).isoformat()
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    running = connection.execute(
                        "SELECT * FROM automatic_tasks WHERE status='running' "
                        "ORDER BY started_at,id LIMIT 1"
                    ).fetchone()
                    if running is not None:
                        if str(running["lease_expires_at"] or "") > now:
                            if running["execution_session_id"] == session_id:
                                return "claimed", dict(running)
                            return "busy", None
                        connection.execute(
                            """UPDATE automatic_tasks SET status='pending',execution_session_id=NULL,
                                   execution_token=NULL,lease_expires_at=NULL,updated_at=?,started_at=NULL
                               WHERE id=? AND status='running'""",
                            (now, running["id"]),
                        )
                    row = connection.execute(
                        "SELECT * FROM automatic_tasks WHERE status='pending' "
                        "ORDER BY queue_position,created_at,id LIMIT 1"
                    ).fetchone()
                    if row is None:
                        return "empty", None
                    execution_token = secrets.token_urlsafe(32)
                    connection.execute(
                        """UPDATE automatic_tasks SET status='running',execution_session_id=?,execution_token=?,
                               lease_expires_at=?,result_summary=NULL,error_summary=NULL,
                               attempts=attempts+1,updated_at=?,started_at=?,finished_at=NULL
                           WHERE id=?""",
                        (session_id, execution_token, lease_expires_at, now, now, row["id"]),
                    )
                    claimed = connection.execute(
                        "SELECT * FROM automatic_tasks WHERE id=?", (row["id"],),
                    ).fetchone()
            return "claimed", None if claimed is None else dict(claimed)
        except sqlite3.Error as exc:
            raise DatabaseError(f"领取自动任务失败: {exc}") from exc

    def finish_automatic_task(
        self, task_id: str, session_id: str, execution_token: str,
        status: str, summary: str | None,
    ) -> tuple[str, dict[str, Any] | None]:
        now = utc_now()
        if status not in {"succeeded", "failed"}:
            raise DatabaseError("自动任务完成状态无效")
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT * FROM automatic_tasks WHERE id=?",
                        (task_id,),
                    ).fetchone()
                    if row is None:
                        return "missing", None
                    if (row["execution_session_id"] != session_id
                            or row["execution_token"] != execution_token):
                        return "owner_mismatch", None
                    if row["status"] in {"succeeded", "failed"}:
                        if row["status"] == status:
                            return "finished", dict(row)
                        return "already_finished", None
                    if row["status"] != "running":
                        return "already_finished", None
                    if str(row["lease_expires_at"] or "") <= now:
                        return "lease_expired", None
                    result_summary = summary if status == "succeeded" else None
                    error_summary = summary if status == "failed" else None
                    connection.execute(
                        """UPDATE automatic_tasks SET status=?,result_summary=?,error_summary=?,
                               lease_expires_at=NULL,updated_at=?,finished_at=? WHERE id=?""",
                        (status, result_summary, error_summary, now, now, task_id),
                    )
                    finished = connection.execute(
                        "SELECT * FROM automatic_tasks WHERE id=?", (task_id,),
                    ).fetchone()
            return "finished", None if finished is None else dict(finished)
        except sqlite3.Error as exc:
            raise DatabaseError(f"完成自动任务失败: {exc}") from exc

    def heartbeat_automatic_task(
        self, task_id: str, session_id: str, execution_token: str,
    ) -> tuple[str, dict[str, Any] | None]:
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        lease_expires_at = (
            now_value + timedelta(seconds=self.AUTOMATIC_TASK_LEASE_SECONDS)
        ).isoformat()
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT * FROM automatic_tasks WHERE id=?", (task_id,),
                    ).fetchone()
                    if row is None:
                        return "missing", None
                    if (row["status"] != "running"
                            or row["execution_session_id"] != session_id
                            or row["execution_token"] != execution_token):
                        return "owner_mismatch", None
                    if str(row["lease_expires_at"] or "") <= now:
                        return "lease_expired", None
                    connection.execute(
                        "UPDATE automatic_tasks SET lease_expires_at=?,updated_at=? WHERE id=?",
                        (lease_expires_at, now, task_id),
                    )
                    renewed = connection.execute(
                        "SELECT * FROM automatic_tasks WHERE id=?", (task_id,),
                    ).fetchone()
            return "renewed", None if renewed is None else dict(renewed)
        except sqlite3.Error as exc:
            raise DatabaseError(f"续期自动任务失败: {exc}") from exc

    def reset_automatic_task(
        self, task_id: str, username: str, source_ip: str,
    ) -> tuple[str, dict[str, Any] | None]:
        now = utc_now()
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT title,status FROM automatic_tasks WHERE id=?", (task_id,),
                    ).fetchone()
                    if row is None:
                        return "missing", None
                    connection.execute(
                        """UPDATE automatic_tasks SET status='pending',execution_session_id=NULL,
                               execution_token=NULL,lease_expires_at=NULL,result_summary=NULL,
                               error_summary=NULL,
                               queue_position=(SELECT COALESCE(MAX(queue_position),-1)+1
                                               FROM automatic_tasks),
                               updated_at=?,started_at=NULL,finished_at=NULL
                           WHERE id=?""",
                        (now, task_id),
                    )
                    self.insert_audit(
                        connection, source_ip, "management.automatic_task.reset", "success",
                        {"task_id": task_id, "title": row["title"], "requested_by": username},
                    )
                    reset = connection.execute(
                        "SELECT * FROM automatic_tasks WHERE id=?", (task_id,),
                    ).fetchone()
            return "reset", None if reset is None else dict(reset)
        except sqlite3.Error as exc:
            raise DatabaseError(f"重置自动任务失败: {exc}") from exc

    @staticmethod
    def _video_spec(workflow_json: str) -> dict[str, Any]:
        try:
            workflow = json.loads(workflow_json)
        except (TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(workflow, dict):
            return {}

        video_inputs: dict[str, Any] = {}
        fps: float | None = None
        steps: int | None = None
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type") or "")
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            if class_type.startswith("MiniMaxH3") and all(
                name in inputs for name in ("width", "height", "length")
            ):
                video_inputs = inputs
            if class_type == "CreateVideo":
                fps = Database._finite_number(inputs.get("fps"), 0, 1000)
            elif class_type == "VHS_VideoCombine":
                fps = Database._finite_number(inputs.get("frame_rate"), 0, 1000)
            if class_type == "BasicScheduler" or class_type.startswith("MiniMaxH3") \
                    and "Sampler" in class_type:
                steps = Database._bounded_int(inputs.get("steps"), 1, 10_000)

        def positive_int(name: str) -> int | None:
            maximum = 1_000_000 if name == "length" else 16_384
            return Database._bounded_int(video_inputs.get(name), 1, maximum)

        width = positive_int("width")
        height = positive_int("height")
        frames = positive_int("length")
        duration_value = frames / fps if frames is not None and fps else None
        duration = round(duration_value, 3) \
            if duration_value is not None and math.isfinite(duration_value) else None
        return {
            "title": Database._video_title(workflow),
            "width": width, "height": height, "frames": frames,
            "fps": fps, "duration_seconds": duration, "steps": steps,
        }

    @staticmethod
    def _video_title(workflow: dict[str, Any]) -> str | None:
        video_extensions = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
        for node in workflow.values():
            if not isinstance(node, dict) or "loadvideo" not in str(
                node.get("class_type") or ""
            ).lower():
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            for value in inputs.values():
                if isinstance(value, str) and value.lower().endswith(video_extensions):
                    title = Database._source_video_title(value)
                    if title:
                        return title

        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            if not str(node.get("class_type") or "").startswith("MiniMaxH3"):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            prompt = inputs.get("prompt")
            if not isinstance(prompt, str):
                continue
            match = re.search(r"(?im)^\s*summary\s*:\s*(.+)$", prompt)
            if match:
                return Database._compact_video_title(match.group(1))
        return None

    @staticmethod
    def _source_video_title(value: str) -> str | None:
        parts = [part.strip() for part in re.split(r"[\\/]", value) if part.strip()]
        if not parts:
            return None
        stem = re.sub(r"\.[^.]+$", "", parts[-1]).strip()
        cleaned_stem = re.sub(
            r"(?i)^(?:source|src|input|video|original)[_\-\s]+|"
            r"[_\-\s]+(?:source|src|input|video|original|原片|源片|素材)$",
            "",
            stem,
        ).strip(" _-")
        if cleaned_stem and not Database._is_generic_video_label(cleaned_stem):
            return Database._compact_video_title(cleaned_stem)

        for parent in reversed(parts[:-1]):
            if Database._is_generic_video_label(parent):
                continue
            cleaned_parent = re.sub(
                r"(?:[_\-\s]*(?:工作流交接|下载))+$", "", parent
            ).strip(" _-")
            if cleaned_parent and not Database._is_generic_video_label(cleaned_parent):
                return Database._compact_video_title(cleaned_parent)
        return None

    @staticmethod
    def _is_generic_video_label(value: str) -> bool:
        normalized = re.sub(r"[_\-\s]+", "", value).lower()
        return normalized in {
            "source", "src", "input", "video", "original", "sourcevideo", "videosource",
            "originalvideo", "asset", "assets", "media", "material", "materials", "videos",
            "原片", "源片", "素材", "原视频", "视频素材",
        }

    @staticmethod
    def _compact_video_title(value: str) -> str | None:
        compact = re.sub(r"\s+", " ", value).strip()
        if not compact:
            return None
        return compact if len(compact) <= 96 else f"{compact[:95].rstrip()}…"

    @staticmethod
    def _finite_number(value: Any, minimum: float, maximum: float) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except OverflowError:
            return None
        return number if math.isfinite(number) and minimum < number <= maximum else None

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) \
            and minimum <= value <= maximum else None

    @staticmethod
    def _decode_video_job(row: sqlite3.Row, *, include_internal: bool = False) -> dict[str, Any]:
        item = dict(row)
        item["cancel_requested"] = bool(item["cancel_requested"])
        try:
            item["video_spec"] = json.loads(item.get("video_spec") or "{}")
        except json.JSONDecodeError as exc:
            raise DatabaseError(f"视频任务规格数据损坏: {exc}") from exc
        if item.get("progress"):
            try:
                item["progress"] = json.loads(item["progress"])
            except json.JSONDecodeError as exc:
                raise DatabaseError(f"视频任务进度数据损坏: {exc}") from exc
        else:
            item["progress"] = None
        if not include_internal:
            item.pop("workflow_json", None)
        return item

    def create_video_job(self, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    existing = connection.execute(
                        "SELECT * FROM video_jobs WHERE idempotency_key=?",
                        (item["idempotency_key"],),
                    ).fetchone()
                    if existing is not None:
                        return self._decode_video_job(existing), False
                    connection.execute(
                        """INSERT INTO video_jobs(
                               id,idempotency_key,payload_hash,session_id,workflow_path,workflow_json,video_spec,
                               requested_output_path,callback_url,callback_directory,
                               generation_scene_id,generation_scene_name,
                               status,phase,created_at,updated_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            item["id"], item["idempotency_key"], item["payload_hash"],
                            item["session_id"], item["workflow_path"], item["workflow_json"],
                            json.dumps(self._video_spec(item["workflow_json"]), separators=(",", ":")),
                             item.get("requested_output_path"), item["callback_url"],
                             item.get("callback_directory"),
                             item["generation_scene_id"], item["generation_scene_name"],
                             "queued", "queued", now, now,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM video_jobs WHERE id=?", (item["id"],)
                    ).fetchone()
            if row is None:
                raise DatabaseError("创建视频任务后无法读回记录")
            return self._decode_video_job(row), True
        except sqlite3.IntegrityError as exc:
            raise DatabaseError(f"视频任务标识无效或冲突: {exc}") from exc
        except (sqlite3.Error, KeyError) as exc:
            raise DatabaseError(f"创建视频任务失败: {exc}") from exc

    def create_video_job_batch(self, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        if not items:
            raise DatabaseError("视频任务批次不能为空")
        now = utc_now()
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    keys = [item["idempotency_key"] for item in items]
                    placeholders = ",".join("?" for _ in keys)
                    existing = connection.execute(
                        f"SELECT * FROM video_jobs WHERE idempotency_key IN ({placeholders}) "
                        "ORDER BY batch_index", keys,
                    ).fetchall()
                    if existing:
                        if len(existing) != len(items):
                            raise DatabaseError("视频任务批次幂等记录不完整，拒绝补写部分批次")
                        return [self._decode_video_job(row) for row in existing], False
                    for item in items:
                        connection.execute(
                            """INSERT INTO video_jobs(
                                   id,idempotency_key,payload_hash,session_id,workflow_path,
                                   workflow_json,video_spec,requested_output_path,callback_url,
                                   callback_directory,generation_scene_id,generation_scene_name,
                                   batch_id,batch_index,batch_size,status,phase,created_at,updated_at
                               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                item["id"], item["idempotency_key"], item["payload_hash"],
                                item["session_id"], item["workflow_path"], item["workflow_json"],
                                json.dumps(self._video_spec(item["workflow_json"]), separators=(",", ":")),
                                item.get("requested_output_path"), item["callback_url"],
                                item.get("callback_directory"), item["generation_scene_id"],
                                item["generation_scene_name"], item["batch_id"],
                                item["batch_index"], item["batch_size"], "queued", "queued", now, now,
                            ),
                        )
                    rows = connection.execute(
                        "SELECT * FROM video_jobs WHERE batch_id=? ORDER BY batch_index",
                        (items[0]["batch_id"],),
                    ).fetchall()
            return [self._decode_video_job(row) for row in rows], True
        except sqlite3.IntegrityError as exc:
            raise DatabaseError(f"视频任务批次标识无效或冲突: {exc}") from exc
        except (sqlite3.Error, KeyError) as exc:
            raise DatabaseError(f"创建视频任务批次失败: {exc}") from exc

    def get_video_job(self, job_id: str, *, include_internal: bool = False) -> dict[str, Any] | None:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM video_jobs WHERE id=?", (job_id,)
                ).fetchone()
            return None if row is None else self._decode_video_job(
                row, include_internal=include_internal
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取视频任务失败: {exc}") from exc

    def list_video_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    f"SELECT {self.VIDEO_JOB_LIST_COLUMNS} FROM video_jobs "
                    "ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [self._decode_video_job(row) for row in rows]
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取视频任务列表失败: {exc}") from exc

    def next_video_job(self) -> dict[str, Any] | None:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    """SELECT * FROM video_jobs
                       WHERE status NOT IN ('succeeded','failed','cancelled')
                       ORDER BY created_at,COALESCE(batch_index,1),id LIMIT 1"""
                ).fetchone()
            return None if row is None else self._decode_video_job(row, include_internal=True)
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取待处理视频任务失败: {exc}") from exc

    def video_jobs_in_batch(
        self, batch_id: str, *, include_internal: bool = False,
    ) -> list[dict[str, Any]]:
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM video_jobs WHERE batch_id=? ORDER BY batch_index", (batch_id,)
                ).fetchall()
            return [self._decode_video_job(row, include_internal=include_internal) for row in rows]
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取视频任务批次失败: {exc}") from exc

    def video_job_queue_summary(self) -> dict[str, int]:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    """SELECT
                           SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                           SUM(CASE WHEN status NOT IN ('queued','succeeded','failed','cancelled')
                               THEN 1 ELSE 0 END) AS active,
                           SUM(CASE WHEN status NOT IN ('succeeded','failed','cancelled')
                               THEN 1 ELSE 0 END) AS nonterminal
                       FROM video_jobs"""
                ).fetchone()
            return {
                "queued_segments": int(row["queued"] or 0),
                "active_segments": int(row["active"] or 0),
                "nonterminal_segments": int(row["nonterminal"] or 0),
            }
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取视频任务队列统计失败: {exc}") from exc

    def abort_remaining_batch_jobs(self, batch_id: str, after_index: int, reason: str) -> int:
        now = utc_now()
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        """UPDATE video_jobs SET status='cancelled',phase='finished',
                               result='cancelled',error_code='batch_aborted',error_summary=?,
                               finished_at=?,updated_at=?
                           WHERE batch_id=? AND batch_index>? AND status='queued'""",
                        (reason, now, now, batch_id, after_index),
                    )
                    return cursor.rowcount
        except sqlite3.Error as exc:
            raise DatabaseError(f"终止视频任务批次失败: {exc}") from exc

    def update_video_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "status", "phase", "prompt_id", "output_path", "shared_output_path", "result", "progress",
            "error_code", "error_summary", "cancel_requested", "started_at", "finished_at",
            "callback_attempts", "original_scene_id", "original_scene_name",
        }
        if not fields or set(fields) - allowed:
            raise DatabaseError("视频任务更新字段不受支持")
        values = dict(fields)
        if "progress" in values:
            values["progress"] = None if values["progress"] is None else json.dumps(
                values["progress"], ensure_ascii=False, separators=(",", ":")
            )
        if "cancel_requested" in values:
            values["cancel_requested"] = int(bool(values["cancel_requested"]))
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{name}=?" for name in values)
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        f"UPDATE video_jobs SET {assignments} WHERE id=?",
                        (*values.values(), job_id),
                    )
                    if cursor.rowcount != 1:
                        raise DatabaseError("视频任务不存在")
        except sqlite3.Error as exc:
            raise DatabaseError(f"更新视频任务失败: {exc}") from exc

    def request_video_job_cancel(self, job_id: str) -> str:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT status,phase FROM video_jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if row is None:
                        return "missing"
                    if row["phase"] in {"callback_pending", "callback_delivered"} or row["status"] in {
                        "callback_pending", "callback_delivered",
                        "succeeded", "failed", "cancelled",
                    }:
                        return "finished"
                    connection.execute(
                        "UPDATE video_jobs SET cancel_requested=1,updated_at=? WHERE id=?",
                        (utc_now(), job_id),
                    )
                    return "requested"
        except sqlite3.Error as exc:
            raise DatabaseError(f"保存视频任务取消请求失败: {exc}") from exc

    def recover_video_jobs(self) -> None:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute(
                        """UPDATE video_jobs SET status=phase,updated_at=?
                           WHERE phase IN ('callback_pending','callback_delivered')
                             AND status NOT IN ('succeeded','failed','cancelled')""",
                        (utc_now(),),
                    )
                    connection.execute(
                        """DELETE FROM resource_leases WHERE owner_id IN (
                               SELECT id FROM video_jobs
                               WHERE status IN ('succeeded','failed','cancelled')
                           )"""
                    )
                    connection.execute(
                        """UPDATE video_jobs SET status='queued',updated_at=?
                           WHERE status NOT IN ('queued','succeeded','failed','cancelled')
                             AND phase NOT IN ('callback_pending','callback_delivered')""",
                        (utc_now(),),
                    )
        except sqlite3.Error as exc:
            raise DatabaseError(f"恢复视频任务失败: {exc}") from exc

    def acquire_resource_lease(self, resource_key: str, owner_id: str) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT owner_id FROM resource_leases WHERE resource_key=?",
                        (resource_key,),
                    ).fetchone()
                    if row is not None:
                        return row["owner_id"] == owner_id
                    connection.execute(
                        "INSERT INTO resource_leases(resource_key,owner_id,acquired_at) VALUES (?,?,?)",
                        (resource_key, owner_id, utc_now()),
                    )
                    return True
        except sqlite3.Error as exc:
            raise DatabaseError(f"获取资源租约失败: {exc}") from exc

    def release_resource_lease(self, resource_key: str, owner_id: str) -> bool:
        try:
            with self.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        "DELETE FROM resource_leases WHERE resource_key=? AND owner_id=?",
                        (resource_key, owner_id),
                    )
                    return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise DatabaseError(f"释放资源租约失败: {exc}") from exc

    def resource_lease_owner(self, resource_key: str) -> str | None:
        try:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT owner_id FROM resource_leases WHERE resource_key=?", (resource_key,)
                ).fetchone()
            return None if row is None else str(row["owner_id"])
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取资源租约失败: {exc}") from exc

    def finish_video_job_with_audit(
        self, job_id: str, status: str, result: str, output_path: str | None,
        error_code: str | None, error_summary: str | None, resource_key: str,
    ) -> None:
        now = utc_now()
        try:
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    cursor = connection.execute(
                        """UPDATE video_jobs SET status=?,phase='finished',result=?,output_path=?,
                               error_code=?,error_summary=?,finished_at=?,updated_at=? WHERE id=?""",
                        (status, result, output_path, error_code, error_summary, now, now, job_id),
                    )
                    if cursor.rowcount != 1:
                        raise DatabaseError("视频任务不存在")
                    self.insert_audit(
                        connection, "local", "management.video_job.finish",
                        "success" if status == "succeeded" else "failure",
                        {"job_id": job_id, "result": result, "error_code": error_code,
                         "output_path": output_path},
                    )
                    lease = connection.execute(
                        "DELETE FROM resource_leases WHERE resource_key=? AND owner_id=?",
                        (resource_key, job_id),
                    )
                    if lease.rowcount != 1:
                        raise DatabaseError("RTX 4090 租约不存在或不属于该视频任务")
        except sqlite3.Error as exc:
            raise DatabaseError(f"保存视频任务终态失败: {exc}") from exc

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
