from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    """Raised when manager configuration is invalid."""


DEFAULT_PORTS = (1234, 3000, 8000, 8001, 8080, 8081, 8765, 18020, 18030, 18031)
MIN_SAMPLE_INTERVAL_SECONDS = 0.5
MAX_SAMPLE_INTERVAL_SECONDS = 3600.0
MIN_HISTORY_MINUTES = 1
MAX_HISTORY_MINUTES = 1440
MIN_COMMAND_TIMEOUT_SECONDS = 0.1
MAX_COMMAND_TIMEOUT_SECONDS = 120.0
MAX_HISTORY_CAPACITY = 172801


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 19100
    sample_interval_seconds: float = 5.0
    history_minutes: int = 15
    command_timeout_seconds: float = 4.0
    critical_ports: tuple[int, ...] = DEFAULT_PORTS
    database_path: Path = PROJECT_ROOT / "data" / "workstation-manager.db"
    session_ttl_seconds: int = 12 * 60 * 60
    cookie_secure: bool = False
    request_body_max_bytes: int = 64 * 1024
    auth_concurrency_limit: int = 2
    session_max_active: int = 64
    audit_retention_max_events: int = 10_000
    audit_retention_days: int = 90
    login_failure_max_rows: int = 10_000
    operation_retention_max: int = 1000
    script_status_timeout_seconds: float = 3.0
    script_action_timeout_seconds: float = 600.0
    manager_log_path: Path = PROJECT_ROOT / "logs" / "manager.log"
    manager_log_level: str = "INFO"
    manager_log_max_bytes: int = 5 * 1024 * 1024
    manager_log_backup_count: int = 5
    setup_disabled: bool = False
    allowed_public_origins: tuple[str, ...] = ()
    trusted_proxy_ips: tuple[str, ...] = ()

    @property
    def history_capacity(self) -> int:
        try:
            history_minutes = float(self.history_minutes)
            sample_interval_seconds = float(self.sample_interval_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConfigError(f"无法计算历史容量: {exc}") from exc
        if (
            not math.isfinite(history_minutes)
            or not math.isfinite(sample_interval_seconds)
            or history_minutes <= 0
            or sample_interval_seconds <= 0
        ):
            raise ConfigError("历史容量输入必须是有限正数")
        sample_count = history_minutes * 60 / sample_interval_seconds
        if not math.isfinite(sample_count) or sample_count < 0:
            raise ConfigError("历史容量必须是有限正数")
        capacity = int(sample_count) + 1
        if not 1 <= capacity <= MAX_HISTORY_CAPACITY:
            raise ConfigError(f"历史容量必须在 1..{MAX_HISTORY_CAPACITY}，实际值为 {capacity}")
        return capacity


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} 必须是有限数字，实际值为 {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError(f"{name} 必须是数字，实际值为 {value!r}") from exc
    if not math.isfinite(number):
        raise ConfigError(f"{name} 必须是有限数字，实际值为 {value!r}")
    if number <= 0:
        raise ConfigError(f"{name} 必须大于 0，实际值为 {number}")
    return number


def _bounded_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    number = _positive_number(value, name)
    if not minimum <= number <= maximum:
        raise ConfigError(f"{name} 必须在 {minimum:g}..{maximum:g}，实际值为 {number:g}")
    return number


def _integer(value: Any, name: str, description: str = "正整数") -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} 必须是{description}，实际值为 {value!r}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        raw = value.strip()
        try:
            number = int(raw)
        except (ValueError, OverflowError) as exc:
            raise ConfigError(
                f"{name} 必须是{description}，实际值包含 {len(raw)} 位数字"
            ) from exc
    else:
        raise ConfigError(f"{name} 必须是{description}，实际值为 {value!r}")
    return number


def _port(value: Any, name: str) -> int:
    port = _integer(value, name, "整数端口")
    if not 1 <= port <= 65535:
        raise ConfigError(f"{name} 必须在 1..65535，实际值为 {port}")
    return port


def _bounded_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    number = _integer(value, name)
    if not minimum <= number <= maximum:
        raise ConfigError(f"{name} 必须在 {minimum}..{maximum}，实际值为 {number}")
    return number


def _ports(value: Any) -> tuple[int, ...]:
    raw = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw, (list, tuple)):
        raise ConfigError("critical_ports 必须是端口数组或逗号分隔字符串")
    return tuple(dict.fromkeys(_port(item, "critical_ports") for item in raw))


def _allowed_public_origins(value: Any) -> tuple[str, ...]:
    raw = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw, (list, tuple)):
        raise ConfigError("allowed_public_origins 必须是 origin 数组或逗号分隔字符串")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigError("allowed_public_origins 条目必须是字符串")
        candidate = item.strip().lower()
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(candidate)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ConfigError(f"allowed_public_origins 条目格式无效: {item!r}") from exc
        if parsed.scheme != "https" or not hostname or parsed.username is not None or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ConfigError(f"allowed_public_origins 条目必须是无路径的 HTTPS origin: {item!r}")
        normalized = f"https://{parsed.netloc}"
        try:
            import ipaddress
            ipaddress.ip_address(hostname)
        except ValueError:
            labels = hostname.split(".")
            if any(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None for label in labels):
                raise ConfigError(f"allowed_public_origins 主机格式无效: {item!r}")
        try:
            if port is not None:
                _port(port, "allowed_public_origins")
        except ValueError as exc:
            raise ConfigError(f"allowed_public_origins 端口无效: {item!r}") from exc
        if "*" in parsed.netloc:
            raise ConfigError(f"allowed_public_origins 条目格式无效: {item!r}")
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _trusted_proxy_ips(value: Any) -> tuple[str, ...]:
    import ipaddress
    raw = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw, (list, tuple)):
        raise ConfigError("trusted_proxy_ips 必须是 IP 数组或逗号分隔字符串")
    result: list[str] = []
    for item in raw:
        try:
            address = ipaddress.ip_address(str(item).strip())
        except ValueError as exc:
            raise ConfigError(f"trusted_proxy_ips 只允许 IP 字面值: {item!r}") from exc
        if not (address.is_loopback or address.is_private):
            raise ConfigError("trusted_proxy_ips 只允许 loopback/private IP")
        normalized = address.compressed
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _boolean(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{name} 必须是布尔值，实际值为 {value!r}")


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ConfigError(f"{name} 必须是路径字符串，实际值为 {value!r}")
    text = os.fspath(value).strip()
    if not text:
        raise ConfigError(f"{name} 不能为空")
    return Path(text)


def _local_log_path(value: Any) -> Path:
    path = _path(value, "manager_log_path")
    text = os.fspath(value).strip().replace("/", "\\")
    if text.startswith("\\\\"):
        raise ConfigError("manager_log_path 只允许本地磁盘路径，禁止 UNC/设备路径")
    return path


def _log_level(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"manager_log_level 必须是字符串，实际值为 {value!r}")
    normalized = value.strip().upper()
    if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("manager_log_level 必须是 DEBUG/INFO/WARNING/ERROR/CRITICAL")
    return normalized


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    data: dict[str, Any] = {}
    config_path = env.get("WM_CONFIG_FILE")
    if config_path:
        path = Path(config_path)
        try:
            config_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc
        try:
            loaded = json.loads(config_text)
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"配置文件 {path} 不是有效 JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"配置文件 {path} 的根节点必须是对象")
        unknown = sorted(set(loaded) - set(Settings.__dataclass_fields__))
        if unknown:
            raise ConfigError(f"配置文件 {path} 包含未知字段: {', '.join(unknown)}")
        data.update(loaded)

    env_mapping = {
        "WM_HOST": "host",
        "WM_PORT": "port",
        "WM_SAMPLE_INTERVAL_SECONDS": "sample_interval_seconds",
        "WM_HISTORY_MINUTES": "history_minutes",
        "WM_COMMAND_TIMEOUT_SECONDS": "command_timeout_seconds",
        "WM_CRITICAL_PORTS": "critical_ports",
        "WM_DATABASE_PATH": "database_path",
        "WM_SESSION_TTL_SECONDS": "session_ttl_seconds",
        "WM_COOKIE_SECURE": "cookie_secure",
        "WM_REQUEST_BODY_MAX_BYTES": "request_body_max_bytes",
        "WM_AUTH_CONCURRENCY_LIMIT": "auth_concurrency_limit",
        "WM_SESSION_MAX_ACTIVE": "session_max_active",
        "WM_AUDIT_RETENTION_MAX_EVENTS": "audit_retention_max_events",
        "WM_AUDIT_RETENTION_DAYS": "audit_retention_days",
        "WM_LOGIN_FAILURE_MAX_ROWS": "login_failure_max_rows",
        "WM_OPERATION_RETENTION_MAX": "operation_retention_max",
        "WM_SCRIPT_STATUS_TIMEOUT_SECONDS": "script_status_timeout_seconds",
        "WM_SCRIPT_ACTION_TIMEOUT_SECONDS": "script_action_timeout_seconds",
        "WM_MANAGER_LOG_PATH": "manager_log_path",
        "WM_MANAGER_LOG_LEVEL": "manager_log_level",
        "WM_MANAGER_LOG_MAX_BYTES": "manager_log_max_bytes",
        "WM_MANAGER_LOG_BACKUP_COUNT": "manager_log_backup_count",
        "WM_SETUP_DISABLED": "setup_disabled",
        "WM_ALLOWED_PUBLIC_ORIGINS": "allowed_public_origins",
        "WM_TRUSTED_PROXY_IPS": "trusted_proxy_ips",
    }
    for env_name, key in env_mapping.items():
        if env_name in env:
            data[key] = env[env_name]

    host = str(data.get("host", "127.0.0.1")).strip()
    if not host:
        raise ConfigError("host 不能为空")
    history_minutes = _bounded_integer(
        data.get("history_minutes", 15),
        "history_minutes",
        MIN_HISTORY_MINUTES,
        MAX_HISTORY_MINUTES,
    )
    return Settings(
        host=host,
        port=_port(data.get("port", 19100), "port"),
        sample_interval_seconds=_bounded_number(
            data.get("sample_interval_seconds", 5),
            "sample_interval_seconds",
            MIN_SAMPLE_INTERVAL_SECONDS,
            MAX_SAMPLE_INTERVAL_SECONDS,
        ),
        history_minutes=history_minutes,
        command_timeout_seconds=_bounded_number(
            data.get("command_timeout_seconds", 4),
            "command_timeout_seconds",
            MIN_COMMAND_TIMEOUT_SECONDS,
            MAX_COMMAND_TIMEOUT_SECONDS,
        ),
        critical_ports=_ports(data.get("critical_ports", DEFAULT_PORTS)),
        database_path=_path(
            data.get("database_path", PROJECT_ROOT / "data" / "workstation-manager.db"),
            "database_path",
        ),
        session_ttl_seconds=_bounded_integer(
            data.get("session_ttl_seconds", 12 * 60 * 60),
            "session_ttl_seconds",
            300,
            30 * 24 * 60 * 60,
        ),
        cookie_secure=_boolean(data.get("cookie_secure", False), "cookie_secure"),
        request_body_max_bytes=_bounded_integer(
            data.get("request_body_max_bytes", 64 * 1024), "request_body_max_bytes", 1024, 10 * 1024 * 1024
        ),
        auth_concurrency_limit=_bounded_integer(
            data.get("auth_concurrency_limit", 2), "auth_concurrency_limit", 1, 16
        ),
        session_max_active=_bounded_integer(
            data.get("session_max_active", 64), "session_max_active", 1, 1024
        ),
        audit_retention_max_events=_bounded_integer(
            data.get("audit_retention_max_events", 10_000), "audit_retention_max_events", 100, 1_000_000
        ),
        audit_retention_days=_bounded_integer(
            data.get("audit_retention_days", 90), "audit_retention_days", 1, 3650
        ),
        login_failure_max_rows=_bounded_integer(
            data.get("login_failure_max_rows", 10_000), "login_failure_max_rows", 100, 1_000_000
        ),
        operation_retention_max=_bounded_integer(
            data.get("operation_retention_max", 1000), "operation_retention_max", 100, 100_000
        ),
        script_status_timeout_seconds=_bounded_number(
            data.get("script_status_timeout_seconds", 3),
            "script_status_timeout_seconds", 0.1, 60,
        ),
        script_action_timeout_seconds=_bounded_number(
            data.get("script_action_timeout_seconds", 600),
            "script_action_timeout_seconds", 1, 24 * 60 * 60,
        ),
        manager_log_path=_local_log_path(
            data.get("manager_log_path", PROJECT_ROOT / "logs" / "manager.log"),
        ),
        manager_log_level=_log_level(data.get("manager_log_level", "INFO")),
        manager_log_max_bytes=_bounded_integer(
            data.get("manager_log_max_bytes", 5 * 1024 * 1024),
            "manager_log_max_bytes", 64 * 1024, 1024 * 1024 * 1024,
        ),
        manager_log_backup_count=_bounded_integer(
            data.get("manager_log_backup_count", 5),
            "manager_log_backup_count", 1, 100,
        ),
        setup_disabled=_boolean(data.get("setup_disabled", False), "setup_disabled"),
        allowed_public_origins=_allowed_public_origins(data.get("allowed_public_origins", ())),
        trusted_proxy_ips=_trusted_proxy_ips(data.get("trusted_proxy_ips", ())),
    )
