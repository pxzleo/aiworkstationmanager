from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SUPPORTED_SUFFIXES = {".cmd", ".bat", ".ps1", ".lnk"}
SCRIPT_SUFFIXES = {".cmd", ".bat", ".ps1"}
SENSITIVE_KEY_PATTERN = (
    r"(?:api[-_]?key|auth[-_]?key|access[-_]?token|refresh[-_]?token|"
    r"client[-_]?secret|private[-_]?key|secret[-_]?key|"
    r"set[-_]?cookie|cookies?|"
    r"(?:[a-z0-9]+[-_])+(?:key|token|secret|password)|"
    r"[a-z0-9]*(?:api|auth|access|refresh|client)(?:key|token|secret)|"
    r"key|token|secret|password|passwd|pwd|credentials?)"
)


class DiscoveryError(RuntimeError):
    """脚本目录扫描失败。"""


class DiscoveryLimitError(DiscoveryError):
    """发现资源超出已配置上限。"""


LinkReader = Callable[[Path, float], dict[str, str]]


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("扫描总时长已用尽")


def _error(exc: Exception, message: str) -> dict[str, str]:
    sanitized_cause, _ = redact_sensitive_text(str(exc))
    sanitized_message, _ = redact_sensitive_text(message)
    return {
        "error_type": type(exc).__name__,
        "message": sanitized_message,
        "cause": sanitized_cause,
    }


def _read_bounded_file(
    path: Path, max_bytes: int, deadline: float | None = None
) -> tuple[bytes, os.stat_result]:
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        if stat.st_size > max_bytes:
            raise DiscoveryLimitError(
                f"文件大小 {stat.st_size} 字节超过上限 {max_bytes} 字节"
            )
        while True:
            _check_deadline(deadline)
            block = stream.read(min(1024 * 1024, max_bytes - total + 1))
            _check_deadline(deadline)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise DiscoveryLimitError(f"文件读取量超过上限 {max_bytes} 字节")
            chunks.append(block)
    return b"".join(chunks), stat


def _sha256_bytes(data: bytes, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    for offset in range(0, len(data), 1024 * 1024):
        _check_deadline(deadline)
        digest.update(data[offset : offset + 1024 * 1024])
    _check_deadline(deadline)
    return digest.hexdigest()


def _decode_script(data: bytes, deadline: float | None = None) -> tuple[str, str]:
    encodings = ("utf-8-sig", "utf-16", "gb18030")
    failures: list[str] = []
    for encoding in encodings:
        _check_deadline(deadline)
        try:
            text = data.decode(encoding)
            _check_deadline(deadline)
            return text, encoding
        except UnicodeError as exc:
            failures.append(f"{encoding}: {exc}")
    raise UnicodeError("已尝试常见 Windows 编码仍无法解码: " + "; ".join(failures))


def read_script_text(path: Path, max_bytes: int = 4 * 1024 * 1024) -> tuple[str, str]:
    data, _ = _read_bounded_file(path, max_bytes)
    return _decode_script(data)


def _unique(values: list[Any]) -> list[Any]:
    return list(dict.fromkeys(values))


def redact_sensitive_text(value: str) -> tuple[str, bool]:
    substitutions = (
        (
            r"(?im)(\b(?:set-cookie|cookie)\s*:\s*).*?$",
            r"\1<redacted>",
        ),
        (
            r"(?i)(\bauthorization\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?)(?:\"[^\"]*\"|'[^']*'|[^\s;&]+)",
            r"\1<redacted>",
        ),
        (
            rf"(?i)([\"']{SENSITIVE_KEY_PATTERN}[\"']\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
            r"\1<redacted>",
        ),
        (rf"(?i)([\\/]{SENSITIVE_KEY_PATTERN}=)([^\\/\s]+)", r"\1<redacted>"),
        (
            rf"(?i)(?<![a-z0-9_-])((?:--?)?{SENSITIVE_KEY_PATTERN})(?:\s*[=:]\s*|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s;&\\/]+)",
            r"\1=<redacted>",
        ),
        (rf"(?i)([?&]{SENSITIVE_KEY_PATTERN}=)([^&\s]+)", r"\1<redacted>"),
        (r"(?i)(https?://)([^/@\s]+)@", r"\1<redacted>@"),
        (r"(\\\\)([^\\\s@]+:[^\\\s@]+)@", r"\1<redacted>@"),
    )
    redacted = value
    detected = False
    for pattern, replacement in substitutions:
        redacted, count = re.subn(pattern, replacement, redacted)
        detected = detected or count > 0
    return redacted, detected


def redact_discovery_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if isinstance(key, str) and re.fullmatch(SENSITIVE_KEY_PATTERN, key, re.IGNORECASE) else redact_discovery_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_discovery_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_discovery_value(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_text(value)[0]
    return value


def parse_script(text: str) -> dict[str, Any]:
    ports: list[int] = []
    for pattern in (
        r"https?://[^\s'\"]+:(\d{1,5})\b",
        r"(?:--port|--api-port|--listen-port|PORT\s*=)\s*[=:]?\s*(\d{1,5})\b",
        r"(?:^|\s)-p\s+(?:\d{1,3}(?:\.\d{1,3}){3}:)?(\d{1,5})(?::\d{1,5})?\b",
        r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{1,5})\b",
    ):
        ports.extend(int(value) for value in re.findall(pattern, text, flags=re.IGNORECASE | re.MULTILINE))
    ports = [port for port in _unique(ports) if 1 <= port <= 65535]

    wsl_distributions = _unique(
        re.findall(
            r"\bwsl(?:\.exe)?\s+(?:--distribution|-d)\s+[\"']?([^\s\"']+)",
            text,
            flags=re.IGNORECASE,
        )
    )
    working_directories = _unique(
        match.strip().strip('"\'')
        for match in re.findall(
            r"(?:^|[;&]\s*|\b)(?:cd|pushd)\s+(?:/d\s+)?([^\r\n;&]+)",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if match.strip()
    )
    service_names = _unique(
        re.findall(
            r"\bsystemctl(?:\s+--user)?\s+(?:start|stop|restart|status|is-active)\s+([\w@.:-]+)",
            text,
            flags=re.IGNORECASE,
        )
    )
    compose_files = _unique(
        re.findall(r"(?:--file|-f)\s+[\"']?([^\s\"']+ya?ml)\b", text, flags=re.IGNORECASE)
    )
    docker_compose = bool(re.search(r"\bdocker(?:\.exe)?\s+compose\b|\bdocker-compose\b", text, re.I))
    gpu_devices = _unique(
        re.findall(
            r"(?:CUDA_VISIBLE_DEVICES|NVIDIA_VISIBLE_DEVICES)\s*[=:]\s*[\"']?([\w,.-]+)",
            text,
            flags=re.IGNORECASE,
        )
        + re.findall(r"GPU-[0-9a-f-]{12,}", text, flags=re.IGNORECASE)
        + re.findall(r"--gpus(?:=|\s+)['\"]?device=([\w,.-]+)", text, flags=re.IGNORECASE)
    )
    raw_urls = _unique(re.findall(r"https?://[^\s'\"<>]+", text, flags=re.IGNORECASE))
    urls: list[str] = []
    sensitive_values_detected = False
    for url in raw_urls:
        sanitized, detected = redact_sensitive_text(url)
        urls.append(sanitized)
        sensitive_values_detected = sensitive_values_detected or detected
    _, text_contains_sensitive_values = redact_sensitive_text(text)
    sensitive_values_detected = sensitive_values_detected or text_contains_sensitive_values
    webui_candidates = [url for url in urls if re.search(r"ui|web|monitor|8081|18031|8765", url, re.I)]
    api_candidates = [
        url for url in urls if url not in webui_candidates or re.search(r"api|health|v1|metrics", url, re.I)
    ]
    return {
        "ports": ports,
        "wsl_distributions": wsl_distributions,
        "working_directories": working_directories,
        "service_names": service_names,
        "docker_compose": docker_compose,
        "docker_compose_files": compose_files,
        "gpu_devices": gpu_devices,
        "webui_candidates": webui_candidates,
        "api_candidates": api_candidates,
        "interactive": bool(re.search(r"\b(?:choice|pause|set\s+/p|Read-Host)\b", text, re.I)),
        "sensitive_values_detected": sensitive_values_detected,
    }


def read_shortcut(path: Path, timeout: float) -> dict[str, str]:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if executable is None:
        raise FileNotFoundError("未找到 PowerShell，无法解析快捷方式")
    fixed_query = (
        "& { "
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:WM_LNK_PATH); "
        "[pscustomobject]@{TargetPath=$s.TargetPath;Arguments=$s.Arguments;"
        "WorkingDirectory=$s.WorkingDirectory} | ConvertTo-Json -Compress }"
    )
    command_env = {
        key: value
        for key in ("SystemRoot", "WINDIR", "TEMP", "TMP", "COMSPEC")
        if (value := os.environ.get(key))
    }
    command_env["WM_LNK_PATH"] = str(path)
    completed = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", fixed_query],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=command_env,
    )
    if completed.returncode != 0:
        sanitized_stderr, _ = redact_sensitive_text(
            completed.stderr.strip() or "无错误输出"
        )
        sanitized_stdout, _ = redact_sensitive_text(completed.stdout.strip())
        output_detail = f"; 输出: {sanitized_stdout}" if sanitized_stdout else ""
        raise DiscoveryError(
            f"PowerShell 解析快捷方式失败（退出码 {completed.returncode}）: "
            f"{sanitized_stderr}{output_detail}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"PowerShell 返回的快捷方式元数据不是有效 JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise DiscoveryError("快捷方式元数据必须是 JSON 对象")
    return {
        "target_path": str(result.get("TargetPath") or ""),
        "arguments": str(result.get("Arguments") or ""),
        "working_directory": str(result.get("WorkingDirectory") or ""),
    }


class ScriptDiscovery:
    def __init__(
        self,
        directory: Path,
        timeout: float,
        link_reader: LinkReader = read_shortcut,
        *,
        max_file_bytes: int = 4 * 1024 * 1024,
        max_entries: int = 512,
        max_shortcuts: int = 64,
        total_timeout_seconds: float = 30.0,
    ) -> None:
        self.directory = Path(directory)
        self.timeout = timeout
        self.link_reader = link_reader
        self.max_file_bytes = max_file_bytes
        self.max_entries = max_entries
        self.max_shortcuts = max_shortcuts
        self.total_timeout_seconds = total_timeout_seconds

    def scan(self) -> dict[str, Any]:
        started = time.monotonic()
        deadline = started + self.total_timeout_seconds
        scan_id = uuid.uuid4().hex
        scanned_at = datetime.now(timezone.utc).isoformat()
        if not self.directory.exists():
            errors: list[dict[str, str]] = []
            if time.monotonic() >= deadline:
                errors.append(
                    _error(TimeoutError("扫描总时长已用尽"), "脚本目录状态检查超时")
                )
            return redact_discovery_value({
                "scan_id": scan_id,
                "scanned_at": scanned_at,
                "directory": str(self.directory.absolute()),
                "directory_exists": False,
                "entries": [],
                "errors": errors,
            })
        try:
            paths: list[Path] = []
            entry_limit_exceeded = False
            enumeration_timed_out = False
            enumerated_count = 0
            for path in self.directory.iterdir():
                if time.monotonic() >= deadline:
                    enumeration_timed_out = True
                    break
                enumerated_count += 1
                if enumerated_count > self.max_entries:
                    entry_limit_exceeded = True
                    break
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                paths.append(path)
            paths.sort(key=lambda item: item.name.lower())
        except OSError as exc:
            raise DiscoveryError(f"无法枚举脚本目录 {self.directory}: {exc}") from exc
        errors: list[dict[str, str]] = []
        if entry_limit_exceeded:
            errors.append(
                _error(
                    DiscoveryLimitError(f"条目数超过上限 {self.max_entries}"),
                    "脚本条目数超限",
                )
            )
        if enumeration_timed_out:
            errors.append(_error(TimeoutError("扫描总时长已用尽"), "脚本目录枚举超时"))
        entries: list[dict[str, Any]] = []
        shortcut_count = 0
        for path in paths:
            if deadline - time.monotonic() <= 0:
                errors.append(_error(TimeoutError("扫描总时长已用尽"), "脚本扫描超时"))
                break
            is_shortcut = path.suffix.lower() == ".lnk"
            shortcut_allowed = not is_shortcut or shortcut_count < self.max_shortcuts
            if is_shortcut:
                shortcut_count += 1
            entries.append(self._inspect(path, shortcut_allowed, deadline))
        if time.monotonic() >= deadline and not any(
            error.get("error_type") == "TimeoutError" for error in errors
        ):
            errors.append(_error(TimeoutError("扫描总时长已用尽"), "脚本扫描超时"))
        return redact_discovery_value({
            "scan_id": scan_id,
            "scanned_at": scanned_at,
            "directory": str(self.directory.resolve()),
            "directory_exists": True,
            "entries": entries,
            "errors": errors,
        })

    def _inspect(
        self, path: Path, shortcut_allowed: bool, deadline: float
    ) -> dict[str, Any]:
        resolved = path.resolve()
        entry: dict[str, Any] = {
            "name": path.name,
            "type": path.suffix.lower().lstrip("."),
            "path": str(resolved),
            "mtime": "",
            "sha256": "",
            "errors": [],
        }
        try:
            data, stat = _read_bounded_file(path, self.max_file_bytes, deadline)
            entry["mtime"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            entry["sha256"] = _sha256_bytes(data, deadline)
        except (OSError, DiscoveryLimitError, TimeoutError) as exc:
            entry["errors"].append(_error(exc, "脚本元数据或校验值读取失败"))
            return entry
        if path.suffix.lower() in SCRIPT_SUFFIXES:
            self._parse_script_bytes(data, entry, deadline)
            return entry
        if not shortcut_allowed:
            entry["errors"].append(
                _error(
                    DiscoveryLimitError(f"快捷方式数超过上限 {self.max_shortcuts}"),
                    "快捷方式数超限",
                )
            )
            return entry
        try:
            link_timeout = min(self.timeout, deadline - time.monotonic())
            if link_timeout <= 0:
                raise TimeoutError("扫描总时长已用尽")
            raw_shortcut = self.link_reader(path, link_timeout)
            if time.monotonic() >= deadline:
                raise TimeoutError("扫描总时长已用尽")
            raw_target_path = str(raw_shortcut.get("target_path", ""))
            shortcut: dict[str, Any] = {}
            sensitive_values_detected = False
            for key in ("target_path", "arguments", "working_directory"):
                sanitized, detected = redact_sensitive_text(str(raw_shortcut.get(key, "")))
                shortcut[key] = sanitized
                sensitive_values_detected = sensitive_values_detected or detected
            shortcut["sensitive_values_detected"] = bool(
                raw_shortcut.get("sensitive_values_detected") or sensitive_values_detected
            )
            entry["shortcut"] = shortcut
            entry.update(
                {
                    "ports": [],
                    "wsl_distributions": [],
                    "working_directories": [shortcut["working_directory"]]
                    if shortcut.get("working_directory")
                    else [],
                    "service_names": [],
                    "docker_compose": False,
                    "docker_compose_files": [],
                    "gpu_devices": [],
                    "webui_candidates": [],
                    "api_candidates": [],
                    "interactive": False,
                    "sensitive_values_detected": shortcut["sensitive_values_detected"],
                }
            )
            # 原始路径仅用于本次只读定位；返回结构始终使用上面的脱敏副本。
            target = Path(raw_target_path)
            if target.suffix.lower() in SCRIPT_SUFFIXES and target.is_file():
                target_data, target_stat = _read_bounded_file(
                    target, self.max_file_bytes, deadline
                )
                target_entry: dict[str, Any] = {
                    "path": redact_sensitive_text(str(target.resolve()))[0],
                    "mtime": datetime.fromtimestamp(target_stat.st_mtime, timezone.utc).isoformat(),
                    "sha256": "",
                    "errors": [],
                }
                target_entry["sha256"] = _sha256_bytes(target_data, deadline)
                self._parse_script_bytes(target_data, target_entry, deadline)
                entry["target_script"] = target_entry
                for key in (
                    "ports",
                    "wsl_distributions",
                    "service_names",
                    "docker_compose_files",
                    "gpu_devices",
                    "webui_candidates",
                    "api_candidates",
                ):
                    entry[key] = _unique(entry[key] + target_entry.get(key, []))
                entry["docker_compose"] = target_entry.get("docker_compose", False)
                entry["interactive"] = target_entry.get("interactive", False)
                entry["sensitive_values_detected"] = bool(
                    entry.get("sensitive_values_detected")
                    or target_entry.get("sensitive_values_detected")
                )
        except Exception as exc:
            entry["errors"].append(_error(exc, "快捷方式解析失败"))
            entry.setdefault("shortcut", None)
        return redact_discovery_value(entry)

    @staticmethod
    def _parse_script_bytes(
        data: bytes, entry: dict[str, Any], deadline: float | None = None
    ) -> None:
        try:
            text, encoding = _decode_script(data, deadline)
            entry["encoding"] = encoding
            parsed = parse_script(text)
            _check_deadline(deadline)
            entry.update(parsed)
        except (UnicodeError, TimeoutError) as exc:
            entry["errors"].append(_error(exc, "脚本文本读取或解码失败"))
            entry.update(
                {
                    "ports": [],
                    "wsl_distributions": [],
                    "working_directories": [],
                    "service_names": [],
                    "docker_compose": False,
                    "docker_compose_files": [],
                    "gpu_devices": [],
                    "webui_candidates": [],
                    "api_candidates": [],
                    "interactive": False,
                    "sensitive_values_detected": False,
                }
            )
