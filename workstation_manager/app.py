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
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .auth import (
    CSRF_HEADER,
    REMEMBER_SESSION_TTL_SECONDS,
    SESSION_COOKIE,
    AuthenticatedSession,
    AuthError,
    AuthService,
    is_loopback,
)
from .config import ConfigError, Settings, load_settings
from .database import SCHEMA_VERSION as DATABASE_SCHEMA_VERSION, Database, DatabaseError
from .file_service import FileCatalog, FileServiceError
from .history import Sampler, parse_window
from .i18n import localize_error, localize_http_error
from .manager_logging import configure_manager_logging
from .registry import RegisteredServiceManager, RegistryError, ScriptRunner
from .video_jobs import VideoJobError, VideoJobManager


PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def _submit_default_scene_when_docker_ready(
    registry: RegisteredServiceManager,
    timeout_seconds: float = 300.0,
    retry_interval_seconds: float = 2.0,
) -> str | None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    first_attempt = True
    while True:
        if not first_attempt and asyncio.get_running_loop().time() >= deadline:
            raise RegistryError(
                503, "docker_handoff_timeout",
                "等待 Docker 登录会话交接结束超时，默认场景未启动",
            )
        first_attempt = False
        try:
            return registry.submit_default_scene_activation()
        except RegistryError as exc:
            if exc.code != "docker_handoff_busy":
                raise
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RegistryError(
                    503, "docker_handoff_timeout",
                    "等待 Docker 登录会话交接结束超时，默认场景未启动",
                ) from exc
            await asyncio.sleep(min(retry_interval_seconds, remaining))


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class LoginCredentials(Credentials):
    remember: bool = False


class PasswordPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=1024)


class ServicePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    script_path: str = Field(min_length=1, max_length=2048)
    gpu_label: str = Field(default="", max_length=100)
    port: int | None = Field(default=None, ge=1, le=65535)
    ui_url: str = Field(default="", max_length=2048)
    health_url: str = Field(default="", max_length=2048)
    health_expect: str = Field(default="", max_length=512)
    wsl_portproxy_enabled: bool = False
    wsl_distro: str = Field(default="Ubuntu-22.04", max_length=100)
    wsl_listen_address: str = Field(default="0.0.0.0", max_length=45)
    wsl_listen_port: int | None = Field(default=None, ge=1, le=65535)
    wsl_connect_port: int | None = Field(default=None, ge=1, le=65535)


class ScenePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    detailed_description: str | None = Field(default=None, max_length=8000)
    is_default_generation: bool = False
    service_ids: list[str] = Field(default_factory=list, max_length=1000)


class SceneOrderPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_ids: list[str] = Field(max_length=1000)


class ServiceActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern=r"^(start|stop|restart)$")


class VideoJobPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    workflow_path: str = Field(min_length=1, max_length=2048)
    workflow_file_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    output_path: str | None = Field(default=None, max_length=2048)
    scene_name: str | None = Field(default=None, max_length=100)
    callback_url: str = Field(min_length=1, max_length=2048)
    callback_directory: str | None = Field(default=None, max_length=2048)


class VideoBatchWorkflowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_path: str = Field(min_length=1, max_length=2048)
    workflow_file_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    output_path: str | None = Field(default=None, max_length=2048)


class VideoJobBatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    workflows: list[VideoBatchWorkflowPayload] = Field(min_length=1, max_length=100)
    scene_name: str | None = Field(default=None, max_length=100)
    callback_url: str = Field(min_length=1, max_length=2048)
    callback_directory: str | None = Field(default=None, max_length=2048)


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "POST" and scope.get("path") == "/api/v1/file-service/upload":
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
                    language_header = next((value.decode("latin-1") for name, value in scope.get("headers", [])
                                            if name.lower() == b"accept-language"), None)
                    message_text, language = localize_error(
                        "request_body_too_large", "请求体超过限制", language_header
                    )
                    response = JSONResponse(
                        _error_body("request_body_too_large", message_text), 413,
                        headers={"Content-Language": language},
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
                registry_manager: RegisteredServiceManager | None = None,
                video_job_manager: VideoJobManager | None = None) -> FastAPI:
    resolved_settings = settings or load_settings()
    resolved_database = database or Database(
        resolved_settings.database_path,
        audit_retention_max_events=resolved_settings.audit_retention_max_events,
        audit_retention_days=resolved_settings.audit_retention_days,
        login_failure_max_rows=resolved_settings.login_failure_max_rows,
        operation_retention_max=resolved_settings.operation_retention_max,
        resource_history_retention_minutes=resolved_settings.history_minutes,
    )
    resolved_sampler = sampler or Sampler(resolved_settings, isolated_collection=True)
    resolved_sampler.set_sample_sink(resolved_database.append_resource_sample)
    resolved_auth = AuthService(resolved_database, resolved_settings.session_ttl_seconds,
                                resolved_settings.session_max_active)
    resolved_registry = registry_manager or RegisteredServiceManager(
        resolved_database,
        ScriptRunner(resolved_settings.script_action_timeout_seconds,
                     resolved_settings.script_status_timeout_seconds),
    )
    resolved_video_jobs = video_job_manager or VideoJobManager(
        resolved_database, resolved_registry,
        comfyui_base_url=resolved_settings.comfyui_base_url,
        ninfer_base_url=resolved_settings.ninfer_base_url,
        ninfer_model_id=resolved_settings.ninfer_model_id,
        output_directory=resolved_settings.video_output_directory,
        shared_output_directory=resolved_settings.file_service_root,
        file_service_port=resolved_settings.file_service_port,
        poll_interval_seconds=resolved_settings.video_job_poll_interval_seconds,
        idle_timeout_seconds=resolved_settings.video_job_idle_timeout_seconds,
        scene_timeout_seconds=resolved_settings.video_job_scene_timeout_seconds,
        generation_timeout_seconds=resolved_settings.video_job_generation_timeout_seconds,
        resource_snapshot=lambda: resolved_sampler.current,
    )
    file_catalog = FileCatalog(resolved_settings.file_service_root)
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
        try:
            await resolved_sampler.sample_once()
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError) as exc:
            resolved_sampler.record_error(exc, "初始资源采样失败，将在后台周期重试")
        resolved_sampler.start()
        try:
            await resolved_registry.start()
            await _submit_default_scene_when_docker_ready(resolved_registry)
            await resolved_video_jobs.start()
            yield
        finally:
            try:
                await resolved_video_jobs.shutdown()
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
    app.state.video_jobs = resolved_video_jobs
    app.state.file_catalog = file_catalog
    app.state.manager_logger = manager_logger

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
        message, language = localize_error(exc.code, exc.message, request.headers.get("accept-language"))
        headers = {"Content-Language": language}
        if exc.status_code == 401:
            headers["WWW-Authenticate"] = "Cookie"
        return JSONResponse(_error_body(exc.code, message), exc.status_code, headers=headers)

    @app.exception_handler(RegistryError)
    async def registry_error_handler(request: Request, exc: RegistryError) -> JSONResponse:
        message, language = localize_error(exc.code, exc.message, request.headers.get("accept-language"))
        return JSONResponse(_error_body(exc.code, message), exc.status_code,
                            headers={"Content-Language": language})

    @app.exception_handler(DatabaseError)
    async def database_error_handler(request: Request, exc: DatabaseError) -> JSONResponse:
        message, language = localize_error(
            "database_error", "持久化操作失败", request.headers.get("accept-language")
        )
        return JSONResponse(_error_body("database_error", message, str(exc)), 500,
                            headers={"Content-Language": language})

    @app.exception_handler(VideoJobError)
    async def video_job_error_handler(request: Request, exc: VideoJobError) -> JSONResponse:
        status = 404 if exc.code == "video_job_not_found" else 409 if exc.code in {
            "idempotency_conflict", "video_job_finished"
        } else 422
        message, language = localize_error(
            exc.code, str(exc), request.headers.get("accept-language")
        )
        return JSONResponse(_error_body(exc.code, message), status,
                            headers={"Content-Language": language})

    @app.exception_handler(FileServiceError)
    async def file_service_error_handler(request: Request, exc: FileServiceError) -> JSONResponse:
        message, language = localize_error(
            exc.code, str(exc), request.headers.get("accept-language")
        )
        return JSONResponse(_error_body(exc.code, message), exc.status_code,
                            headers={"Content-Language": language})

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = str(exc.detail.get("error_type", "http_error")) if isinstance(exc.detail, dict) \
            else "http_error"
        if isinstance(exc.detail, dict):
            fallback = str(exc.detail.get("message", "请求失败"))
            message, language = localize_error(
                code, fallback, request.headers.get("accept-language")
            )
        else:
            message, language = localize_http_error(
                exc.status_code, request.headers.get("accept-language")
            )
        headers = dict(exc.headers or {})
        headers["Content-Language"] = language
        if isinstance(exc.detail, dict):
            return JSONResponse(_error_body(code, message, exc.detail.get("cause")),
                                exc.status_code, headers=headers)
        return JSONResponse(_error_body(code, message), exc.status_code, headers=headers)

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
        message, language = localize_error(
            "validation_error", "请求参数无效", request.headers.get("accept-language")
        )
        return JSONResponse(_error_body("validation_error", message, details), 422,
                            headers={"Content-Language": language})

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        manager_logger.exception("unhandled application error")
        message, language = localize_error(
            "internal_error", "服务器内部错误", request.headers.get("accept-language")
        )
        return JSONResponse(_error_body("internal_error", message,
                                        {"error_type": type(exc).__name__}), 500,
                            headers={"Content-Language": language})

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

    def set_session_cookie(response: Response, token: str, remember: bool = False) -> None:
        max_age = REMEMBER_SESSION_TTL_SECONDS if remember else resolved_settings.session_ttl_seconds
        response.set_cookie(SESSION_COOKIE, token, max_age=max_age,
                            httponly=True, secure=resolved_settings.cookie_secure,
                            samesite="strict", path="/")

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        sampler_running = resolved_sampler._task is not None and not resolved_sampler._task.done()
        sampler_error = resolved_sampler.last_error
        public_sampler_error = None if sampler_error is None else {
            "error_type": sampler_error["error_type"],
            "message": sampler_error["message"],
        }
        collector_errors = resolved_sampler.current.get("collector_errors", []) \
            if resolved_sampler.current else []
        history_persistence_error = resolved_sampler.history_persistence_error
        public_history_error = None if history_persistence_error is None else {
            "error_type": history_persistence_error["error_type"],
            "message": history_persistence_error["message"],
        }
        operation_error = resolved_registry.last_operation_error
        health_monitor_error = resolved_registry.last_health_error
        portproxy_error = resolved_registry.last_portproxy_error
        video_job_error = resolved_video_jobs.last_error
        return {"version": __version__, "schema": {"api": "v1", "database": DATABASE_SCHEMA_VERSION},
                "status": "healthy" if sampler_running and not sampler_error and not collector_errors
                and not history_persistence_error and not operation_error
                and not health_monitor_error and not portproxy_error and not video_job_error else "degraded",
                "sampler_running": sampler_running,
                "sampler_error": public_sampler_error,
                "collector_errors": collector_errors,
                "history_persistence_error": public_history_error,
                "service_operation_error": operation_error,
                "service_health_monitor_error": health_monitor_error,
                "wsl_portproxy_error": portproxy_error,
                "video_job_scheduler_error": video_job_error,
                "service_status_mode": "health",
                "sampled_at": resolved_sampler.current.get("sampled_at")
                if resolved_sampler.current else None,
                "readiness": {"setup_complete": resolved_auth.is_setup(),
                              "sampler": "ready" if sampler_running and not sampler_error
                              else "degraded" if sampler_running else "not_ready",
                              "resource_history": "ready" if not history_persistence_error
                              else "degraded",
                              "registered_services": "ready"
                              if not operation_error and not health_monitor_error
                              and not portproxy_error else "degraded",
                              "video_jobs": "ready" if not video_job_error else "degraded"}}

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
    async def auth_login(credentials: LoginCredentials, request: Request,
                         response: Response) -> dict[str, Any]:
        async with auth_concurrency:
            token, csrf, expires_at = await asyncio.to_thread(
                resolved_auth.login, credentials.username, credentials.password,
                _client_ip(request), credentials.remember,
            )
        set_session_cookie(response, token, credentials.remember)
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

    @app.get("/api/v1/users")
    async def users(session: AuthenticatedSession = Depends(require_session)) -> dict[str, Any]:
        items = resolved_auth.list_users()
        for item in items:
            item["is_current"] = item["username"] == session.username
        return {"users": items}

    @app.post("/api/v1/users", status_code=201)
    async def create_user(credentials: Credentials, request: Request,
                          _: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        async with auth_concurrency:
            return await asyncio.to_thread(
                resolved_auth.create_user, credentials.username, credentials.password,
                _client_ip(request),
            )

    @app.put("/api/v1/users/{user_id}/password")
    async def update_user_password(user_id: int, payload: PasswordPayload, request: Request,
                                   session: AuthenticatedSession = Depends(require_csrf)) \
            -> dict[str, Any]:
        async with auth_concurrency:
            return await asyncio.to_thread(
                resolved_auth.update_user_password, user_id, payload.password,
                session.username, _client_ip(request),
            )

    @app.delete("/api/v1/users/{user_id}", status_code=204)
    async def delete_user(user_id: int, request: Request,
                          session: AuthenticatedSession = Depends(require_csrf)) -> Response:
        async with auth_concurrency:
            await asyncio.to_thread(
                resolved_auth.delete_user, user_id, session.username, _client_ip(request)
            )
        return Response(status_code=204)

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
        bucket_seconds = 0 if minutes <= 15 else 15 if minutes <= 60 else 60
        result = await asyncio.to_thread(
            resolved_database.query_resource_history, minutes, bucket_seconds
        )
        return {"window": f"{minutes}m", **result}

    @app.get("/api/v1/host-services", dependencies=[Depends(protected_access)])
    async def host_services() -> dict[str, Any]:
        current = resolved_sampler.current or await resolved_sampler.sample_once()
        return _host_services(current)

    @app.get("/api/v1/registered-services")
    @app.get("/api/v1/services")
    async def registered_services(_: AuthenticatedSession = Depends(require_session)) -> dict[str, Any]:
        return {"services": resolved_registry.list_services(),
                "status_mode": "health"}

    @app.post("/api/v1/registered-services", status_code=201)
    async def create_service(payload: ServicePayload, request: Request,
                             session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        return await resolved_registry.create_service(payload.model_dump(), session.username,
                                                      _client_ip(request))

    @app.put("/api/v1/registered-services/{service_id}")
    async def update_service(service_id: str, payload: ServicePayload, request: Request,
                             session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        return await resolved_registry.update_service(
            service_id, payload.model_dump(exclude_unset=True), session.username,
                                                      _client_ip(request))

    @app.delete("/api/v1/registered-services/{service_id}", status_code=204)
    async def delete_service(service_id: str, request: Request,
                             session: AuthenticatedSession = Depends(require_csrf)) -> Response:
        await resolved_registry.delete_service(service_id, session.username, _client_ip(request))
        return Response(status_code=204)

    @app.post("/api/v1/registered-services/{service_id}/status")
    async def check_service_status(
        service_id: str, _: AuthenticatedSession = Depends(require_csrf)
    ) -> dict[str, Any]:
        return await resolved_registry.check_service_status(service_id)

    @app.post("/api/v1/registered-services/actions/stop-all", status_code=202)
    async def stop_all_services(request: Request,
                                session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, str]:
        operation_id = resolved_registry.submit_stop_all(session.username, _client_ip(request))
        return {"operation_id": operation_id, "status": "queued"}

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

    @app.post("/api/v1/scenes/reorder")
    async def reorder_scenes(payload: SceneOrderPayload, request: Request,
                             session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        return {"scenes": resolved_registry.reorder_scenes(
            payload.scene_ids, session.username, _client_ip(request)
        )}

    @app.put("/api/v1/scenes/{scene_id}")
    async def update_scene(scene_id: str, payload: ScenePayload, request: Request,
                           session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        return resolved_registry.update_scene(scene_id, payload.model_dump(exclude_unset=True), session.username,
                                              _client_ip(request))

    @app.delete("/api/v1/scenes/{scene_id}", status_code=204)
    async def delete_scene(scene_id: str, request: Request,
                           session: AuthenticatedSession = Depends(require_csrf)) -> Response:
        resolved_registry.delete_scene(scene_id, session.username, _client_ip(request))
        return Response(status_code=204)

    @app.put("/api/v1/scenes/{scene_id}/default")
    async def set_default_scene(scene_id: str, request: Request,
                                session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        return resolved_registry.set_default_scene(
            scene_id, True, session.username, _client_ip(request)
        )

    @app.delete("/api/v1/scenes/{scene_id}/default")
    async def clear_default_scene(scene_id: str, request: Request,
                                  session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, Any]:
        return resolved_registry.set_default_scene(
            scene_id, False, session.username, _client_ip(request)
        )

    @app.post("/api/v1/scenes/{scene_id}/activate", status_code=202)
    async def activate_scene(scene_id: str, request: Request,
                             session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, str]:
        operation_id = resolved_registry.submit_scene_activation(
            scene_id, session.username, _client_ip(request)
        )
        return {"operation_id": operation_id, "status": "queued"}

    @app.post("/api/v1/operations/{operation_id}/cancel", status_code=202)
    async def cancel_operation(operation_id: str, request: Request,
                               session: AuthenticatedSession = Depends(require_csrf)) -> dict[str, str]:
        return resolved_registry.request_scene_cancel(
            operation_id, session.username, _client_ip(request)
        )

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

    @app.post("/api/v1/video-jobs", status_code=202)
    async def submit_video_job(
        payload: VideoJobPayload, request: Request, response: Response,
    ) -> dict[str, Any]:
        if not is_loopback(_client_ip(request)):
            raise VideoJobError("loopback_required", "视频任务只允许从本机提交")
        job, created = resolved_video_jobs.submit(payload.model_dump())
        response.status_code = 202 if created else 200
        return {"job": job, "created": created}

    @app.post("/api/v1/video-job-batches", status_code=202)
    async def submit_video_job_batch(
        payload: VideoJobBatchPayload, request: Request, response: Response,
    ) -> dict[str, Any]:
        if not is_loopback(_client_ip(request)):
            raise VideoJobError("loopback_required", "视频任务只允许从本机提交")
        jobs, created = resolved_video_jobs.submit_batch(payload.model_dump())
        response.status_code = 202 if created else 200
        return {"jobs": jobs, "batch_id": jobs[0]["batch_id"], "created": created}

    @app.get("/api/v1/video-jobs")
    async def video_jobs(
        limit: int = Query(default=100, ge=1, le=500),
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        return {
            "jobs": [
                resolved_video_jobs.public_job(job)
                for job in resolved_database.list_video_jobs(limit)
            ],
            "limit": limit,
            "queue_summary": resolved_database.video_job_queue_summary(),
        }

    @app.get("/api/v1/video-jobs/{job_id}")
    async def video_job(
        job_id: str, _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
            raise VideoJobError("video_job_not_found", "视频任务不存在")
        job = resolved_database.get_video_job(job_id)
        if job is None:
            raise VideoJobError("video_job_not_found", "视频任务不存在")
        return resolved_video_jobs.public_job(job)

    @app.post("/api/v1/video-jobs/{job_id}/cancel", status_code=202)
    async def cancel_video_job(
        job_id: str, request: Request,
        session: AuthenticatedSession = Depends(require_csrf),
    ) -> dict[str, Any]:
        if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
            raise VideoJobError("video_job_not_found", "视频任务不存在")
        return resolved_video_jobs.cancel(job_id, session.username, _client_ip(request))

    @app.get("/api/v1/file-service")
    async def file_service_info(
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        return {
            "port": resolved_settings.file_service_port,
            "root": str(resolved_settings.file_service_root),
            "root_available": file_catalog.root.is_dir(),
        }

    @app.get("/api/v1/file-service/files")
    async def list_file_service_files(
        path: str = Query(default="", max_length=4096),
        sort_by: str = Query(default="modified", max_length=16),
        sort_order: str = Query(default="desc", max_length=4),
        _: AuthenticatedSession = Depends(require_session),
    ) -> dict[str, Any]:
        return file_catalog.list_directory(path, sort_by=sort_by, sort_order=sort_order)

    @app.get("/api/v1/file-service/content")
    async def read_file_service_file(
        request: Request,
        path: str = Query(min_length=1, max_length=4096),
        download: bool = Query(default=False),
        _: AuthenticatedSession = Depends(require_session),
    ) -> Response:
        return file_catalog.file_response(
            path,
            download=download,
            range_header=request.headers.get("range"),
        )

    @app.post("/api/v1/file-service/upload", status_code=201)
    async def upload_file_service_file(
        request: Request,
        path: str = Query(default="", max_length=4096),
        name: str = Query(min_length=1, max_length=255),
        session: AuthenticatedSession = Depends(require_csrf),
    ) -> dict[str, Any]:
        temporary, target, stream = file_catalog.begin_upload(path, name)
        try:
            pending = bytearray()
            async for chunk in request.stream():
                pending.extend(chunk)
                if len(pending) >= 1024 * 1024:
                    await asyncio.to_thread(stream.write, bytes(pending))
                    pending.clear()
            if pending:
                await asyncio.to_thread(stream.write, bytes(pending))
            uploaded = await asyncio.to_thread(
                file_catalog.complete_upload, temporary, target, stream,
            )
        except OSError as exc:
            try:
                await asyncio.to_thread(file_catalog.discard_upload, temporary, stream)
            except OSError as cleanup_error:
                exc.add_note(f"上传临时文件清理失败: {cleanup_error}")
            raise FileServiceError(
                "upload_write_failed", f"写入上传文件失败: {exc}", 500,
            ) from exc
        except BaseException as exc:
            try:
                await asyncio.to_thread(file_catalog.discard_upload, temporary, stream)
            except OSError as cleanup_error:
                exc.add_note(f"上传临时文件清理失败: {cleanup_error}")
            raise
        resolved_database.append_audit(
            _client_ip(request), "management.file.upload", "success",
            {"username": session.username, "path": uploaded["path"], "size": uploaded["size"]},
        )
        return {"file": uploaded}

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

    @app.get("/i18n.js", include_in_schema=False)
    async def i18n_javascript() -> FileResponse:
        return FileResponse(PROJECT_ROOT / "i18n.js", media_type="text/javascript")

    @app.get("/request-guard.js", include_in_schema=False)
    async def request_guard_javascript() -> FileResponse:
        return FileResponse(PROJECT_ROOT / "request-guard.js", media_type="text/javascript")

    @app.get("/gpu-layout.js", include_in_schema=False)
    async def gpu_layout_javascript() -> FileResponse:
        return FileResponse(PROJECT_ROOT / "gpu-layout.js", media_type="text/javascript")

    @app.get("/monitor-chart.js", include_in_schema=False)
    async def monitor_chart_javascript() -> FileResponse:
        return FileResponse(PROJECT_ROOT / "monitor-chart.js", media_type="text/javascript")

    @app.get("/theme.js", include_in_schema=False)
    async def theme_javascript() -> FileResponse:
        return FileResponse(PROJECT_ROOT / "theme.js", media_type="text/javascript")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app
