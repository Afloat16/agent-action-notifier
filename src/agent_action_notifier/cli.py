import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from . import __version__
from .hooks import ingest_hook
from .models import Event, parse_channels
from .privacy import ValidationError
from .server import MAX_BODY, serve
from .store import Store
from .worker import drain


def default_database() -> str:
    root = os.environ.get("XDG_STATE_HOME")
    return str(Path(root).expanduser() / "agent-action-notifier" / "state.db") if root else str(
        Path.home() / ".local" / "state" / "agent-action-notifier" / "state.db")


def read_json(path: str) -> object:
    if path == "-":
        data = sys.stdin.buffer.read(MAX_BODY + 1)
    else:
        with open(path, "rb") as file:
            data = file.read(MAX_BODY + 1)
    if len(data) > MAX_BODY:
        raise ValidationError("event_too_large")
    try:
        return json.loads(data)
    except (UnicodeError, json.JSONDecodeError):
        raise ValidationError("invalid_json") from None


def output(payload: dict, stream=None):
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True), file=stream or sys.stdout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Notify humans without approving or resuming agent actions.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--db", default=os.environ.get("AAN_DB", default_database()), help="SQLite state file")
    parser.add_argument("--channels", default=os.environ.get("AAN_CHANNELS", "stdout"), help="email,desktop,stdout")
    parser.add_argument("--action-host", action="append", default=[], help="exact allowed HTTPS review host; repeatable")
    parser.add_argument("--include-summary", action="store_true", default=os.environ.get("AAN_INCLUDE_SUMMARY") == "1",
                        help="opt in to heuristic-redacted summaries in notifications")
    sub = parser.add_subparsers(dest="command", required=True)
    emit = sub.add_parser("emit", help="durably ingest a canonical JSON event")
    emit.add_argument("--file", default="-", help="JSON file or stdin (-)")
    emit.add_argument("--queue-only", action="store_true", help="leave delivery to the foreground worker")
    sub.add_parser("status", help="inspect task state, stale reports, pending requests and delivery failures")
    worker = sub.add_parser("worker", help="drain due notifications")
    worker.add_argument("--watch", action="store_true", help="run in foreground until interrupted")
    sub.add_parser("retry-dead", help="requeue dead deliveries after fixing their configuration")
    receiver = sub.add_parser("serve", help="loopback bearer-authenticated receiver plus foreground delivery worker")
    receiver.add_argument("--port", type=int, default=8765)
    hook = sub.add_parser("hook", help="enqueue supported vendor hooks; return no permission decision")
    hook.add_argument("source", choices=["codex", "claude"])
    sub.add_parser("demo", help="local synthetic workflow; no network or desktop notifications")
    return parser


def demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="aan-demo-") as directory:
        store = Store(Path(directory) / "state.db")
        now = time.time()
        request = Event.parse({"version": 1, "event_id": "demo:request", "task_id": "demo",
                               "request_id": "review", "type": "human_input_required",
                               "status": "waiting", "independent_work": True}, ())
        first = store.emit(request, ("stdout",), now=now)
        duplicate = store.emit(request, ("stdout",), now=now)
        waiting = store.snapshot(now)["tasks"][0]
        deliveries = drain(store)
        resolved = Event.parse({"version": 1, "event_id": "demo:resolved", "task_id": "demo",
                                "type": "request_resolved", "request_id": "review", "outcome": "handled"}, ())
        store.emit(resolved, ("stdout",))
        after_resolution = store.snapshot()["tasks"][0]["status"]
        completed = Event.parse({"version": 1, "event_id": "demo:completed", "task_id": "demo",
                                 "type": "task_status", "status": "completed"}, ())
        store.emit(completed, ("stdout",))
        drain(store)
        return {"synthetic": True, "first": first, "duplicate": duplicate, "waiting_status": waiting["status"],
                "independent_work": waiting["independent_work"], "after_resolution": after_resolution,
                "final_status": store.snapshot()["tasks"][0]["status"], "deliveries": deliveries,
                "note": "No real email, user approval, upstream agent or desktop delivery was exercised."}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    is_hook = args.command == "hook"
    try:
        if args.command == "demo":
            output(demo())
            return 0
        channels = parse_channels(args.channels)
        env_hosts = [x.strip().lower() for x in os.environ.get("AAN_ACTION_HOSTS", "").split(",") if x.strip()]
        hosts = tuple(dict.fromkeys(env_hosts + [x.lower() for x in args.action_host]))
        store = Store(args.db)
        if args.command == "emit":
            event = Event.parse(read_json(args.file), hosts)
            result = store.emit(event, channels, args.include_summary)
            if not args.queue_only:
                result["delivery"] = drain(store)
            output(result)
        elif args.command == "hook":
            result = ingest_hook(store, args.source, read_json("-"), channels, hosts, args.include_summary)
            # stdout belongs to the host's hook protocol, never human text.
            output({})
            output(result, sys.stderr)
        elif args.command == "status":
            output(store.snapshot())
        elif args.command == "retry-dead":
            output({"requeued": store.retry_dead()})
        elif args.command == "worker":
            while True:
                result = drain(store)
                if any(result.values()) or not args.watch:
                    output(result)
                if not args.watch:
                    break
                time.sleep(1)
        elif args.command == "serve":
            token = os.environ.get("AAN_WEBHOOK_TOKEN", "")
            serve(store, token, channels, hosts, args.include_summary, args.port)
    except KeyboardInterrupt:
        return 0
    except ValidationError as exc:
        if is_hook:
            output({})
        output({"error": str(exc)}, sys.stderr)
        return 1
    except Exception:
        # Even filenames and driver errors may carry private information.
        if is_hook:
            output({})
        output({"error": "operation_failed"}, sys.stderr)
        return 1
    return 0
