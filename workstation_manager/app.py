from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .auth import CSRF_HEADER, SESSION_COOKIE, AuthenticatedSession, AuthError, AuthService, is_loopback
from .config import ConfigError, Settings, load_settings
from .database import SCHEMA_VERSION as DATABASE_SCHEMA_VERSION, Database, DatabaseError
from .history import Sampler, parse_window
from .manager_logging import configure_manager_logging
from .registry import RegisteredServiceManager, RegistryError, ScriptRunner


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class ServicePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    script_path: str = Field(min_length=1, max_length=2048)
    gpu_label: str = Field(default="", max_length=100)
    port: int | None = Field(default=None, ge=1, le=65535)
    ui_url: str = Field(default="", max_length=2048)


class ScenePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    service_ids: list[str] = Field(default_factory=list, max_length=1000)


class ServiceActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern=r"^(start|stop|restart)$")


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
                    response = JSONResponse(_error_body("request_body_too_large", "请求体超过限制"), 413)
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
                result = messages[index]
                index += 1
                return result
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, **_: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                ])
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secured)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return body


def _host_services(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {"containers": snapshot.get("containers", snapshot.get("docker", {}).get("containers", [])),
            "listening_ports": snapshot.get("listening_ports", snapshot.get("ports", [])),
            "sampled_at": snapshot.get("sampled_at")}


def create_app(settings: Settings | None = None, sampler: Sampler | None = None,
               database: Database | None = None,
               registry_manager: RegisteredServiceManager | None = None) -> FastAPI:
    resolved_settings = settings or load_settings()
    resolved_sampler = sampler or Sampler(resolved_settings)
    resolved_database = database or Database(
        resolved_settings.database_path,
        audit_retention_max_events=resolved_settings.audit_retention_max_events,
        audit_retention_days=resolved_settings.audit_retention_days,
        login_failure_max_rows=resolved_settings.login_failure_max_rows,
        operation_retention_max=resolved_settings.operation_retention_max,
    )
    resolved_auth = AuthService(resolved_database, resolved_settings.session_ttl_seconds,
                                resolved_settings.session_max_active)
    resolved_registry = registry_manager or RegisteredServiceManager(
        resolved_database,
        ScriptRunner(resolved_settings.script_action_timeout_seconds,
                     resolved_settings.script_status_timeout_seconds),
        resolved_settings.service_status_interval_seconds,
    )
    auth_concurrency = asyncio.Semaphore(resolved_settings.auth_concurrency_limit)
    if resolved_settings.host.lower() != "localhost" and not is_loopback(resolved_settings.host) \
            and not resolved_auth.is_setup():
        raise ConfigError("管理员未初始化时只允许绑定 loopback 地址")
    manager_logger = configure_manager_logging(
        resolved_settings.manager_log_path, resolved_settings.manager_log_level,
        resolved_settings.manager_log_max_bytes, resolved_settings.manager_log_backup_count,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await resolved_sampler.sample_once()
        resolved_sampler.start()
        try:
            await resolved_registry.start()
            yield
        finally:
            try:
                await resolved_registry.shutdown()
            finally:
                try:
                    await resolved_sampler.stop()
                finally:
                    for handler in list(manager_logger.handlers):
                        manager_logger.removeHandler(handler)
                        handler.close()

    app = FastAPI(title="AXIS AI 工作站管理器", version=__version__, lifespan=lifespan)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=resolved_settings.request_body_max_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.settings = resolved_settings
    app.state.sampler = resolved_sampler
    app.state.database = resolved_database
    app.state.auth = resolved_auth
    app.state.registry = resolved_registry
    app.state.manager_logger = manager_logger

    @app.exception_handler(AuthError)
    async def auth_error_handler(_: Request, exc: AuthError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Cookie"} if exc.status_code == 401 else None
        return JSONResponse(_error_body(exc.code, exc.message), exc.status_code, headers=headers)

    @app.exception_handler(RegistryError)
    async def registry_error_handler(_: Request, exc: RegistryError) -> JSONResponse:
        return JSONResponse(_error_body(exc.code, exc.message), exc.status_code)

    @app.exception_handler(DatabaseError)
    async def database_error_handler(_: Request, exc: DatabaseError) -> JSONResponse:
        return JSONResponse(_error_body("database_error", "持久化操作失败", str(exc)), 500)

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            return JSONResponse(_error_body(str(exc.detail.get("error_type", "http_error")),
                                                  str(exc.detail.get("message", "请求失败")),
                                                  exc.detail.get("cause")),
                                exc.status_code, headers=exc.headers)
        return JSONResponse(_error_body("http_error", str(exc.detail)), exc.status_code,
                            headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        auth_event = {"/api/v1/auth/setup": "auth.setup", "/api/v1/auth/login": "auth.login"}.get(
            request.url.path
        )
        if auth_event:
            resolved_database.append_audit(_client_ip(request), auth_event, "failure",
                                           {"reason": "validation_error"})
        details = [{"location": list(error["loc"]), "message": error["msg"],
                    "type": error["type"]} for error in exc.errors()]
        return JSONResponse(_error_body("validation_error", "请求参数无效", details), 422)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        manager_logger.exception("unhandled application error")
        return JSONResponse(_error_body("internal_error", "服务器内部错误",
                                        {"error_type": type(exc).__name__}), 500)

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

    async def require_csrf(session: AuthenticatedSession = Depends(require_session),
                           csrf_token: str | None = Header(default=None, alias=CSRF_HEADER)) \
            -> AuthenticatedSession:
        resolved_auth.verify_csrf(session, csrf_token)
        return session

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(SESSION_COOKIE, token, max_age=resolved_settings.session_ttl_seconds,
                            httponly=True, secure=resolved_settings.cookie_secure,
                            samesite="strict", path="/")

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        sampler_running = resolved_sampler._task is not None and not resolved_sampler._task.done()
        collector_errors = resolved_sampler.current.get("collector_errors", []) \
            if resolved_sampler.current else []
        manager_error = resolved_registry.last_poll_error
        operation_error = resolved_registry.last_operation_error
        return {"version": __version__, "schema": {"api": "v1", "database": DATABASE_SCHEMA_VERSION},
                "status": "healthy" if sampler_running and not collector_errors and not manager_error and not operation_error else "degraded",
                "sampler_running": sampler_running,
                "collector_errors": collector_errors,
                "service_status_error": manager_error,
                "service_operation_error": operation_error,
                "service_status_interval_seconds": resolved_settings.service_status_interval_seconds,
                "sampled_at": resolved_sampler.current.get("sampled_at")
                if resolved_sampler.current else None,
                "readiness": {"setup_complete": resolved_auth.is_setup(),
                              "sampler": "ready" if sampler_running else "not_ready",
                              "registered_services": "ready" if not manager_error and not operation_error else "degraded"}}

    @app.get("/api/v1/auth/status")
    async def auth_status(request: Request) -> dict[str, bool]:
        configured = resolved_auth.is_setup()
        authenticated = False
        if configured and request.cookies.get(SESSION_COOKIE):
            try:
                resolved_auth.authenticate(request.cookies.get(SESSION_COOKIE))
                authenticated = True
            except AuthError:
                pass
        return {"configured": configured, "setup_required": not configured,
                "authenticated": authenticated}

    @app.post("/api/v1/auth/setup", status_code=201)
    async def auth_setup(credentials: Credentials, request: Request, response: Response) -> dict[str, Any]:
        if resolved_settings.setup_disabled:
            raise AuthError(403, "setup_disabled", "首次设置已被部署配置禁用")
        if not is_loopback(_client_ip(request)):
            raise AuthError(403, "direct_loopback_required", "首次设置必须直连本机 loopback 地址")
        async with auth_concurrency:
            token, csrf, expires_at = await asyncio.to_thread(
                resolved_auth.setup, credentials.username, credentials.password, _client_ip(request)
            )
        set_session_cookie(response, token)
        return {"authenticated": True, "csrf_token": csrf, "expires_at": expires_at}

    @app.post("/api/v1/auth/login")
    async def auth_login(credentials: Credentials, request: Request, response: Response) -> dict[str, Any]:
        async with auth_concurrency:
            token, csrf, expires_at = await asyncio.to_thread(
                resolved_auth.login, credentials.username, credentials.password, _client_ip(request)
            )
        set_session_cookie(response, token)
        return {"authenticated": True, "csrf_token": csrf, "expires_at": expires_at}

    @app.post("/api/v1/auth/logout")
    async def auth_logout(request: Request, response: Response,
                          session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, bool]:
        resolved_auth.logout(session, _client_ip(request))
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True,
                               secure=resolved_settings.cookie_secure, samesite="strict")
        return {"authenticated": False}

    @app.get("/api/v1/auth/me")
    async def auth_me(session: AuthenticatedSession = Depends(require_session)) -> dict[str, Any]:
        return {"username": session.username, "expires_at": session.expires_at,
                "csrf_token": resolved_auth.rotate_csrf(session)}

    @app.get("/api/v1/snapshot", dependencies=[Depends(protected_access)])
    async def snapshot() -> dict[str, Any]:
        return resolved_sampler.current if resolved_sampler.current is not None \
            else await resolved_sampler.sample_once()

    @app.get("/api/v1/history", dependencies=[Depends(protected_access)])
    async def history(window: str = Query(default="15m")) -> dict[str, Any]:
        try:
            minutes = parse_window(window)
        except ValueError as exc:
            raise HTTPException(422, {"error_type": type(exc).__name__,
                                      "message": "无效的历史窗口", "cause": str(exc)}) from exc
        return {"window": f"{minutes}m", "samples": resolved_sampler.history.query(minutes)}

    @app.get("/api/v1/host-services", dependencies=[Depends(protected_access)])
    async def host_services() -> dict[str, Any]:
        current = resolved_sampler.current or await resolved_sampler.sample_once()
        return _host_services(current)

    @app.get("/api/v1/registered-services")
    @app.get("/api/v1/services")
    async def registered_services(_: AuthenticatedSession = Depends(require_session)) -> dict[str, Any]:
        return {"services": resolved_registry.list_services(),
                "poll_interval_seconds": resolved_settings.service_status_interval_seconds}

    @app.post("/api/v1/registered-services", status_code=201)
    async def create_service(payload: ServicePayload, request: Request,
                             session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        return await resolved_registry.create_service(payload.model_dump(), session.username,
                                                      _client_ip(request))

    @app.put("/api/v1/registered-services/{service_id}")
    async def update_service(service_id: str, payload: ServicePayload, request: Request,
                             session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        return await resolved_registry.update_service(service_id, payload.model_dump(), session.username,
                                                      _client_ip(request))

    @app.delete("/api/v1/registered-services/{service_id}", status_code=204)
    async def delete_service(service_id: str, request: Request,
                             session: AuthenticatedSession = Depends(require_csrf)) -> Response:
        resolved_registry.delete_service(service_id, session.username, _client_ip(request))
        return Response(status_code=204)

    @app.post("/api/v1/registered-services/refresh")
    async def refresh_services(_: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        await resolved_registry.refresh_all_statuses()
        return {"services": resolved_registry.list_services()}

    @app.post("/api/v1/registered-services/{service_id}/actions", status_code=202)
    async def service_action(service_id: str, payload: ServiceActionPayload, request: Request,
                             session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, str]:
        operation_id = resolved_registry.submit_service_action(
            service_id, payload.action, session.username, _client_ip(request)
        )
        return {"operation_id": operation_id, "status": "queued"}

    @app.get("/api/v1/scenes")
    async def scenes(_: AuthenticatedSession = Depends(require_session)) -> dict[str, Any]:
        return {"scenes": resolved_registry.list_scenes()}

    @app.post("/api/v1/scenes", status_code=201)
    async def create_scene(payload: ScenePayload, request: Request,
                           session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        return resolved_registry.create_scene(payload.model_dump(), session.username,
                                              _client_ip(request))

    @app.put("/api/v1/scenes/{scene_id}")
    async def update_scene(scene_id: str, payload: ScenePayload, request: Request,
                           session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        return resolved_registry.update_scene(scene_id, payload.model_dump(), session.username,
                                              _client_ip(request))

    @app.delete("/api/v1/scenes/{scene_id}", status_code=204)
    async def delete_scene(scene_id: str, request: Request,
                           session: AuthenticatedSession = Depends(require_csrf)) -> Response:
        resolved_registry.delete_scene(scene_id, session.username, _client_ip(request))
        return Response(status_code=204)

    @app.post("/api/v1/scenes/{scene_id}/activate", status_code=202)
    async def activate_scene(scene_id: str, request: Request,
                             session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, str]:
        operation_id = resolved_registry.submit_scene_activation(
            scene_id, session.username, _client_ip(request)
        )
        return {"operation_id": operation_id, "status": "queued"}

    @app.get("/api/v1/operations/{operation_id}")
    async def operation(operation_id: str,
                        _: AuthenticatedSession = Depends(require_session)) -> dict[str, Any]:
        if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
            raise RegistryError(404, "operation_not_found", "操作记录不存在")
        item = resolved_database.get_operation(operation_id)
        if item is None:
            raise RegistryError(404, "operation_not_found", "操作记录不存在")
        return item

    @app.get("/api/v1/operations")
    async def operations(limit: int = Query(default=100, ge=1, le=500),
                         _: AuthenticatedSession = Depends(require_session)) -> dict[str, Any]:
        return {"operations": resolved_database.list_operations(limit), "limit": limit}

    @app.get("/api/v1/audit")
    async def audit(limit: int = Query(default=100, ge=1, le=500),
                    _: AuthenticatedSession = Depends(require_session)) -> dict[str, Any]:
        return {"events": resolved_database.list_audit(limit), "limit": limit}

    @app.get("/", include_in_schema=False)
    async def root() -> FileResponse:
        return FileResponse(PROJECT_ROOT / "index.html")

    @app.get("/styles.css", include_in_schema=False)
    async def stylesheet() -> FileResponse:
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
