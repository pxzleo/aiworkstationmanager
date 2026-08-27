from __future__ import annotations

import re
from typing import Any


SENSITIVE_KEY_PATTERN = (
    r"(?:api[-_]?key|auth[-_]?key|access[-_]?token|refresh[-_]?token|"
    r"client[-_]?secret|private[-_]?key|secret[-_]?key|set[-_]?cookie|cookies?|"
    r"(?:[a-z0-9]+[-_])+(?:key|token|secret|password)|"
    r"[a-z0-9]*(?:api|auth|access|refresh|client)(?:key|token|secret)|"
    r"key|token|secret|password|passwd|pwd|credentials?)"
)


def redact_sensitive_text(value: str) -> tuple[str, bool]:
    substitutions = (
        (r"(?im)(\b(?:set-cookie|cookie)\s*:\s*).*?$", r"\1<redacted>"),
        (r"(?i)(\bauthorization\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?)(?:\"[^\"]*\"|'[^']*'|[^\s;&]+)", r"\1<redacted>"),
        (rf"(?i)([\"']{SENSITIVE_KEY_PATTERN}[\"']\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)", r"\1<redacted>"),
        (rf"(?i)([\\/]{SENSITIVE_KEY_PATTERN}=)([^\\/\s]+)", r"\1<redacted>"),
        (rf"(?i)(?<![a-z0-9_-])((?:--?)?{SENSITIVE_KEY_PATTERN})(?:\s*[=:]\s*|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s;&\\/]+)", r"\1=<redacted>"),
        (rf"(?i)([?&]{SENSITIVE_KEY_PATTERN}=)([^&\s]+)", r"\1<redacted>"),
        (r"(?i)(https?://)([^/@\s]+)@", r"\1<redacted>@"),
    )
    redacted = value
    detected = False
    for pattern, replacement in substitutions:
        redacted, count = re.subn(pattern, replacement, redacted)
        detected = detected or count > 0
    return redacted, detected


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if isinstance(key, str) and re.fullmatch(
                SENSITIVE_KEY_PATTERN, key, re.IGNORECASE
            ) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_text(value)[0]
    return value
