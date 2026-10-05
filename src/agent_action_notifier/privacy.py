"""Conservative outbound minimization, not a general-purpose secret detector."""

import hashlib
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit


class ValidationError(ValueError):
    """Only a stable, non-sensitive error code may leave the input boundary."""


def reference(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def request_reference(task_id: str, request_id: str) -> str:
    value = json.dumps([task_id, request_id], separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def redact_text(value: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    value = re.sub(r"https?://\S+", "[link omitted]", value, flags=re.I)
    value = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "[email redacted]", value)
    value = re.sub(r"\bBearer\s+\S+", "Bearer [redacted]", value, flags=re.I)
    value = re.sub(
        r"\b(password|passwd|secret|token|api[_ -]?key|authorization)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)",
        r"\1=[redacted]", value, flags=re.I,
    )
    value = re.sub(r"\b(?:sk-|gh[opusr]_|github_pat_)[A-Za-z0-9_-]{8,}\b", "[redacted]", value)
    value = re.sub(r"\b[A-Za-z0-9_+/=-]{48,}\b", "[long value redacted]", value)
    return " ".join(value.split())


def safe_action_url(value: str, allowed_hosts: tuple[str, ...]) -> str:
    if not value:
        return ""
    if len(value) > 2048 or re.search(r"[\s\x00-\x1f\\]", value):
        raise ValidationError("unsafe_action_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValidationError("unsafe_action_url") from None
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)
            or parsed.query or parsed.fragment or host not in allowed_hosts):
        raise ValidationError("unsafe_action_url")
    # A review link must never be a bearer-token transport. IDs may still be
    # sensitive; callers must use ordinary, authenticated review pages.
    decoded_path = parsed.path
    for _ in range(3):
        decoded_path = unquote(decoded_path)
    if re.search(r"(?i)(?:token|password|secret|api[-_]?key)[=/]|(?:sk-|gh[pousr]_|github_pat_)", decoded_path):
        raise ValidationError("unsafe_action_url")
    return urlunsplit(("https", host, parsed.path or "/", "", ""))
