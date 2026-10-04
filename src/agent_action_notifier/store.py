"""SQLite transactionally couples state changes with an at-least-once outbox."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from .models import Event, TERMINAL
from .privacy import ValidationError, reference

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
 event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, received_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
 task_id TEXT PRIMARY KEY, status TEXT NOT NULL, independent_work INTEGER NOT NULL,
 summary TEXT NOT NULL, action_url TEXT NOT NULL, observed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
 task_id TEXT NOT NULL, request_id TEXT NOT NULL, state TEXT NOT NULL,
 revision INTEGER NOT NULL, summary TEXT NOT NULL, action_url TEXT NOT NULL,
 opened_at REAL NOT NULL, updated_at REAL NOT NULL, outcome TEXT NOT NULL,
 PRIMARY KEY(task_id, request_id));
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL, task_id TEXT NOT NULL,
 request_id TEXT NOT NULL, channel TEXT NOT NULL, title TEXT NOT NULL,
 body TEXT NOT NULL, message_id TEXT NOT NULL, state TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
 lease_until REAL NOT NULL DEFAULT 0, lease_owner TEXT NOT NULL DEFAULT '',
 last_error TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, UNIQUE(dedupe_key, channel));
CREATE INDEX IF NOT EXISTS delivery_due ON outbox(state, next_attempt, lease_until);
PRAGMA user_version = 1;
"""


def render(event: Event, include_summary: bool, observed_at: float | None = None) -> tuple[str, str]:
    task_ref = reference(event.task_id)
    if event.type == "human_input_required":
        title = "Human input needed"
        lines = [f"Task reference: {task_ref}", f"Status: {event.status}",
                 "Work requiring your input is paused."]
        if event.independent_work:
            lines.append("The producer reports independent authorized work is continuing.")
        else:
            lines.append("No independent work has been reported as continuing.")
        lines.append("Review and respond in the original agent or approval interface.")
    elif event.type == "request_resolved":
        title = "Human-input request cleared"
        lines = [f"Task reference: {task_ref}", "The producer reports this request is cleared.",
                 f"Outcome: {event.outcome}", "This notice does not prove work has resumed."]
    else:
        title = f"Agent task: {event.status}"
        lines = [f"Task reference: {task_ref}", f"Producer-reported status: {event.status}"]
        if event.status == "unknown":
            lines.append("No current running or completed state has been confirmed.")
        if event.independent_work:
            lines.append("The producer reports independent authorized work is continuing.")
    if observed_at is not None:
        timestamp = datetime.fromtimestamp(observed_at, timezone.utc).isoformat(timespec="seconds")
        lines.insert(1, f"Reported at UTC: {timestamp}")
        lines.append("This is a point-in-time report, not proof of current activity.")
    if include_summary and event.summary:
        lines.append(f"Summary (heuristically redacted): {event.summary}")
    if event.action_url:
        lines.append(f"Review link: {event.action_url}")
    lines.append("Notifications cannot grant permission or bypass approval.")
    return title, "\n".join(lines)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or self.path.is_symlink():
                raise ValidationError("unsafe_database_path") from None
            if os.name != "nt" and info.st_mode & 0o077:
                raise ValidationError("database_permissions_too_open") from None
        else:
            os.close(fd)
        with self.connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValidationError("unsupported_database_version")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _cancel_request(conn, task_id: str, request_id: str):
        conn.execute("UPDATE outbox SET state='superseded' WHERE task_id=? AND request_id=? "
                     "AND state IN ('pending','processing','dead')", (task_id, request_id))

    def emit(self, event: Event, channels: tuple[str, ...], include_summary: bool = False,
             now: float | None = None) -> dict:
        now = time.time() if now is None else now
        payload = json.dumps(event.dictionary(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT digest FROM events WHERE event_id=?", (event.event_id,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ValidationError("event_id_conflict")
                return {"accepted": True, "duplicate": True, "queued": 0}
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (event.task_id,)).fetchone()
            notify, key = True, event.event_id
            notification_targets = None
            if event.type == "human_input_required":
                if task and task["status"] in TERMINAL:
                    raise ValidationError("task_already_terminal")
                old = conn.execute("SELECT * FROM requests WHERE task_id=? AND request_id=?",
                                   (event.task_id, event.request_id)).fetchone()
                if old and old["state"] != "pending":
                    raise ValidationError("request_already_resolved")
                unchanged = (old and old["summary"] == event.summary and old["action_url"] == event.action_url
                             and task["status"] == event.status
                             and bool(task["independent_work"]) == event.independent_work)
                revision = old["revision"] if unchanged else (old["revision"] + 1 if old else 1)
                notify = not unchanged
                if old and notify:
                    self._cancel_request(conn, event.task_id, event.request_id)
                conn.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?) "
                             "ON CONFLICT(task_id,request_id) DO UPDATE SET revision=excluded.revision,"
                             "summary=excluded.summary,action_url=excluded.action_url,updated_at=excluded.updated_at",
                             (event.task_id, event.request_id, "pending", revision, event.summary,
                              event.action_url, old["opened_at"] if old else now, now, ""))
                key = json.dumps(["request", event.task_id, event.request_id, revision], separators=(",", ":"))
                status, independent = event.status, event.independent_work
                summary, action_url = event.summary, event.action_url
            elif event.type == "request_resolved":
                old = conn.execute("SELECT * FROM requests WHERE task_id=? AND request_id=?",
                                   (event.task_id, event.request_id)).fetchone()
                if not old:
                    raise ValidationError("unknown_request")
                if old["state"] == "resolved":
                    if old["outcome"] != event.outcome:
                        raise ValidationError("resolution_conflict")
                    conn.execute("INSERT INTO events VALUES (?,?,?)", (event.event_id, digest, now))
                    conn.commit()
                    return {"accepted": True, "duplicate": False, "queued": 0}
                else:
                    self._cancel_request(conn, event.task_id, event.request_id)
                    conn.execute("UPDATE requests SET state='resolved',outcome=?,updated_at=? "
                                 "WHERE task_id=? AND request_id=?",
                                 (event.outcome, now, event.task_id, event.request_id))
                pending = conn.execute("SELECT count(*) FROM requests WHERE task_id=? AND state='pending'",
                                       (event.task_id,)).fetchone()[0]
                status = task["status"] if pending else "unknown"
                independent = bool(task["independent_work"]) if pending else False
                summary, action_url = "", ""
                key = json.dumps(["resolved", event.task_id, event.request_id], separators=(",", ":"))
            else:
                pending = conn.execute("SELECT count(*) FROM requests WHERE task_id=? AND state='pending'",
                                       (event.task_id,)).fetchone()[0]
                if pending and event.status not in {"waiting", "blocked"}:
                    raise ValidationError("pending_human_requests")
                if task and task["status"] in TERMINAL and event.status != task["status"]:
                    raise ValidationError("task_already_terminal")
                notify = not (task and task["status"] == event.status
                              and bool(task["independent_work"]) == event.independent_work
                              and task["summary"] == event.summary and task["action_url"] == event.action_url)
                status, independent = event.status, event.independent_work
                summary, action_url = event.summary, event.action_url
            if (event.type in {"task_status", "human_input_required"} and task
                    and (task["status"] != status or bool(task["independent_work"]) != independent)):
                # Any task-wide state change must refresh other pending request
                # notices too, including a second request changing global state.
                notification_targets = [(event, key)] if event.type == "human_input_required" else []
                for request in conn.execute("SELECT * FROM requests WHERE task_id=? AND state='pending'",
                                            (event.task_id,)).fetchall():
                    if event.type == "human_input_required" and request["request_id"] == event.request_id:
                        continue
                    self._cancel_request(conn, event.task_id, request["request_id"])
                    revision = request["revision"] + 1
                    conn.execute("UPDATE requests SET revision=?,updated_at=? WHERE task_id=? AND request_id=?",
                                 (revision, now, event.task_id, request["request_id"]))
                    refreshed = Event(1, event.event_id, event.task_id, "human_input_required",
                                      request["request_id"], request["summary"], request["action_url"],
                                      status, independent)
                    request_key = json.dumps(["request", event.task_id, request["request_id"], revision],
                                             separators=(",", ":"))
                    notification_targets.append((refreshed, request_key))
            if notify:
                # Never deliver an obsolete queued "running" notice after a
                # newer waiting/terminal state has already been reported.
                conn.execute("UPDATE outbox SET state='superseded' WHERE task_id=? AND request_id='' "
                             "AND state IN ('pending','processing','dead')", (event.task_id,))
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                         "status=excluded.status,independent_work=excluded.independent_work,"
                         "summary=excluded.summary,action_url=excluded.action_url,observed_at=excluded.observed_at",
                         (event.task_id, status, independent, summary, action_url, now))
            conn.execute("INSERT INTO events VALUES (?,?,?)", (event.event_id, digest, now))
            queued = 0
            if notify:
                for notice_event, notice_key in notification_targets or [(event, key)]:
                    title, body = render(notice_event, include_summary, now)
                    for channel in channels:
                        message_id = f"<{hashlib.sha256((notice_key + ':' + channel).encode()).hexdigest()}@agent-action-notifier.invalid>"
                        # Only a duplicate key/channel may be ignored. Invalid
                        # NOT NULL data must fail and roll back the transaction.
                        cur = conn.execute("INSERT INTO outbox "
                                           "(dedupe_key,task_id,request_id,channel,title,body,message_id,state,next_attempt,created_at) "
                                           "VALUES (?,?,?,?,?,?,?,'pending',?,?) ON CONFLICT(dedupe_key,channel) DO NOTHING",
                                           (notice_key, notice_event.task_id, notice_event.request_id, channel,
                                            title, body, message_id, now, now))
                        queued += cur.rowcount
            conn.commit()
            return {"accepted": True, "duplicate": False, "queued": queued}

    def task(self, task_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def snapshot(self, now: float | None = None, stale_after: float = 300) -> dict:
        now = time.time() if now is None else now
        with self.connection() as conn:
            tasks = []
            for row in conn.execute("SELECT * FROM tasks ORDER BY observed_at DESC,task_id"):
                task = dict(row)
                task["independent_work"] = bool(task["independent_work"])
                task["stale"] = (task["status"] not in TERMINAL and now - task["observed_at"] > stale_after)
                task["pending_requests"] = [dict(r) for r in conn.execute(
                    "SELECT request_id,revision,summary,action_url,opened_at,updated_at FROM requests "
                    "WHERE task_id=? AND state='pending' ORDER BY opened_at,request_id", (task["task_id"],))]
                tasks.append(task)
            deliveries = [dict(r) for r in conn.execute(
                "SELECT id,channel,state,attempts,next_attempt,last_error,evidence FROM outbox ORDER BY id DESC LIMIT 100")]
            totals = {r[0]: r[1] for r in conn.execute("SELECT state,count(*) FROM outbox GROUP BY state")}
        return {"tasks": tasks, "deliveries": deliveries, "delivery_totals": totals,
                "note": "States are producer-reported; stale running state is not proof of live work."}

    def claim(self, owner: str, now: float, lease_seconds: float = 60) -> dict | None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM outbox WHERE (state='pending' AND next_attempt<=?) "
                               "OR (state='processing' AND lease_until<=?) ORDER BY id LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE outbox SET state='processing',attempts=attempts+1,lease_until=?,lease_owner=? WHERE id=?",
                         (now + lease_seconds, owner, row["id"]))
            result = dict(row)
            result.update(state="processing", attempts=row["attempts"] + 1, lease_owner=owner)
            conn.commit()
            return result

    def active(self, delivery_id: int, owner: str) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM outbox WHERE id=? AND state='processing' AND lease_owner=?",
                                (delivery_id, owner)).fetchone() is not None

    def finish(self, delivery_id: int, owner: str, *, state: str, now: float,
               error: str = "", evidence: str = "") -> bool:
        with self.connection() as conn:
            cur = conn.execute("UPDATE outbox SET state=?,next_attempt=?,last_error=?,evidence=?,lease_until=0,lease_owner='' "
                               "WHERE id=? AND state='processing' AND lease_owner=?",
                               (state, now, error, evidence, delivery_id, owner))
            conn.commit()
            return cur.rowcount == 1

    def renew(self, delivery_id: int, owner: str, now: float, lease_seconds: float = 60) -> bool:
        with self.connection() as conn:
            cur = conn.execute("UPDATE outbox SET lease_until=? WHERE id=? AND state='processing' AND lease_owner=?",
                               (now + lease_seconds, delivery_id, owner))
            conn.commit()
            return cur.rowcount == 1

    def retry_dead(self) -> int:
        with self.connection() as conn:
            cur = conn.execute("UPDATE outbox SET state='pending',attempts=0,next_attempt=?,last_error='',"
                               "lease_until=0,lease_owner='' WHERE state='dead'", (time.time(),))
            conn.commit()
            return cur.rowcount
