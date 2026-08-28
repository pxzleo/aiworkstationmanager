from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import Database, DatabaseError, utc_now


PBKDF2_ITERATIONS = 310_000
MIN_PASSWORD_LENGTH = 4
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_MINUTES = 5
SESSION_COOKIE = "wm_session"
CSRF_HEADER = "X-CSRF-Token"
CSRF_TOKENS_PER_SESSION = 8


class AuthError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AuthenticatedSession:
    username: str
    token_hash: str
    csrf_hash: str
    expires_at: str


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def password_digest(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


class AuthService:
    def __init__(
        self, database: Database, session_ttl_seconds: int, session_max_active: int = 64
    ) -> None:
        self.database = database
        self.session_ttl_seconds = session_ttl_seconds
        self.session_max_active = session_max_active

    def is_setup(self) -> bool:
        try:
            with self.database.connect() as connection:
                return connection.execute("SELECT 1 FROM admin_user LIMIT 1").fetchone() is not None
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取管理员状态失败: {exc}") from exc

    @staticmethod
    def validate_credentials(username: str, password: str) -> tuple[str, str]:
        normalized = username.strip()
        if not 3 <= len(normalized) <= 64:
            raise AuthError(422, "invalid_credentials", "用户名长度必须为 3..64 个字符")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(
                422,
                "weak_password",
                f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符",
            )
        return normalized, password

    def setup(self, username: str, password: str, source_ip: str) -> tuple[str, str, str]:
        if not is_loopback(source_ip):
            self.database.append_audit(source_ip, "auth.setup", "failure", {"reason": "non_loopback"})
            raise AuthError(403, "loopback_required", "首次设置仅允许从本机访问")
        try:
            username, password = self.validate_credentials(username, password)
        except AuthError as exc:
            self.database.append_audit(source_ip, "auth.setup", "failure", {"reason": exc.code})
            raise
        salt = secrets.token_bytes(32)
        digest = password_digest(password, salt)
        token, csrf, expires_at = self._new_session_values()
        try:
            with self.database.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if connection.execute("SELECT 1 FROM admin_user LIMIT 1").fetchone():
                        raise AuthError(409, "already_setup", "管理员已完成设置")
                    connection.execute(
                        """INSERT INTO admin_user(
                               id, username, password_hash, password_salt, iterations, created_at
                           ) VALUES (1, ?, ?, ?, ?, ?)""",
                        (username, digest, salt, PBKDF2_ITERATIONS, utc_now()),
                    )
                    self._insert_session(connection, 1, token, csrf, expires_at, source_ip)
                    self.database.insert_audit(
                        connection, source_ip, "auth.setup", "success", {"username": username}
                    )
        except AuthError:
            self.database.append_audit(source_ip, "auth.setup", "failure", {"reason": "already_setup"})
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise DatabaseError(f"创建管理员失败: {exc}") from exc
        return token, csrf, expires_at

    def create_user(self, username: str, password: str, source_ip: str) -> dict[str, Any]:
        if not is_loopback(source_ip):
            self.database.append_audit(
                source_ip, "auth.user.create", "failure", {"reason": "non_loopback"}
            )
            raise AuthError(403, "loopback_required", "新增用户仅允许从本机执行")
        try:
            username, password = self.validate_credentials(username, password)
        except AuthError as exc:
            self.database.append_audit(
                source_ip, "auth.user.create", "failure", {"reason": exc.code}
            )
            raise
        salt = secrets.token_bytes(32)
        digest = password_digest(password, salt)
        created_at = utc_now()
        try:
            with self.database.connect() as connection:
                with connection:
                    cursor = connection.execute(
                        """INSERT INTO admin_user(
                               username,password_hash,password_salt,iterations,created_at
                           ) VALUES (?,?,?,?,?)""",
                        (username, digest, salt, PBKDF2_ITERATIONS, created_at),
                    )
                    self.database.insert_audit(
                        connection, source_ip, "auth.user.create", "success",
                        {"username": username},
                    )
        except sqlite3.IntegrityError as exc:
            self.database.append_audit(
                source_ip, "auth.user.create", "failure", {"reason": "username_exists"}
            )
            raise AuthError(409, "username_exists", "用户名已存在") from exc
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise DatabaseError(f"创建用户失败: {exc}") from exc
        return {"id": int(cursor.lastrowid), "username": username,
                "created_at": created_at, "active_sessions": 0}

    def list_users(self) -> list[dict[str, Any]]:
        now = utc_now()
        try:
            with self.database.connect() as connection:
                rows = connection.execute(
                    """SELECT a.id, a.username, a.created_at,
                              COUNT(s.token_hash) AS active_sessions
                       FROM admin_user a
                       LEFT JOIN sessions s
                         ON s.admin_id = a.id AND s.expires_at > ?
                       GROUP BY a.id, a.username, a.created_at
                       ORDER BY a.id""",
                    (now,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取用户列表失败: {exc}") from exc
        return [
            {"id": int(row["id"]), "username": row["username"],
             "created_at": row["created_at"],
             "active_sessions": int(row["active_sessions"])}
            for row in rows
        ]

    def update_user_password(
        self, user_id: int, password: str, requested_by: str, source_ip: str
    ) -> dict[str, Any]:
        try:
            if len(password) < MIN_PASSWORD_LENGTH:
                raise AuthError(
                    422, "weak_password", f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符"
                )
            salt = secrets.token_bytes(32)
            digest = password_digest(password, salt)
            with self.database.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT username FROM admin_user WHERE id = ?", (user_id,)
                    ).fetchone()
                    if row is None:
                        raise AuthError(404, "user_not_found", "用户不存在")
                    connection.execute(
                        """UPDATE admin_user
                           SET password_hash = ?, password_salt = ?, iterations = ?
                           WHERE id = ?""",
                        (digest, salt, PBKDF2_ITERATIONS, user_id),
                    )
                    connection.execute("DELETE FROM sessions WHERE admin_id = ?", (user_id,))
                    self.database.insert_audit(
                        connection, source_ip, "auth.user.password_update", "success",
                        {"username": row["username"], "requested_by": requested_by},
                    )
        except AuthError as exc:
            self.database.append_audit(
                source_ip, "auth.user.password_update", "failure",
                {"user_id": user_id, "requested_by": requested_by, "reason": exc.code},
            )
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise DatabaseError(f"修改用户密码失败: {exc}") from exc
        return {"id": user_id, "username": row["username"],
                "current_session_invalidated": row["username"] == requested_by}

    def delete_user(
        self, user_id: int, requested_by: str, source_ip: str
    ) -> dict[str, Any]:
        try:
            with self.database.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT username FROM admin_user WHERE id = ?", (user_id,)
                    ).fetchone()
                    if row is None:
                        raise AuthError(404, "user_not_found", "用户不存在")
                    if row["username"] == requested_by:
                        raise AuthError(409, "cannot_delete_current_user", "不能删除当前登录用户")
                    count = connection.execute(
                        "SELECT COUNT(*) FROM admin_user"
                    ).fetchone()[0]
                    if int(count) <= 1:
                        raise AuthError(409, "cannot_delete_last_user", "不能删除最后一个用户")
                    connection.execute("DELETE FROM admin_user WHERE id = ?", (user_id,))
                    self.database.insert_audit(
                        connection, source_ip, "auth.user.delete", "success",
                        {"username": row["username"], "requested_by": requested_by},
                    )
        except AuthError as exc:
            self.database.append_audit(
                source_ip, "auth.user.delete", "failure",
                {"user_id": user_id, "requested_by": requested_by, "reason": exc.code},
            )
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise DatabaseError(f"删除用户失败: {exc}") from exc
        return {"id": user_id, "username": row["username"]}

    def login(self, username: str, password: str, source_ip: str) -> tuple[str, str, str]:
        since = (datetime.now(timezone.utc) - timedelta(minutes=LOGIN_FAILURE_WINDOW_MINUTES)).isoformat()
        if self.database.is_login_rate_limited(source_ip, since, LOGIN_FAILURE_LIMIT):
            self.database.record_login_failure_atomic(source_ip, since, LOGIN_FAILURE_LIMIT)
            raise AuthError(429, "rate_limited", "登录失败次数过多，请稍后重试")
        # 对不存在用户也执行同等成本的 PBKDF2，避免用时差泄露账号状态。
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    """SELECT id, username, password_hash, password_salt, iterations
                       FROM admin_user WHERE username = ?""",
                    (username.strip(),),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(f"读取管理员失败: {exc}") from exc
        salt = bytes(row["password_salt"]) if row else b"\0" * 32
        iterations = int(row["iterations"]) if row else PBKDF2_ITERATIONS
        expected = bytes(row["password_hash"]) if row else b"\0" * 32
        actual = password_digest(password, salt, iterations)
        if row is None or not hmac.compare_digest(actual, expected):
            self._reject_login(source_ip, since)
        token, csrf, expires_at = self._new_session_values()
        try:
            with self.database.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute(
                        "SELECT password_hash FROM admin_user WHERE id = ?", (int(row["id"]),)
                    ).fetchone()
                    if current is None or not hmac.compare_digest(
                        bytes(current["password_hash"]), expected
                    ):
                        raise AuthError(401, "invalid_credentials", "用户名或密码错误")
                    self._insert_session(
                        connection, int(row["id"]), token, csrf, expires_at, source_ip
                    )
                    self.database.insert_audit(
                        connection,
                        source_ip,
                        "auth.login",
                        "success",
                        {"username": row["username"]},
                    )
        except AuthError:
            self._reject_login(source_ip, since)
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise DatabaseError(f"创建登录会话及审计失败: {exc}") from exc
        return token, csrf, expires_at

    def _reject_login(self, source_ip: str, since: str) -> None:
        limited = self.database.record_login_failure_atomic(
            source_ip, since, LOGIN_FAILURE_LIMIT
        )
        if limited:
            raise AuthError(429, "rate_limited", "登录失败次数过多，请稍后重试")
        raise AuthError(401, "invalid_credentials", "用户名或密码错误")

    def _new_session_values(self) -> tuple[str, str, str]:
        token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.session_ttl_seconds)).isoformat()
        return token, csrf, expires_at

    def _insert_session(
        self,
        connection: sqlite3.Connection,
        admin_id: int,
        token: str,
        csrf: str,
        expires_at: str,
        source_ip: str,
    ) -> None:
        now = utc_now()
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        connection.execute(
            """INSERT INTO sessions(
                   token_hash, admin_id, csrf_hash, created_at, expires_at, source_ip
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (secret_hash(token), admin_id, secret_hash(csrf), now, expires_at, source_ip),
        )
        connection.execute(
            """INSERT INTO session_csrf_tokens(
                   session_token_hash, csrf_hash, issued_at
               ) VALUES (?, ?, ?)""",
            (secret_hash(token), secret_hash(csrf), now),
        )
        connection.execute(
            """DELETE FROM sessions WHERE token_hash IN (
                   SELECT token_hash FROM sessions ORDER BY created_at DESC
                   LIMIT -1 OFFSET ?
               )""",
            (self.session_max_active,),
        )

    def authenticate(self, token: str | None) -> AuthenticatedSession:
        if not token:
            raise AuthError(401, "authentication_required", "需要登录")
        token_hash = secret_hash(token)
        now = utc_now()
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    """SELECT a.username, s.token_hash, s.csrf_hash, s.expires_at
                       FROM sessions s JOIN admin_user a ON a.id = s.admin_id
                       WHERE s.token_hash = ?""",
                    (token_hash,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(f"验证会话失败: {exc}") from exc
        if row is None:
            raise AuthError(401, "invalid_session", "会话无效或已过期")
        if row["expires_at"] <= now:
            try:
                with self.database.connect() as connection:
                    with connection:
                        connection.execute(
                            "DELETE FROM sessions WHERE token_hash = ?", (token_hash,)
                        )
            except sqlite3.Error as exc:
                raise DatabaseError(f"清理过期会话失败: {exc}") from exc
            raise AuthError(401, "invalid_session", "会话无效或已过期")
        return AuthenticatedSession(
            username=row["username"],
            token_hash=row["token_hash"],
            csrf_hash=row["csrf_hash"],
            expires_at=row["expires_at"],
        )

    def is_session_hash_active(self, token_hash: str) -> bool:
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT expires_at FROM sessions WHERE token_hash = ?",
                    (token_hash,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(f"验证只读资源会话失败: {exc}") from exc
        return row is not None and row["expires_at"] > utc_now()

    def rotate_csrf(self, session: AuthenticatedSession) -> str:
        """为会话追加 CSRF 值，保留有限个令牌供多个标签页并行使用。"""
        csrf = secrets.token_urlsafe(32)
        try:
            with self.database.connect() as connection:
                with connection:
                    if connection.execute(
                        "SELECT 1 FROM sessions WHERE token_hash = ?",
                        (session.token_hash,),
                    ).fetchone() is None:
                        raise AuthError(401, "invalid_session", "会话无效或已过期")
                    connection.execute(
                        """INSERT INTO session_csrf_tokens(
                               session_token_hash, csrf_hash, issued_at
                           ) VALUES (?, ?, ?)""",
                        (session.token_hash, secret_hash(csrf), utc_now()),
                    )
                    connection.execute(
                        """DELETE FROM session_csrf_tokens
                           WHERE session_token_hash = ? AND id NOT IN (
                               SELECT id FROM session_csrf_tokens
                               WHERE session_token_hash = ?
                               ORDER BY issued_at DESC, id DESC LIMIT ?
                           )""",
                        (
                            session.token_hash,
                            session.token_hash,
                            CSRF_TOKENS_PER_SESSION,
                        ),
                    )
        except AuthError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"刷新 CSRF 令牌失败: {exc}") from exc
        return csrf

    def verify_csrf(self, session: AuthenticatedSession, supplied: str | None) -> None:
        if not supplied:
            raise AuthError(403, "invalid_csrf", "CSRF 验证失败")
        supplied_hash = secret_hash(supplied)
        try:
            with self.database.connect() as connection:
                rows = connection.execute(
                    "SELECT csrf_hash FROM session_csrf_tokens WHERE session_token_hash = ?",
                    (session.token_hash,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"验证 CSRF 令牌失败: {exc}") from exc
        if not any(hmac.compare_digest(supplied_hash, row["csrf_hash"]) for row in rows):
            raise AuthError(403, "invalid_csrf", "CSRF 验证失败")

    def logout(self, session: AuthenticatedSession, source_ip: str) -> None:
        try:
            with self.database.connect() as connection:
                with connection:
                    connection.execute(
                        "DELETE FROM sessions WHERE token_hash = ?", (session.token_hash,)
                    )
                    self.database.insert_audit(
                        connection,
                        source_ip,
                        "auth.logout",
                        "success",
                        {"username": session.username},
                    )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise DatabaseError(f"注销会话及审计失败: {exc}") from exc
