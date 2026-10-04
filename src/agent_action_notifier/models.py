from dataclasses import asdict, dataclass
import re

from .privacy import ValidationError, redact_text, safe_action_url

STATUSES = {"running", "waiting", "blocked", "completed", "failed", "cancelled", "unknown"}
TERMINAL = {"completed", "failed", "cancelled"}
TYPES = {"human_input_required", "request_resolved", "task_status"}
CHANNELS = {"email", "desktop", "stdout"}


def identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValidationError(code)
    return value


@dataclass(frozen=True)
class Event:
    version: int
    event_id: str
    task_id: str
    type: str
    request_id: str = ""
    summary: str = ""
    action_url: str = ""
    status: str = ""
    independent_work: bool = False
    outcome: str = ""

    @classmethod
    def parse(cls, value: object, allowed_hosts: tuple[str, ...]) -> "Event":
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValidationError("invalid_event_fields")
        if type(value.get("version")) is not int or value["version"] != 1:
            raise ValidationError("unsupported_event_version")
        event_id = identifier(value.get("event_id"), "invalid_event_id")
        task_id = identifier(value.get("task_id"), "invalid_task_id")
        kind = value.get("type")
        if not isinstance(kind, str) or kind not in TYPES:
            raise ValidationError("invalid_event_type")
        request_id = value.get("request_id", "")
        if not isinstance(request_id, str):
            raise ValidationError("invalid_request_id")
        if kind != "task_status":
            request_id = identifier(request_id, "invalid_request_id")
        elif request_id != "":
            raise ValidationError("unexpected_request_id")
        summary = value.get("summary", "")
        if not isinstance(summary, str) or len(summary) > 400:
            raise ValidationError("invalid_summary")
        action_url = value.get("action_url", "")
        if not isinstance(action_url, str):
            raise ValidationError("unsafe_action_url")
        action_url = safe_action_url(action_url, allowed_hosts)
        independent = value.get("independent_work", False)
        if type(independent) is not bool:
            raise ValidationError("invalid_independent_work")
        status = value.get("status", "waiting" if kind == "human_input_required" else "")
        outcome = value.get("outcome", "")
        if not isinstance(status, str) or not isinstance(outcome, str):
            raise ValidationError("invalid_state")
        if kind == "human_input_required" and status not in {"waiting", "blocked"}:
            raise ValidationError("request_must_wait")
        if kind == "task_status" and status not in STATUSES:
            raise ValidationError("invalid_task_status")
        if kind == "request_resolved":
            if outcome not in {"handled", "cancelled", "superseded"}:
                raise ValidationError("invalid_resolution_outcome")
            if status or independent or summary or action_url:
                raise ValidationError("unexpected_resolution_fields")
        elif outcome:
            raise ValidationError("unexpected_outcome")
        if independent and (status not in {"waiting", "blocked"}):
            raise ValidationError("unexpected_independent_work")
        return cls(1, event_id, task_id, kind, request_id, redact_text(summary), action_url,
                   status, independent, outcome)

    def dictionary(self) -> dict:
        return asdict(self)


def parse_channels(value: str) -> tuple[str, ...]:
    channels = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not channels or set(channels) - CHANNELS:
        raise ValidationError("invalid_channels")
    return channels
