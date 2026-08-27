from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import hashlib
import ipaddress
import secrets
import threading
import time
from urllib.parse import urlsplit
from typing import Any, AsyncIterator
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import CSRF_HEADER, SESSION_COOKIE, AuthenticatedSession, AuthError, AuthService, is_loopback
from .config import ConfigError, Settings, load_settings
from .database import SCHEMA_VERSION as DATABASE_SCHEMA_VERSION, Database, DatabaseError
from .control import ControlConfig, ControlConfigError, ControlError, ControlPlane, load_control_config
from .discovery import DiscoveryError, ScriptDiscovery, redact_sensitive_text
from .history import Sampler, parse_window
from .integrations import (
    IntegrationConfigError, IntegrationError, IntegrationsConfig, LogService,
    WebUIService, load_integrations_config,
)
from .manager_logging import configure_manager_logging
from . import __version__


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class EnvironmentActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern=r"^(start|stop|restart)$")
    confirmation: str = Field(min_length=1, max_length=160)


class SceneActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str = Field(min_length=1, max_length=160)


class RecoveryResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str = Field(min_length=1, max_length=160)


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    response = JSONResponse(
                        _error_body("request_body_too_large", "请求体超过限制"),
                        status_code=413,
                    )
                    await response(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break
        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)


class SecurityHeadersMiddleware:
    def __init__(
        self, app: ASGIApp,
        allowed_public_origins: tuple[str, ...] = (),
        trusted_proxy_ips: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.allowed_public_origins = set(allowed_public_origins)
        self.trusted_proxy_ips = set(trusted_proxy_ips)

    def _request_origin(self, scope: Scope) -> str | None:
        raw_host = next((value for name, value in scope.get("headers", []) if name.lower() == b"host"), b"")
        try:
            host = raw_host.decode("ascii").lower()
            parsed = urlsplit("//" + host)
            if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
                return None
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                return None
        except (UnicodeDecodeError, ValueError):
            return None
        peer = scope.get("client")
        peer_ip = ""
        if peer:
            try:
                peer_ip = ipaddress.ip_address(str(peer[0]).split("%", 1)[0]).compressed
            except ValueError:
                pass
        public_origin = f"https://{host}"
        if peer_ip in self.trusted_proxy_ips and public_origin in self.allowed_public_origins:
            return public_origin
        scheme = scope.get("scheme", "http")
        return f"{scheme}://{host}" if scheme in {"http", "https"} else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                request_path = scope.get("path", "")
                proxy_page = request_path.startswith("/proxy/webui/") or request_path.startswith("/proxy-asset/")
                if proxy_page:
                    origin = self._request_origin(scope)
                    if origin is None:
                        csp = "sandbox; default-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
                    else:
                        csp = (
                            "sandbox allow-scripts; default-src 'none'; "
                            f"script-src {origin}; style-src {origin} 'unsafe-inline'; "
                            f"img-src {origin} data: blob:; font-src {origin} data:; connect-src {origin}; "
                            f"form-action 'none'; frame-ancestors 'none'; base-uri {origin}"
                        )
                else:
                    csp = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
                headers.extend([
                    (b"content-security-policy", csp.encode("ascii")),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
                ])
                message["headers"] = headers
            await send(message)
        await self.app(scope, receive, secured)


class ProxyCapabilityStore:
    def __init__(self, ttl_seconds: float = 120.0, max_active: int = 256) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_active = max_active
        self._entries: OrderedDict[str, tuple[str, str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        expired = [digest for digest, (_, _, expiry) in self._entries.items() if expiry <= now]
        for digest in expired:
            self._entries.pop(digest, None)

    def issue(
        self, webui_id: str, session_hash: str, session_expires_at: str | None = None,
    ) -> str:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = time.monotonic()
        ttl = self.ttl_seconds
        if session_expires_at is not None:
            try:
                session_remaining = (
                    datetime.fromisoformat(session_expires_at) - datetime.now(timezone.utc)
                ).total_seconds()
            except (TypeError, ValueError) as exc:
                raise IntegrationError(403, "invalid_proxy_capability", "只读资源授权无效或已过期") from exc
            ttl = min(ttl, max(0.0, session_remaining))
        with self._lock:
            self._purge(now)
            while len(self._entries) >= self.max_active:
                self._entries.popitem(last=False)
            self._entries[digest] = (webui_id, session_hash, now + ttl)
        return token

    def validate(self, token: str, webui_id: str) -> str:
        if not token or len(token) > 128:
            raise IntegrationError(403, "invalid_proxy_capability", "只读资源授权无效或已过期")
        digest = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            entry = self._entries.get(digest)
            if entry is None or entry[0] != webui_id:
                raise IntegrationError(403, "invalid_proxy_capability", "只读资源授权无效或已过期")
            return entry[1]

    def revoke_session(self, session_hash: str) -> None:
        with self._lock:
            doomed = [digest for digest, (_, owner, _) in self._entries.items() if owner == session_hash]
            for digest in doomed:
                self._entries.pop(digest, None)


def _services(snapshot: dict[str, Any]) -> dict[str, Any]:
    containers = snapshot.get("docker", {}).get("containers", [])
    ports = snapshot.get("ports", [])
    return {
        "sampled_at": snapshot.get("sampled_at"),
        "containers": containers,
        "listening_ports": [item for item in ports if item.get("listening")],
        "all_critical_ports": ports,
        "collector_errors": [
            error for error in snapshot.get("collector_errors", [])
            if error.get("collector") in {"docker", "ports"}
        ],
    }


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    # detail 保留 0.1 API 的结构化字段；新客户端统一使用 error。
    return {"error": error, "detail": {"error_type": code, "message": message, "cause": details}}


async def _await_log_cleanup(task: asyncio.Task[Any], timeout: float = 2.0) -> Any:
    return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


def _failed_scan(directory: Path, exc: Exception) -> dict[str, Any]:
    try:
        directory_exists = directory.exists()
    except OSError:
        directory_exists = False
    sanitized_cause, _ = redact_sensitive_text(str(exc))
    return {
        "scan_id": uuid.uuid4().hex,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "directory": str(directory.absolute()),
        "directory_exists": directory_exists,
        "entries": [],
        "errors": [
            {"error_type": type(exc).__name__, "message": "脚本扫描失败", "cause": sanitized_cause}
        ],
    }


def create_app(
    settings: Settings | None = None,
    sampler: Sampler | None = None,
    database: Database | None = None,
    discovery: ScriptDiscovery | None = None,
    control_plane: ControlPlane | None = None,
    webui_service: WebUIService | None = None,
    log_service: LogService | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()
    resolved_sampler = sampler or Sampler(resolved_settings)
    resolved_database = database or Database(
        resolved_settings.database_path,
        audit_retention_max_events=resolved_settings.audit_retention_max_events,
        audit_retention_days=resolved_settings.audit_retention_days,
        login_failure_max_rows=resolved_settings.login_failure_max_rows,
        operation_retention_max=resolved_settings.operation_retention_max,
    )
    resolved_auth = AuthService(
        resolved_database,
        resolved_settings.session_ttl_seconds,
        resolved_settings.session_max_active,
    )
    proxy_capabilities = ProxyCapabilityStore()
    auth_concurrency = asyncio.Semaphore(resolved_settings.auth_concurrency_limit)
    bind_is_loopback = resolved_settings.host.lower() == "localhost" or is_loopback(
        resolved_settings.host
    )
    if not bind_is_loopback and not resolved_auth.is_setup():
        raise ConfigError("管理员未初始化时只允许绑定 loopback 地址")
    resolved_discovery = discovery or ScriptDiscovery(
        resolved_settings.discovery_scripts_path,
        resolved_settings.command_timeout_seconds,
        max_file_bytes=resolved_settings.discovery_max_file_bytes,
        max_entries=resolved_settings.discovery_max_entries,
        max_shortcuts=resolved_settings.discovery_max_shortcuts,
        total_timeout_seconds=resolved_settings.discovery_total_timeout_seconds,
    )
    control_config_error: dict[str, Any] | None = None
    if control_plane is None:
        try:
            resolved_control = ControlPlane(
                load_control_config(resolved_settings.control_config_path), resolved_database
            )
        except ControlConfigError as exc:
            control_config_error = {
                "code": exc.code, "message": exc.message, "details": exc.details
            }
            resolved_control = ControlPlane(
                ControlConfig(source="example_preview"), resolved_database
            )
    else:
        resolved_control = control_plane
    integration_config_error: dict[str, str] | None = None
    try:
        resolved_integrations = load_integrations_config(resolved_settings.integrations_config_path)
    except IntegrationConfigError as exc:
        integration_config_error = {"code": exc.code, "message": exc.message}
        resolved_integrations = IntegrationsConfig(
            source="invalid", blockers=("integrations 正式配置无效，全部集成已禁用",)
        )
    resolved_webuis = webui_service or WebUIService(resolved_integrations)
    resolved_logs = log_service or LogService(
        resolved_integrations, resolved_settings.manager_log_path,
        timeout=resolved_settings.command_timeout_seconds,
    )
    manager_logger = configure_manager_logging(
        resolved_settings.manager_log_path, resolved_settings.manager_log_level,
        resolved_settings.manager_log_max_bytes, resolved_settings.manager_log_backup_count,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await resolved_sampler.sample_once()
        resolved_sampler.start()
        if resolved_settings.scan_scripts_on_startup:
            try:
                result = await asyncio.to_thread(resolved_discovery.scan)
            except Exception as exc:
                sanitized_cause, _ = redact_sensitive_text(str(exc))
                app.state.startup_discovery_error = {
                    "error_type": type(exc).__name__, "message": "启动时脚本扫描失败", "cause": sanitized_cause
                }
                try:
                    resolved_database.record_failed_scan_with_audit(
                        _failed_scan(resolved_settings.discovery_scripts_path, exc),
                        "startup",
                        type(exc).__name__,
                    )
                except DatabaseError as audit_exc:
                    sanitized_audit_cause, _ = redact_sensitive_text(str(audit_exc))
                    app.state.startup_audit_error = {
                        "error_type": type(audit_exc).__name__,
                        "message": "启动扫描失败且无法写入元数据/审计", "cause": sanitized_audit_cause,
                    }
            else:
                try:
                    error_count = len(result.get("errors", [])) + sum(
                        len(entry.get("errors", [])) for entry in result["entries"]
                    )
                    resolved_database.replace_discovered_with_audit(
                        result, "startup", "partial" if error_count else "success",
                        {"entry_count": len(result["entries"]), "entry_error_count": error_count,
                         "directory_exists": result["directory_exists"]},
                    )
                    app.state.last_discovery = result
                # 启动发现是可选边界：持久化失败留存但不阻断应用。
                except Exception as exc:
                    sanitized_cause, _ = redact_sensitive_text(str(exc))
                    app.state.startup_discovery_error = {
                        "error_type": type(exc).__name__, "message": "启动时脚本扫描持久化失败", "cause": sanitized_cause
                    }
        try:
            yield
        finally:
            await resolved_control.shutdown()
            await resolved_sampler.stop()
            for handler in list(manager_logger.handlers):
                manager_logger.removeHandler(handler)
                handler.close()

    app = FastAPI(title="AXIS AI 工作站管理器", version=__version__, lifespan=lifespan)
    app.add_middleware(
        RequestBodyLimitMiddleware, max_bytes=resolved_settings.request_body_max_bytes
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        allowed_public_origins=resolved_settings.allowed_public_origins,
        trusted_proxy_ips=resolved_settings.trusted_proxy_ips,
    )
    app.state.settings = resolved_settings
    app.state.sampler = resolved_sampler
    app.state.database = resolved_database
    app.state.auth = resolved_auth
    app.state.discovery = resolved_discovery
    app.state.last_discovery = None
    app.state.startup_discovery_error = None
    app.state.startup_audit_error = None
    app.state.control = resolved_control
    app.state.control_config_error = control_config_error
    app.state.integrations = resolved_integrations
    app.state.integration_config_error = integration_config_error
    app.state.webuis = resolved_webuis
    app.state.proxy_capabilities = proxy_capabilities
    app.state.logs = resolved_logs
    app.state.manager_logger = manager_logger
    manager_logger.info("manager application initialized")

    @app.exception_handler(AuthError)
    async def auth_error_handler(_: Request, exc: AuthError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Cookie"} if exc.status_code == 401 else None
        return JSONResponse(_error_body(exc.code, exc.message), status_code=exc.status_code, headers=headers)

    @app.exception_handler(ControlError)
    async def control_error_handler(_: Request, exc: ControlError) -> JSONResponse:
        return JSONResponse(
            _error_body(exc.code, exc.message, exc.details), status_code=exc.status_code
        )

    @app.exception_handler(IntegrationError)
    async def integration_error_handler(_: Request, exc: IntegrationError) -> JSONResponse:
        return JSONResponse(_error_body(exc.code, exc.message), status_code=exc.status_code)

    @app.exception_handler(DatabaseError)
    async def database_error_handler(_: Request, exc: DatabaseError) -> JSONResponse:
        sanitized_cause, _ = redact_sensitive_text(str(exc))
        return JSONResponse(
            _error_body("database_error", "持久化操作失败", sanitized_cause), status_code=500
        )

    @app.exception_handler(DiscoveryError)
    async def discovery_error_handler(_: Request, exc: DiscoveryError) -> JSONResponse:
        sanitized_cause, _ = redact_sensitive_text(str(exc))
        return JSONResponse(
            _error_body("discovery_error", "脚本扫描失败", sanitized_cause),
            status_code=500,
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            code = str(exc.detail.get("error_type", "http_error"))
            message = str(exc.detail.get("message", "请求失败"))
            details = exc.detail.get("cause")
        else:
            code, message, details = "http_error", str(exc.detail), None
        return JSONResponse(_error_body(code, message, details), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        auth_event = {
            "/api/v1/auth/setup": "auth.setup",
            "/api/v1/auth/login": "auth.login",
        }.get(request.url.path)
        if auth_event:
            resolved_database.append_audit(
                _client_ip(request), auth_event, "failure", {"reason": "validation_error"}
            )
        details = [
            {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return JSONResponse(_error_body("validation_error", "请求参数无效", details), status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        manager_logger.error("unhandled application error: %s", type(exc).__name__)
        return JSONResponse(
            _error_body("internal_error", "服务器内部错误", {"error_type": type(exc).__name__}),
            status_code=500,
        )

    async def protected_access(request: Request) -> AuthenticatedSession | None:
        if not resolved_auth.is_setup():
            if is_loopback(_client_ip(request)):
                return None
            raise AuthError(403, "setup_required", "管理员未初始化，仅允许本机访问")
        return resolved_auth.authenticate(request.cookies.get(SESSION_COOKIE))

    async def require_session(request: Request) -> AuthenticatedSession:
        session = await protected_access(request)
        if session is None:
            raise AuthError(401, "authentication_required", "需要先完成管理员设置并登录")
        return session

    async def require_csrf(
        request: Request,
        session: AuthenticatedSession = Depends(require_session),
        csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),
    ) -> AuthenticatedSession:
        resolved_auth.verify_csrf(session, csrf_token)
        return session

    def validate_proxy_boundary(request: Request) -> None:
        try:
            peer_ip = ipaddress.ip_address(_client_ip(request).split("%", 1)[0]).compressed
        except ValueError:
            peer_ip = ""
        trusted_proxy = peer_ip in resolved_settings.trusted_proxy_ips
        forwarded = any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"))
        if forwarded and not trusted_proxy:
            raise IntegrationError(400, "invalid_proxy_boundary", "代理请求来源无效")
        host_header = request.headers.get("host", "").lower()
        testclient_host = host_header.lower() == "testserver" and "http.response.debug" in request.scope.get("extensions", {})
        try:
            parsed_host = urlsplit("//" + host_header)
            host_ip = ipaddress.ip_address((parsed_host.hostname or "").split("%", 1)[0])
            valid_host = parsed_host.port == resolved_settings.port and (host_ip.is_loopback or host_ip.is_private)
        except (ValueError, TypeError):
            valid_host = host_header.lower() == f"localhost:{resolved_settings.port}"
        public_origin = next(
            (candidate for candidate in resolved_settings.allowed_public_origins if urlsplit(candidate).netloc == host_header),
            None,
        )
        public_proxy = trusted_proxy and public_origin is not None
        if not (valid_host or public_proxy or testclient_host):
            raise IntegrationError(400, "invalid_proxy_boundary", "代理请求来源无效")
        origin = request.headers.get("origin")
        if origin and origin != "null":
            parsed_origin = urlsplit(origin)
            normalized_origin = f"{parsed_origin.scheme}://{parsed_origin.netloc.lower()}"
            if parsed_origin.scheme not in {"http", "https"} or parsed_origin.path not in {"", "/"} or parsed_origin.query or parsed_origin.fragment:
                raise IntegrationError(403, "invalid_proxy_origin", "代理请求来源无效")
            if public_proxy:
                origin_valid = normalized_origin == public_origin
            else:
                origin_valid = parsed_origin.scheme == request.url.scheme and parsed_origin.netloc.lower() == host_header
            if not origin_valid:
                raise IntegrationError(403, "invalid_proxy_origin", "代理请求来源无效")

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE, token, max_age=resolved_settings.session_ttl_seconds,
            httponly=True, secure=resolved_settings.cookie_secure, samesite="strict", path="/",
        )

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        sampler_running = resolved_sampler._task is not None and not resolved_sampler._task.done()
        collector_errors = resolved_sampler.current.get("collector_errors", []) if resolved_sampler.current else []
        healthy = sampler_running and resolved_sampler.last_error is None and not collector_errors
        return {
            "version": __version__,
            "schema": {"api": "v1", "integrations": 1, "database": DATABASE_SCHEMA_VERSION},
            "status": "healthy" if healthy else "degraded",
            "read_only": not resolved_control.config.control_enabled,
            "control_enabled": resolved_control.config.control_enabled,
            "sampler_running": sampler_running,
            "collector_errors": [
                {"collector": item.get("collector"), "error_type": item.get("error_type"), "message": item.get("message")}
                for item in collector_errors
            ],
            "sampled_at": resolved_sampler.current.get("sampled_at") if resolved_sampler.current else None,
            "readiness": {
                "setup_complete": resolved_auth.is_setup(),
                "sampler": "ready" if sampler_running else "not_ready",
                "integrations": "ready" if integration_config_error is None else "disabled",
            },
        }

    @app.get("/api/v1/auth/status")
    async def auth_status(request: Request) -> dict[str, bool]:
        configured = resolved_auth.is_setup()
        authenticated = False
        if configured and request.cookies.get(SESSION_COOKIE):
            try:
                resolved_auth.authenticate(request.cookies.get(SESSION_COOKIE))
                authenticated = True
            except AuthError:
                authenticated = False
        return {"configured": configured, "setup_required": not configured, "authenticated": authenticated}

    @app.post("/api/v1/auth/setup", status_code=201)
    async def auth_setup(credentials: Credentials, request: Request, response: Response) -> dict[str, Any]:
        if resolved_settings.setup_disabled:
            raise AuthError(403, "setup_disabled", "首次设置已被部署配置禁用")
        if any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")):
            raise AuthError(403, "direct_loopback_required", "首次设置必须直连本机 loopback 地址")
        host = request.headers.get("host", "").lower()
        allowed_hosts = {f"127.0.0.1:{resolved_settings.port}", f"localhost:{resolved_settings.port}", f"[::1]:{resolved_settings.port}"}
        testclient_host = host == "testserver" and "http.response.debug" in request.scope.get("extensions", {})
        if host not in allowed_hosts and not testclient_host:
            raise AuthError(403, "direct_loopback_required", "首次设置必须直连本机 loopback 地址")
        async with auth_concurrency:
            token, csrf, expires_at = await asyncio.to_thread(
                resolved_auth.setup,
                credentials.username,
                credentials.password,
                _client_ip(request),
            )
        set_session_cookie(response, token)
        return {"authenticated": True, "csrf_token": csrf, "expires_at": expires_at}

    @app.post("/api/v1/auth/login")
    async def auth_login(credentials: Credentials, request: Request, response: Response) -> dict[str, Any]:
        async with auth_concurrency:
            token, csrf, expires_at = await asyncio.to_thread(
                resolved_auth.login,
                credentials.username,
                credentials.password,
                _client_ip(request),
            )
        set_session_cookie(response, token)
        return {"authenticated": True, "csrf_token": csrf, "expires_at": expires_at}

    @app.post("/api/v1/auth/logout")
    async def auth_logout(
        request: Request, response: Response,
        session: AuthenticatedSession = Depends(require_csrf),
    ) -> dict[str, bool]:
        proxy_capabilities.revoke_session(session.token_hash)
        resolved_auth.logout(session, _client_ip(request))
        response.delete_cookie(
            SESSION_COOKIE, path="/", httponly=True,
            secure=resolved_settings.cookie_secure, samesite="strict",
        )
        return {"authenticated": False}

    @app.get("/api/v1/auth/me")
    async def auth_me(session: AuthenticatedSession = Depends(require_session)) -> dict[str, Any]:
        csrf = resolved_auth.rotate_csrf(session)
        return {
            "username": session.username,
            "expires_at": session.expires_at,
            "csrf_token": csrf,
        }

    @app.get("/api/v1/snapshot", dependencies=[Depends(protected_access)])
    async def snapshot() -> dict[str, Any]:
        return resolved_sampler.current if resolved_sampler.current is not None else await resolved_sampler.sample_once()

    @app.get("/api/v1/history", dependencies=[Depends(protected_access)])
    async def history(window: str = Query(default="15m")) -> dict[str, Any]:
        try:
            minutes = parse_window(window)
        except ValueError as exc:
            raise HTTPException(422, {"error_type": type(exc).__name__, "message": "无效的历史窗口", "cause": str(exc)}) from exc
        return {"window": f"{minutes}m", "samples": resolved_sampler.history.query(minutes)}

    @app.get("/api/v1/services", dependencies=[Depends(protected_access)])
    async def services() -> dict[str, Any]:
        current = resolved_sampler.current or await resolved_sampler.sample_once()
        return _services(current)

    @app.get("/api/v1/discovery/scripts")
    async def discovered_scripts(
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        return {
            "directory": redact_sensitive_text(
                str(resolved_settings.discovery_scripts_path.absolute())
            )[0],
            "entries": resolved_database.list_discovered(),
            "latest_scan": resolved_database.latest_scan_run(),
            "startup_error": app.state.startup_discovery_error,
        }

    @app.post("/api/v1/discovery/scripts/scan")
    async def scan_scripts(
        request: Request, _: AuthenticatedSession = Depends(require_csrf)
    ) -> dict[str, Any]:
        source_ip = _client_ip(request)
        try:
            result = await asyncio.to_thread(resolved_discovery.scan)
        except Exception as exc:
            resolved_database.record_failed_scan_with_audit(
                _failed_scan(resolved_settings.discovery_scripts_path, exc),
                source_ip,
                type(exc).__name__,
            )
            raise
        error_count = len(result.get("errors", [])) + sum(
            len(entry.get("errors", [])) for entry in result["entries"]
        )
        resolved_database.replace_discovered_with_audit(
            result, source_ip, "partial" if error_count else "success",
            {"entry_count": len(result["entries"]), "entry_error_count": error_count,
             "directory_exists": result["directory_exists"]},
        )
        app.state.last_discovery = result
        return result

    @app.get("/api/v1/audit")
    async def audit_events(
        limit: int = Query(default=100, ge=1, le=200),
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        return {"events": resolved_database.list_audit(limit), "limit": limit}

    @app.get("/api/v1/webuis")
    async def webuis(
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        result = await resolved_webuis.list_status()
        result["config_error"] = integration_config_error
        return result

    @app.api_route(
        "/proxy/webui/{webui_id}/", methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    @app.api_route(
        "/proxy/webui/{webui_id}/{proxy_path:path}", methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def proxy_webui(
        webui_id: str, request: Request, proxy_path: str = "",
        session: AuthenticatedSession = Depends(require_session),
    ) -> Response:
        validate_proxy_boundary(request)
        path_digest = hashlib.sha256(proxy_path.encode("utf-8", "replace")).hexdigest()[:16]
        capability = proxy_capabilities.issue(
            webui_id, session.token_hash, session.expires_at,
        )
        asset_prefix = f"/proxy-asset/{capability}/{webui_id}"
        try:
            status, response_headers, content = await resolved_webuis.proxy(
                webui_id, proxy_path, request.method, request.scope.get("raw_path"),
                request.scope.get("query_string", b""), request.headers, b"",
                proxy_prefix=asset_prefix,
            )
        except Exception as exc:
            resolved_database.append_audit(_client_ip(request), "webui.proxy", "failure", {"webui_id": webui_id, "method": request.method, "path_digest": path_digest, "error_type": type(exc).__name__})
            raise
        resolved_database.append_audit(_client_ip(request), "webui.proxy", "success", {"webui_id": webui_id, "method": request.method, "path_digest": path_digest, "status": status})
        response = Response(content=content if request.method != "HEAD" else b"", status_code=status)
        response.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in response_headers
        ]
        return response

    @app.api_route(
        "/proxy-asset/{capability}/{webui_id}/", methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    @app.api_route(
        "/proxy-asset/{capability}/{webui_id}/{proxy_path:path}", methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def proxy_webui_asset(
        capability: str, webui_id: str, request: Request, proxy_path: str = "",
    ) -> Response:
        validate_proxy_boundary(request)
        path_digest = hashlib.sha256(proxy_path.encode("utf-8", "replace")).hexdigest()[:16]
        try:
            owner_session_hash = proxy_capabilities.validate(capability, webui_id)
            if not resolved_auth.is_session_hash_active(owner_session_hash):
                proxy_capabilities.revoke_session(owner_session_hash)
                raise IntegrationError(403, "invalid_proxy_capability", "只读资源授权无效或已过期")
            asset_prefix = f"/proxy-asset/{capability}/{webui_id}"
            status, response_headers, content = await resolved_webuis.proxy(
                webui_id, proxy_path, request.method, request.scope.get("raw_path"),
                request.scope.get("query_string", b""), request.headers, b"",
                proxy_prefix=asset_prefix,
            )
        except Exception as exc:
            resolved_database.append_audit(
                _client_ip(request), "webui.proxy_asset", "failure",
                {"webui_id": webui_id, "method": request.method, "path_digest": path_digest, "error_type": type(exc).__name__},
            )
            raise
        resolved_database.append_audit(
            _client_ip(request), "webui.proxy_asset", "success",
            {"webui_id": webui_id, "method": request.method, "path_digest": path_digest, "status": status},
        )
        response = Response(content=content if request.method != "HEAD" else b"", status_code=status)
        response.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in response_headers
        ]
        return response

    @app.get("/api/v1/log-sources")
    async def log_sources(
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        result = resolved_logs.list_sources()
        result["config_error"] = integration_config_error
        return result

    @app.get("/api/v1/log-sources/{source_id}/entries")
    async def log_entries(
        source_id: str, request: Request,
        lines: int = Query(default=200), since: str = Query(default="1h"),
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        cancel_event = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(resolved_logs.entries, source_id, lines, since, cancel_event)
        )
        cancellation_audited = False

        async def finish_cancelled_read(reason: str) -> None:
            nonlocal cancellation_audited
            cancel_event.set()
            cleanup = "complete"
            cleanup_error: str | None = None
            cleanup_error_code: str | None = None
            cleanup_exception: BaseException | None = None
            try:
                await _await_log_cleanup(task)
            except IntegrationError as exc:
                if exc.code != "log_read_cancelled":
                    cleanup, cleanup_error = "error", type(exc).__name__
                    cleanup_error_code = exc.code
                    cleanup_exception = exc
            except asyncio.TimeoutError as exc:
                cleanup, cleanup_error = "timeout", "TimeoutError"
                cleanup_exception = exc
            except Exception as exc:
                cleanup, cleanup_error = "error", type(exc).__name__
                cleanup_exception = exc
            audit_reason = reason if cleanup == "complete" else "cancelled_cleanup_failed"
            summary = {"source_id": source_id, "reason": audit_reason, "requested_reason": reason, "cleanup": cleanup}
            if cleanup_error is not None:
                summary["cleanup_error_type"] = cleanup_error
            if cleanup_error_code is not None:
                summary["cleanup_error_code"] = cleanup_error_code
            if cleanup_error is None:
                manager_logger.warning("log source read cancelled and cleaned up")
            elif cleanup_error_code is not None:
                manager_logger.error(
                    "cancelled log source cleanup failed: %s",
                    cleanup_error_code,
                    exc_info=(type(cleanup_exception), cleanup_exception, cleanup_exception.__traceback__),
                )
            elif cleanup == "timeout":
                manager_logger.error(
                    "cancelled log source cleanup timed out",
                    exc_info=(type(cleanup_exception), cleanup_exception, cleanup_exception.__traceback__),
                )
            else:
                manager_logger.error(
                    "cancelled log source cleanup failed: %s",
                    cleanup_error,
                    exc_info=(type(cleanup_exception), cleanup_exception, cleanup_exception.__traceback__),
                )
            resolved_database.append_audit(_client_ip(request), "logs.read", "failure", summary)
            cancellation_audited = True

        async def wait_for_disconnect() -> None:
            while not task.done():
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.05)

        disconnect_task = asyncio.create_task(wait_for_disconnect())
        try:
            done, _ = await asyncio.wait({task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
            if disconnect_task in done and not task.done():
                await finish_cancelled_read("client_disconnected")
                raise asyncio.CancelledError
            result = await task
        except asyncio.CancelledError:
            if not cancellation_audited:
                await finish_cancelled_read("cancelled")
            raise
        except Exception as exc:
            resolved_database.append_audit(
                _client_ip(request), "logs.read", "failure",
                {"source_id": source_id, "error_type": type(exc).__name__},
            )
            manager_logger.warning("log source read failed: %s", type(exc).__name__)
            raise
        finally:
            if not disconnect_task.done():
                disconnect_task.cancel()
            try:
                await disconnect_task
            except asyncio.CancelledError:
                pass
        resolved_database.append_audit(
            _client_ip(request), "logs.read", "success", {"source_id": source_id}
        )
        return result

    @app.get("/api/v1/environments")
    async def environments(
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        result = await resolved_control.list_environments()
        result["config_error"] = app.state.control_config_error
        return result

    @app.post("/api/v1/environments/{environment_id}/preflight")
    async def environment_preflight(
        environment_id: str,
        action: str | None = Query(default=None, pattern=r"^(start|stop|restart)$"),
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        return await resolved_control.environment_preflight(environment_id, action)

    @app.post("/api/v1/environments/{environment_id}/actions", status_code=202)
    async def environment_action(
        environment_id: str, payload: EnvironmentActionRequest, request: Request,
        session: AuthenticatedSession = Depends(require_csrf),
    ) -> dict[str, Any]:
        operation_id = await resolved_control.submit_environment(
            environment_id, payload.action, payload.confirmation,
            session.username, _client_ip(request),
        )
        return {"operation_id": operation_id, "status": "queued"}

    @app.post("/api/v1/control/recovery/preflight")
    async def control_recovery_preflight(
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        return await resolved_control.recovery_preflight()

    @app.post("/api/v1/control/recovery/resolve")
    async def control_recovery_resolve(
        payload: RecoveryResolveRequest, request: Request,
        session: AuthenticatedSession = Depends(require_csrf),
    ) -> dict[str, Any]:
        await resolved_control.resolve_recovery(
            payload.confirmation, session.username, _client_ip(request))
        return {"resolved": True}

    @app.get("/api/v1/operations/{operation_id}")
    async def operation(
        operation_id: str, _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
            raise ControlError(404, "operation_not_found", "操作任务不存在")
        item = resolved_database.get_operation(operation_id)
        if item is None:
            raise ControlError(404, "operation_not_found", "操作任务不存在")
        return item

    @app.get("/api/v1/operations")
    async def operations(
        limit: int = Query(default=50, ge=1, le=200),
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        return {"operations": resolved_database.list_operations(limit), "limit": limit}

    @app.get("/api/v1/scenes")
    async def scenes(
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        result = await resolved_control.list_scenes()
        result["config_error"] = app.state.control_config_error
        return result

    @app.post("/api/v1/scenes/{scene_id}/preflight")
    async def scene_preflight(
        scene_id: str, _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        return await resolved_control.scene_preflight(scene_id)

    @app.post("/api/v1/scenes/{scene_id}/activate", status_code=202)
    async def scene_activate(
        scene_id: str, payload: SceneActivateRequest, request: Request,
        session: AuthenticatedSession = Depends(require_csrf),
    ) -> dict[str, Any]:
        operation_id = await resolved_control.submit_scene(
            scene_id, payload.confirmation, session.username, _client_ip(request)
        )
        return {"operation_id": operation_id, "status": "queued"}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(PROJECT_ROOT / "index.html", media_type="text/html")

    @app.get("/styles.css", include_in_schema=False)
    async def styles() -> FileResponse:
        return FileResponse(PROJECT_ROOT / "styles.css", media_type="text/css")

    @app.get("/app.js", include_in_schema=False)
    async def javascript() -> FileResponse:
        return FileResponse(PROJECT_ROOT / "app.js", media_type="text/javascript")

    @app.get("/request-guard.js", include_in_schema=False)
    async def request_guard_javascript() -> FileResponse:
        return FileResponse(PROJECT_ROOT / "request-guard.js", media_type="text/javascript")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app
