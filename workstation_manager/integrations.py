from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx

from .discovery import redact_sensitive_text


SCHEMA_VERSION = 1
KNOWN_WEBUI_IDS = {"ninfer-4090", "ninfer-3090", "comfyui-4090", "comfyui-3090-audio", "lmstudio-monitor"}
ALLOWED_METHODS = {"GET", "HEAD"}
REQUEST_BODY_MAX_BYTES = 1024 * 1024
RESPONSE_BODY_MAX_BYTES = 16 * 1024 * 1024
QUERY_MAX_BYTES = 4096
ANSI_RE = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
SAFE_NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,80}")
SAFE_KIND_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
SAFE_CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SAFE_DISTRO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
SAFE_UNIT_RE = re.compile(r"[A-Za-z0-9@_.:-]{1,128}\.service")


class IntegrationConfigError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class IntegrationError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class LogProcessCleanupError(subprocess.SubprocessError):
    """Raised when a bounded log subprocess cannot be confirmed terminated."""


@dataclass(frozen=True)
class BackendProbeConfig:
    url: str
    timeout_seconds: float = 2.0
    json_equals: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class WebUIConfig:
    id: str
    name: str
    kind: str
    configured: bool
    target: str
    health_path: str = "/"
    backend_probe: BackendProbeConfig | None = None


@dataclass(frozen=True)
class LogSourceConfig:
    id: str
    name: str
    type: str
    configured: bool
    container: str | None = None
    distro: str | None = None
    scope: str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class IntegrationsConfig:
    source: str = "unconfigured"
    webuis: tuple[WebUIConfig, ...] = ()
    log_sources: tuple[LogSourceConfig, ...] = ()
    blockers: tuple[str, ...] = ("缺少正式 config/integrations.json",)


def _strict_keys(value: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise IntegrationConfigError("unknown_field", f"{where} 包含未知字段: {', '.join(unknown)}")


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise IntegrationConfigError("invalid_type", f"{where} 必须是布尔值")
    return value


def _text(value: Any, pattern: re.Pattern[str], where: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise IntegrationConfigError("invalid_value", f"{where} 格式无效")
    return value


def _validate_loopback_url(value: Any, where: str) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise IntegrationConfigError("invalid_target", f"{where} 必须是短 URL 字符串")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise IntegrationConfigError("invalid_target", f"{where} 只允许 http/https")
    if parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.query:
        raise IntegrationConfigError("invalid_target", f"{where} 禁止凭据、查询参数或 fragment")
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise IntegrationConfigError("invalid_target", f"{where} 端口无效") from exc
    if parsed.hostname is None or parsed_port is None:
        raise IntegrationConfigError("invalid_target", f"{where} 必须使用 loopback IP 和显式固定端口")
    try:
        address = ipaddress.ip_address(parsed.hostname.split("%", 1)[0])
    except ValueError as exc:
        raise IntegrationConfigError("invalid_target", f"{where} 禁止 DNS 名称") from exc
    if not address.is_loopback:
        raise IntegrationConfigError("invalid_target", f"{where} 只允许 loopback IP")
    if parsed.path not in {"", "/"}:
        raise IntegrationConfigError("invalid_target", f"{where} 不允许基础路径")
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{parsed.scheme}://{host}:{parsed_port}"


def _validate_probe_url(value: Any, where: str) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise IntegrationConfigError("invalid_backend_probe", f"{where} 必须是短 URL 字符串")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise IntegrationConfigError("invalid_backend_probe", f"{where} 只允许无凭据/query/fragment 的 http/https URL")
    try:
        address = ipaddress.ip_address((parsed.hostname or "").split("%", 1)[0])
        port = parsed.port
    except (ValueError, TypeError) as exc:
        raise IntegrationConfigError("invalid_backend_probe", f"{where} 必须使用 loopback IP 和显式端口") from exc
    if not address.is_loopback or port is None or not parsed.path.startswith("/") or "\\" in parsed.path:
        raise IntegrationConfigError("invalid_backend_probe", f"{where} 必须使用 loopback IP、显式端口和绝对路径")
    try:
        _validate_proxy_path(parsed.path)
    except IntegrationError as exc:
        raise IntegrationConfigError("invalid_backend_probe", f"{where} 路径无效") from exc
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return urlunsplit((parsed.scheme, f"{host}:{port}", parsed.path, "", ""))


def _parse_backend_probe(value: Any, where: str) -> BackendProbeConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise IntegrationConfigError("invalid_backend_probe", f"{where} 必须是对象")
    _strict_keys(value, {"url", "timeout_seconds", "json_equals"}, where)
    timeout = value.get("timeout_seconds", 2.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= float(timeout) <= 10:
        raise IntegrationConfigError("invalid_backend_probe", f"{where}.timeout_seconds 必须在 0.1..10")
    raw_equals = value.get("json_equals", {})
    if not isinstance(raw_equals, dict) or len(raw_equals) > 16:
        raise IntegrationConfigError("invalid_backend_probe", f"{where}.json_equals 必须是最多 16 项对象")
    equals: list[tuple[str, Any]] = []
    for field, expected in raw_equals.items():
        if not isinstance(field, str) or re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){0,7}", field) is None:
            raise IntegrationConfigError("invalid_backend_probe", f"{where}.json_equals 字段路径无效")
        if expected is not None and not isinstance(expected, (str, int, float, bool)):
            raise IntegrationConfigError("invalid_backend_probe", f"{where}.json_equals 只允许标量")
        equals.append((field, expected))
    return BackendProbeConfig(_validate_probe_url(value.get("url"), f"{where}.url"), float(timeout), tuple(equals))


def _validate_proxy_path(path: str, raw_path: bytes | None = None) -> str:
    raw = raw_path.decode("latin-1", "ignore") if raw_path else path
    if len(raw) > 4096 or "\\" in raw or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise IntegrationError(400, "invalid_proxy_path", "代理路径无效")
    decoded = raw
    for _ in range(8):
        newer = unquote(decoded)
        if newer == decoded:
            break
        decoded = newer
    if unquote(decoded) != decoded:
        raise IntegrationError(400, "invalid_proxy_path", "代理路径无效")
    if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
        raise IntegrationError(400, "invalid_proxy_path", "代理路径无效")
    if path.startswith("//"):
        raise IntegrationError(400, "invalid_proxy_path", "代理路径无效")
    candidate = path.lstrip("/")
    if urlsplit(candidate).scheme:
        raise IntegrationError(400, "invalid_proxy_path", "代理路径无效")
    return "/" + candidate


def _parse_webui(item: Any, preview: bool) -> WebUIConfig:
    if not isinstance(item, dict):
        raise IntegrationConfigError("invalid_type", "webuis 条目必须是对象")
    _strict_keys(item, {"id", "name", "kind", "configured", "target", "health_path", "backend_probe"}, "webuis 条目")
    webui_id = _text(item.get("id"), SAFE_ID_RE, "webuis.id")
    if webui_id not in KNOWN_WEBUI_IDS:
        raise IntegrationConfigError("unknown_webui", f"未知 WebUI ID: {webui_id}")
    target = _validate_loopback_url(item.get("target"), f"webuis[{webui_id}].target")
    health_path = item.get("health_path", "/")
    if not isinstance(health_path, str):
        raise IntegrationConfigError("invalid_health_path", "health_path 必须是字符串")
    try:
        health_path = _validate_proxy_path(health_path)
    except IntegrationError as exc:
        raise IntegrationConfigError("invalid_health_path", "health_path 路径无效") from exc
    return WebUIConfig(
        id=webui_id,
        name=_text(item.get("name"), SAFE_NAME_RE, "webuis.name"),
        kind=_text(item.get("kind"), SAFE_KIND_RE, "webuis.kind"),
        configured=False if preview else _bool(item.get("configured"), "webuis.configured"),
        target=target,
        health_path=health_path,
        backend_probe=_parse_backend_probe(item.get("backend_probe"), f"webuis[{webui_id}].backend_probe"),
    )


def _parse_log_source(item: Any, preview: bool) -> LogSourceConfig:
    if not isinstance(item, dict):
        raise IntegrationConfigError("invalid_type", "log_sources 条目必须是对象")
    _strict_keys(item, {"id", "name", "type", "configured", "container", "distro", "scope", "unit"}, "log_sources 条目")
    source_id = _text(item.get("id"), SAFE_ID_RE, "log_sources.id")
    if source_id == "manager":
        raise IntegrationConfigError("reserved_id", "log_sources.id manager 为内建来源保留")
    source_type = item.get("type")
    if source_type not in {"docker_logs", "wsl_journal"}:
        raise IntegrationConfigError("invalid_log_source", "日志来源只允许 docker_logs/wsl_journal")
    configured = False if preview else _bool(item.get("configured"), "log_sources.configured")
    common = dict(id=source_id, name=_text(item.get("name"), SAFE_NAME_RE, "log_sources.name"), type=source_type, configured=configured)
    if source_type == "docker_logs":
        if set(item) & {"distro", "scope", "unit"}:
            raise IntegrationConfigError("invalid_log_source", "docker_logs 不允许 WSL 字段")
        return LogSourceConfig(**common, container=_text(item.get("container"), SAFE_CONTAINER_RE, "container"))
    if "container" in item:
        raise IntegrationConfigError("invalid_log_source", "wsl_journal 不允许 container")
    scope = item.get("scope", "system")
    if scope not in {"system", "user"}:
        raise IntegrationConfigError("invalid_log_source", "scope 只允许 system/user")
    return LogSourceConfig(
        **common,
        distro=_text(item.get("distro"), SAFE_DISTRO_RE, "distro"),
        scope=scope,
        unit=_text(item.get("unit"), SAFE_UNIT_RE, "unit"),
    )


def load_integrations_config(path: Path, example_path: Path | None = None) -> IntegrationsConfig:
    formal = path.exists()
    selected = path if formal else (example_path or path.with_name("integrations.example.json"))
    if not selected.exists():
        return IntegrationsConfig()
    try:
        loaded = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise IntegrationConfigError("invalid_integrations_file", "无法读取 integrations 配置") from exc
    if not isinstance(loaded, dict):
        raise IntegrationConfigError("invalid_type", "integrations 根节点必须是对象")
    _strict_keys(loaded, {"schema_version", "webuis", "log_sources"}, "integrations")
    if loaded.get("schema_version") != SCHEMA_VERSION:
        raise IntegrationConfigError("unsupported_schema", "integrations schema_version 必须为 1")
    webuis_raw, logs_raw = loaded.get("webuis", []), loaded.get("log_sources", [])
    if not isinstance(webuis_raw, list) or not isinstance(logs_raw, list):
        raise IntegrationConfigError("invalid_type", "webuis/log_sources 必须是数组")
    preview = not formal
    webuis = tuple(_parse_webui(item, preview) for item in webuis_raw)
    logs = tuple(_parse_log_source(item, preview) for item in logs_raw)
    if len({item.id for item in webuis}) != len(webuis) or len({item.id for item in logs}) != len(logs):
        raise IntegrationConfigError("duplicate_id", "integration ID 不得重复")
    blockers = () if formal else ("正式 config/integrations.json 不存在；示例仅供预览，不会探测或代理",)
    return IntegrationsConfig(source="formal" if formal else "example_preview", webuis=webuis, log_sources=logs, blockers=blockers)


class WebUIService:
    def __init__(
        self,
        config: IntegrationsConfig,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        probe_timeout: float = 2.0,
        proxy_timeout: float = 15.0,
        concurrency: int = 4,
    ) -> None:
        self.config = config
        self.client_factory = client_factory or (
            lambda: httpx.AsyncClient(trust_env=False, follow_redirects=False)
        )
        self.probe_timeout = probe_timeout
        self.proxy_timeout = proxy_timeout
        self.semaphore = asyncio.Semaphore(concurrency)

    def get(self, webui_id: str) -> WebUIConfig:
        item = next((candidate for candidate in self.config.webuis if candidate.id == webui_id), None)
        if item is None:
            raise IntegrationError(404, "webui_not_found", "WebUI 不存在")
        return item

    async def list_status(self) -> dict[str, Any]:
        async def status(item: WebUIConfig) -> dict[str, Any]:
            now = datetime.now(timezone.utc).isoformat()
            blockers = list(self.config.blockers)
            state = "unconfigured"
            backend_status = "unknown"
            backend_checked_at: str | None = None
            backend_blocker: str | None = "未配置模型后端探测" if item.backend_probe is None else None
            if item.configured and self.config.source == "formal":
                try:
                    async with self.client_factory() as client:
                        async with client.stream(
                            "GET", item.target + item.health_path,
                            timeout=self.probe_timeout,
                            headers={"Accept": "text/html,application/json"},
                        ) as response:
                            status_code = response.status_code
                            location = response.headers.get("location")
                    state = "online" if 200 <= status_code < 400 else "offline"
                    if state == "online" and location:
                        redirected = urlsplit(urljoin(item.target + "/", location))
                        origin = urlsplit(item.target)
                        try:
                            same_origin = (redirected.scheme, redirected.hostname, redirected.port) == (origin.scheme, origin.hostname, origin.port)
                        except ValueError:
                            same_origin = False
                        if not same_origin:
                            state = "offline"
                            blockers.append("健康检查返回外部跳转")
                    if state == "offline":
                        blockers.append("健康检查未就绪")
                except (httpx.HTTPError, asyncio.TimeoutError):
                    state = "offline"
                    blockers.append("健康检查不可用")
                if item.backend_probe is not None:
                    backend_checked_at = datetime.now(timezone.utc).isoformat()
                    try:
                        async with self.client_factory() as client:
                            async with client.stream(
                                "GET", item.backend_probe.url,
                                timeout=item.backend_probe.timeout_seconds,
                                headers={"Accept": "application/json"},
                            ) as response:
                                backend_status = "online" if 200 <= response.status_code < 300 else "offline"
                                body = bytearray()
                                if backend_status == "online" and item.backend_probe.json_equals:
                                    async for chunk in response.aiter_bytes():
                                        if len(body) + len(chunk) > 64 * 1024:
                                            backend_status, backend_blocker = "offline", "模型后端响应超过限制"
                                            break
                                        body.extend(chunk)
                        if backend_status == "online" and item.backend_probe.json_equals:
                            try:
                                payload = json.loads(body)
                            except (UnicodeDecodeError, ValueError, TypeError):
                                backend_status, backend_blocker = "offline", "模型后端响应不是有效 JSON"
                            else:
                                for field, expected in item.backend_probe.json_equals:
                                    actual: Any = payload
                                    for part in field.split("."):
                                        if not isinstance(actual, dict) or part not in actual:
                                            actual = object()
                                            break
                                        actual = actual[part]
                                    if type(actual) is not type(expected) or actual != expected:
                                        backend_status, backend_blocker = "offline", "模型后端响应字段不匹配"
                                        break
                        if backend_status == "offline" and backend_blocker is None:
                            backend_blocker = "模型后端健康检查未就绪"
                    except (httpx.HTTPError, asyncio.TimeoutError):
                        backend_status, backend_blocker = "offline", "模型后端健康检查不可用"
            else:
                blockers.append("目标尚未在正式配置中启用")
            return {
                "id": item.id, "name": item.name, "kind": item.kind,
                "configured": item.configured and self.config.source == "formal",
                "status": state, "ui_status": state, "backend_status": backend_status,
                "backend_checked_at": backend_checked_at, "backend_blocker": backend_blocker,
                "last_check": now,
                "proxy_url": f"/proxy/webui/{item.id}/" if state == "online" else None,
                "blockers": list(dict.fromkeys(blockers)),
            }
        return {"source": self.config.source, "webuis": await asyncio.gather(*(status(item) for item in self.config.webuis)), "blockers": list(self.config.blockers)}

    async def proxy(
        self,
        webui_id: str,
        path: str,
        method: str,
        raw_path: bytes | None,
        query: bytes,
        headers: Any,
        body: bytes,
        proxy_prefix: str | None = None,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        item = self.get(webui_id)
        if self.config.source != "formal" or not item.configured:
            raise IntegrationError(409, "webui_unconfigured", "WebUI 尚未配置")
        if method not in ALLOWED_METHODS:
            raise IntegrationError(405, "method_not_allowed", "代理方法不受支持")
        if len(body) > REQUEST_BODY_MAX_BYTES:
            raise IntegrationError(413, "proxy_request_too_large", "代理请求体超过限制")
        if len(query) > QUERY_MAX_BYTES or any(byte < 32 or byte == 127 for byte in query):
            raise IntegrationError(400, "invalid_proxy_query", "代理查询参数无效")
        safe_path = _validate_proxy_path(path, raw_path)
        target = item.target + safe_path
        if query:
            try:
                target += "?" + query.decode("ascii", "strict")
            except UnicodeError as exc:
                raise IntegrationError(400, "invalid_proxy_query", "代理查询参数无效") from exc
        outgoing_headers: dict[str, str] = {}
        for name in ("accept", "accept-language", "content-type", "range", "if-none-match", "if-modified-since"):
            if headers.get(name):
                outgoing_headers[name] = headers[name]
        acquired = False
        try:
            await asyncio.wait_for(self.semaphore.acquire(), timeout=0.01)
            acquired = True
        except asyncio.TimeoutError as exc:
            raise IntegrationError(503, "proxy_busy", "代理并发已达上限") from exc
        try:
            try:
                async with self.client_factory() as client:
                    async with client.stream(
                        method, target, headers=outgoing_headers, content=body,
                        timeout=self.proxy_timeout,
                    ) as response:
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > RESPONSE_BODY_MAX_BYTES:
                                raise IntegrationError(502, "proxy_response_too_large", "上游响应超过限制")
                        content_type = response.headers.get("content-type", "").lower()
                        if content_type.startswith("text/event-stream"):
                            raise IntegrationError(502, "unsupported_stream", "只读预览不支持流式响应")
                        payload = bytes(content)
                        response_prefix = proxy_prefix or f"/proxy/webui/{item.id}"
                        if "text/html" in content_type and method != "HEAD":
                            payload = _rewrite_html(payload, response_prefix, safe_path)
                        response_headers = self._response_headers(
                            item, response.headers, response_prefix, target,
                        )
                        return response.status_code, response_headers, payload
            except IntegrationError:
                raise
            except (httpx.HTTPError, asyncio.TimeoutError, UnicodeError) as exc:
                raise IntegrationError(502, "upstream_unavailable", "WebUI 上游不可用") from exc
        finally:
            if acquired:
                self.semaphore.release()

    @staticmethod
    def _response_headers(
        item: WebUIConfig, headers: httpx.Headers, proxy_prefix: str,
        request_url: str,
    ) -> list[tuple[str, str]]:
        allowed = {"content-type", "content-language", "etag", "last-modified", "content-range", "accept-ranges"}
        result = [(name, value) for name, value in headers.multi_items() if name.lower() in allowed]
        result.append(("content-disposition", "inline"))
        result.append(("cache-control", "no-store"))
        location = headers.get("location")
        if location:
            absolute = urljoin(request_url, location)
            parsed, origin = urlsplit(absolute), urlsplit(item.target)
            try:
                same_origin = (parsed.scheme, parsed.hostname, parsed.port) == (origin.scheme, origin.hostname, origin.port)
            except ValueError:
                same_origin = False
            if not same_origin:
                raise IntegrationError(502, "external_redirect_blocked", "上游外部跳转已阻止")
            rewritten = f"{proxy_prefix}{parsed.path or '/'}"
            if parsed.query:
                rewritten += "?" + parsed.query
            if parsed.fragment:
                rewritten += "#" + parsed.fragment
            result.append(("location", rewritten))
        # Set-Cookie 被有意剥离，避免不同 WebUI 在管理器来源下共享 Cookie。
        return result


def _rewrite_html(payload: bytes, prefix: str, request_path: str) -> bytes:
    text = payload.decode("utf-8", "replace")
    default_base_path = request_path if request_path.endswith("/") else request_path.rsplit("/", 1)[0] + "/"
    base_path = default_base_path
    upstream_base = re.search(r"(?i)<base\b[^>]*\bhref\s*=\s*(['\"])(.*?)\1[^>]*>", text)
    if upstream_base:
        candidate = upstream_base.group(2)
        parsed = urlsplit(candidate)
        if not parsed.scheme and not parsed.netloc and not parsed.query and not parsed.fragment and "\\" not in candidate:
            resolved_path = urljoin("http://proxy.invalid" + default_base_path, candidate)
            resolved = urlsplit(resolved_path).path
            if not any(part in {".", ".."} for part in resolved.split("/")):
                base_path = resolved if resolved.endswith("/") else resolved.rsplit("/", 1)[0] + "/"
    text = re.sub(r"(?i)<base\b[^>]*>", "", text)
    text = re.sub(
        r"(?i)(\b(?:src|href|action)\s*=\s*['\"])/(?!/|proxy(?:/webui|-asset)/)",
        lambda match: match.group(1) + prefix + "/",
        text,
    )
    base = f'<base href="{prefix}{base_path}">'
    if re.search(r"(?i)<head(?:\s[^>]*)?>", text):
        text = re.sub(r"(?i)(<head(?:\s[^>]*)?>)", r"\1" + base, text, count=1)
    else:
        text = base + text
    return text.encode("utf-8")


def validate_lines(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise IntegrationError(422, "invalid_lines", "lines 必须在 1..1000")
    return value


def validate_since(value: str) -> str:
    match = re.fullmatch(r"([1-9][0-9]{0,3})([mhd])", value)
    if match is None:
        raise IntegrationError(422, "invalid_since", "since 仅支持 1m..168h 或 1d..7d")
    number, unit = int(match.group(1)), match.group(2)
    seconds = number * {"m": 60, "h": 3600, "d": 86400}[unit]
    if seconds > 7 * 86400:
        raise IntegrationError(422, "invalid_since", "since 最大为 7 天")
    return value


def sanitize_log_text(value: str, max_bytes: int = 256 * 1024) -> tuple[str, bool]:
    encoded = value.encode("utf-8", "replace")
    truncated = len(encoded) > max_bytes
    if truncated:
        encoded = encoded[:max_bytes]
        value = encoded.decode("utf-8", "ignore")
    clean = ANSI_RE.sub("", value).replace("\x00", "")
    clean = re.sub(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", "<redacted-jwt>", clean)
    clean = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9]{12,}|hf_[A-Za-z0-9]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|sk-[A-Za-z0-9_-]{12,})\b", "<redacted-token>", clean)
    clean = re.sub(r"(?i)([a-z][a-z0-9+.-]*://[^\s/@:]+:)[^\s/@]+(@)", r"\1<redacted>\2", clean)
    clean, _ = redact_sensitive_text(clean)
    return clean, truncated


def _bounded_subprocess_run(args: list[str], timeout: float, startupinfo: Any, creationflags: int, cancel_event: threading.Event | None = None) -> subprocess.CompletedProcess[str]:
    if cancel_event is not None and cancel_event.is_set():
        raise subprocess.SubprocessError("log read cancelled")
    process = subprocess.Popen(
        args, shell=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        startupinfo=startupinfo, creationflags=creationflags,
    )
    output = bytearray()
    hit_limit = threading.Event()

    def terminate_and_wait() -> None:
        kill_error: BaseException | None = None
        try:
            process.kill()
        except BaseException as exc:
            kill_error = exc
        try:
            process.wait(timeout=1)
        except BaseException as wait_error:
            error = LogProcessCleanupError("日志采集进程无法确认终止")
            if kill_error is not None:
                error.add_note(f"kill failed: {type(kill_error).__name__}")
            raise error from wait_error
        if kill_error is not None:
            raise LogProcessCleanupError("日志采集进程 kill 失败") from kill_error

    def read_output() -> None:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(8192)
            if not chunk:
                return
            remaining = 256 * 1024 - len(output)
            output.extend(chunk[:remaining])
            if len(chunk) > remaining or remaining == 0:
                hit_limit.set()
                return

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if hit_limit.is_set():
            terminate_and_wait()
            break
        if cancel_event is not None and cancel_event.wait(0.05):
            terminate_and_wait(); reader.join(timeout=1)
            raise subprocess.SubprocessError("log read cancelled")
        if time.monotonic() >= deadline:
            terminate_and_wait(); reader.join(timeout=1)
            raise subprocess.TimeoutExpired(args, timeout)
        time.sleep(0.05)
    returncode = process.wait(timeout=1)
    reader.join(timeout=1)
    if hit_limit.is_set():
        output.extend(b"\n[output truncated by manager]\n")
        returncode = 0
    return subprocess.CompletedProcess(args, returncode, output.decode("utf-8", "replace"), "")


def _read_file_tail(path: Path, max_bytes: int = 256 * 1024) -> tuple[str, bool]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        data = handle.read(max_bytes)
    return data.decode("utf-8", "replace"), size > max_bytes


class LogService:
    def __init__(
        self,
        config: IntegrationsConfig,
        manager_log_path: Path,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        timeout: float = 8.0,
    ) -> None:
        self.config = config
        self.manager_log_path = manager_log_path
        self.runner = runner
        self.timeout = timeout
        self.semaphore = threading.BoundedSemaphore(4)
        self.source_semaphores = {
            item.id: threading.BoundedSemaphore(1) for item in config.log_sources
        }
        self.source_semaphores["manager"] = threading.BoundedSemaphore(1)

    def list_sources(self) -> dict[str, Any]:
        sources = [{"id": "manager", "name": "管理器自身日志", "type": "manager_file", "configured": True, "blockers": []}]
        for item in self.config.log_sources:
            configured = self.config.source == "formal" and item.configured
            sources.append({
                "id": item.id, "name": item.name, "type": item.type,
                "configured": configured,
                "blockers": [] if configured else ["日志来源尚未在正式配置中启用"],
            })
        return {"source": self.config.source, "sources": sources, "blockers": list(self.config.blockers)}

    def entries(self, source_id: str, lines: int, since: str, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        lines = validate_lines(lines)
        since = validate_since(since)
        if source_id == "manager":
            if not self.semaphore.acquire(blocking=False):
                raise IntegrationError(503, "log_reader_busy", "日志读取并发已达上限")
            source_semaphore = self.source_semaphores["manager"]
            if not source_semaphore.acquire(blocking=False):
                self.semaphore.release()
                raise IntegrationError(503, "log_reader_busy", "该日志来源已有读取任务")
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise IntegrationError(499, "log_read_cancelled", "日志读取已取消")
                content, tail_truncated = _read_file_tail(self.manager_log_path) if self.manager_log_path.exists() else ("", False)
                if cancel_event is not None and cancel_event.is_set():
                    raise IntegrationError(499, "log_read_cancelled", "日志读取已取消")
            except OSError as exc:
                raise IntegrationError(500, "log_read_failed", "管理器日志读取失败") from exc
            finally:
                source_semaphore.release()
                self.semaphore.release()
            selected = "\n".join(content.splitlines()[-lines:])
            clean, truncated = sanitize_log_text(selected)
            return {"source_id": source_id, "lines": clean.splitlines(), "truncated": tail_truncated or truncated}
        item = next((candidate for candidate in self.config.log_sources if candidate.id == source_id), None)
        if item is None:
            raise IntegrationError(404, "log_source_not_found", "日志来源不存在")
        if self.config.source != "formal" or not item.configured:
            raise IntegrationError(409, "log_source_unconfigured", "日志来源尚未配置")
        if item.type == "docker_logs":
            args = ["docker", "logs", "--tail", str(lines), "--since", since, item.container or ""]
        else:
            args = ["wsl.exe", "-d", item.distro or "", "--exec", "journalctl", "--no-pager", "--output=short-iso", f"--lines={lines}", f"--since=-{since}"]
            if item.scope == "user":
                args.append("--user")
            args.append(f"--unit={item.unit}")
        startupinfo = None
        creationflags = 0
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        source_semaphore = self.source_semaphores[source_id]
        if not self.semaphore.acquire(blocking=False):
            raise IntegrationError(503, "log_reader_busy", "日志读取并发已达上限")
        if not source_semaphore.acquire(blocking=False):
            self.semaphore.release()
            raise IntegrationError(503, "log_reader_busy", "该日志来源已有读取任务")
        try:
            if self.runner is None:
                completed = _bounded_subprocess_run(args, self.timeout, startupinfo, creationflags, cancel_event)
            else:
                completed = self.runner(
                    args, shell=False, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=self.timeout, check=False,
                    startupinfo=startupinfo, creationflags=creationflags,
                )
        except LogProcessCleanupError as exc:
            raise IntegrationError(502, "log_cancel_cleanup_failed", "日志采集进程无法确认终止") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise IntegrationError(499, "log_read_cancelled", "日志读取已取消") from exc
            raise IntegrationError(502, "log_read_failed", "日志来源读取失败") from exc
        finally:
            source_semaphore.release()
            self.semaphore.release()
        combined = completed.stdout + (("\n" + completed.stderr) if completed.stderr else "")
        clean, truncated = sanitize_log_text(combined)
        if completed.returncode != 0:
            raise IntegrationError(502, "log_read_failed", "日志来源读取失败")
        return {"source_id": source_id, "lines": clean.splitlines()[-lines:], "truncated": truncated}
