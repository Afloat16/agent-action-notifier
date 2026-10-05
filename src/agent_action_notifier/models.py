from dataclasses import asdict, dataclass
import re

from .privacy import ValidationError, redact_text, safe_action_url

STATUSES = {"running", "waiting", "blocked", "completed", "failed", "cancelled", "unknown"}
TERMINAL = {"completed", "failed", "cancelled"}
TYPES = {"human_input_required", "request_resolved", "task_status"}
CHANNELS = {"email", "desktop", "stdout"}
ACTION_KINDS = {"login", "question", "review", "external_action", "host_confirmation"}


def action_text(value: object, limit: int, code: str, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValidationError(code)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValidationError(code) from None
    value = redact_text(value)
    if required and not value:
        raise ValidationError(code)
    return value


@dataclass(frozen=True)
class Action:
    """Producer-reviewed instructions, never commands or authorization."""

    kind: str
    steps: tuple[str, ...]
    reply_prompt: str = ""
    completion_hint: str = ""

    @classmethod
    def parse(cls, value: object) -> "Action":
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValidationError("invalid_action_fields")
        kind = value.get("kind")
        if not isinstance(kind, str) or kind not in ACTION_KINDS:
            raise ValidationError("invalid_action_kind")
        steps = value.get("steps")
        if not isinstance(steps, (list, tuple)) or not 1 <= len(steps) <= 12:
            raise ValidationError("invalid_action_steps")
        return cls(kind, tuple(action_text(step, 500, "invalid_action_step", required=True)
                               for step in steps),
                   action_text(value.get("reply_prompt", ""), 400, "invalid_reply_prompt"),
                   action_text(value.get("completion_hint", ""), 400, "invalid_completion_hint"))


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
    action: Action | None = None

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
        action = Action.parse(value["action"]) if "action" in value else None
        if action is not None:
            if kind != "human_input_required":
                raise ValidationError("unexpected_action")
            if action.kind == "login" and not action_url:
                raise ValidationError("login_requires_official_action_url")
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
                   status, independent, outcome, action)

    def dictionary(self) -> dict:
        value = asdict(self)
        # Preserve v0.1 event digests when upgrading a database and retrying
        # an unchanged, previously stored event.
        if self.action is None:
            value.pop("action")
        return value


def parse_channels(value: str) -> tuple[str, ...]:
    channels = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not channels or set(channels) - CHANNELS:
        raise ValidationError("invalid_channels")
    return channels
