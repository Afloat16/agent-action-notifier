"""Map selected public hook payloads. Never read a transcript or return a decision."""

import hashlib
import json
import time

from .models import Event
from .privacy import ValidationError
from .store import Store


def ingest_hook(store: Store, source: str, payload: object, channels: tuple[str, ...],
                allowed_hosts: tuple[str, ...], include_summary: bool = False,
                now: float | None = None) -> dict:
    if source not in {"codex", "claude"} or not isinstance(payload, dict):
        raise ValidationError("invalid_hook_input")
    now = time.time() if now is None else now
    session = payload.get("session_id")
    hook = payload.get("hook_event_name")
    if not isinstance(session, str) or not session or len(session) > 256:
        raise ValidationError("missing_hook_session")
    if not isinstance(hook, str):
        raise ValidationError("missing_hook_event")
    task_id = source + ":" + hashlib.sha256(session.encode()).hexdigest()[:24]
    tool = payload.get("tool_name", "")
    if not isinstance(tool, str) or len(tool) > 128:
        raise ValidationError("invalid_hook_tool")
    is_question = source == "claude" and hook == "PreToolUse" and tool == "AskUserQuestion"
    is_permission = hook == "PermissionRequest"
    if is_permission or is_question:
        producer_id = payload.get("tool_use_id")
        if producer_id is not None and (not isinstance(producer_id, str) or len(producer_id) > 256):
            raise ValidationError("invalid_hook_request_id")
        if producer_id:
            fingerprint = json.dumps([source, session, hook, producer_id], separators=(",", ":"))
        else:
            # Hooks lacking a producer request ID cannot give authoritative
            # pending-request tracking. A bounded window prevents permanently
            # suppressing a later, similar approval. Tool inputs are hashed
            # locally, never stored, echoed, or included in a notification.
            fingerprint = json.dumps([source, session, hook, tool, payload.get("tool_input", {}),
                                      int(now // 30)], sort_keys=True, separators=(",", ":"))
        ref = hashlib.sha256(fingerprint.encode()).hexdigest()[:32]
        event = Event.parse({"version": 1, "event_id": "hook:" + ref, "task_id": task_id,
                             "type": "human_input_required", "request_id": "hook:" + ref,
                             "summary": "The agent needs a human answer." if is_question
                             else "The agent needs human permission.", "status": "waiting"}, allowed_hosts)
    elif hook == "Stop":
        current = store.task(task_id)
        # Stop means the turn ended, not that the user's task is complete.
        status = current["status"] if current and current["status"] in {"waiting", "blocked"} else "unknown"
        ref = hashlib.sha256(json.dumps([source, session, "Stop", payload.get("turn_id", ""),
                                        int(now // 30)], separators=(",", ":")).encode()).hexdigest()[:32]
        event = Event.parse({"version": 1, "event_id": "hook:" + ref, "task_id": task_id,
                             "type": "task_status", "status": status,
                             "summary": "An agent turn ended; task completion has not been confirmed."}, allowed_hosts)
    else:
        return {"accepted": True, "ignored": True, "queued": 0}
    result = store.emit(event, channels, include_summary, now)
    result.update(task_id=event.task_id, request_id=event.request_id)
    return result
