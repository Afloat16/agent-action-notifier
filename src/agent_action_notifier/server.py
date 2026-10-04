"""Foreground, loopback-only authenticated canonical-event receiver."""

import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
from socketserver import ThreadingMixIn
import threading

from .models import Event
from .privacy import ValidationError
from .store import Store
from .worker import drain

MAX_BODY = 16384


class BoundedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # No raw traceback, path, payload or credential-bearing errors.


def make_server(store: Store, token: str, channels: tuple[str, ...], allowed_hosts: tuple[str, ...],
                include_summary: bool = False, port: int = 8765) -> HTTPServer:
    if not isinstance(token, str) or len(token) < 32 or len(token) > 1024 or any(c.isspace() for c in token):
        raise ValidationError("webhook_token_required_minimum_32_chars")
    if not (0 <= port <= 65535):
        raise ValidationError("invalid_port")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentActionNotifier"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(3)
            connection = self.connection

            def expire():
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            self.deadline = threading.Timer(4, expire)
            self.deadline.daemon = True
            self.deadline.start()

        def finish(self):
            try:
                super().finish()
            finally:
                self.deadline.cancel()

        def log_message(self, *_):
            pass  # URLs, authentication headers and payloads never enter logs.

        def answer(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def send_error(self, code, message=None, explain=None):
            self.answer(code, {"error": "unsupported_http_request" if code == 501 else "malformed_http_request"})

        def authorized(self) -> bool:
            if self.headers.get("Origin"):
                self.answer(403, {"error": "browser_origins_not_supported"})
                return False
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode(), ("Bearer " + token).encode()):
                self.answer(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != "/v1/status":
                self.answer(404, {"error": "not_found"})
                return
            try:
                snapshot = store.snapshot()
            except Exception:
                self.answer(503, {"error": "event_storage_unavailable"})
            else:
                snapshot["worker_health"] = dict(self.server.worker_health)
                self.answer(200, snapshot)

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/v1/events":
                self.answer(404, {"error": "not_found"})
                return
            if self.headers.get_content_type() != "application/json":
                self.answer(415, {"error": "json_content_type_required"})
                return
            if self.headers.get("Transfer-Encoding"):
                self.answer(400, {"error": "chunked_input_not_supported"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0:
                    raise ValueError()
            except ValueError:
                self.answer(411, {"error": "content_length_required"})
                return
            if length > MAX_BODY:
                self.answer(413, {"error": "event_too_large"})
                return
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValidationError("incomplete_body")
                event = Event.parse(json.loads(raw), allowed_hosts)
                result = store.emit(event, channels, include_summary)
            except (UnicodeError, json.JSONDecodeError):
                self.answer(400, {"error": "invalid_json"})
            except ValidationError as exc:
                code = 409 if str(exc) in {"event_id_conflict", "resolution_conflict"} else 400
                self.answer(code, {"error": str(exc)})
            except Exception:
                self.answer(500, {"error": "event_storage_failed"})
            else:
                self.answer(202, result)

    server = BoundedHTTPServer(("127.0.0.1", port), Handler)
    server.worker_health = {"state": "not_started", "last_error": ""}
    return server


def serve(store: Store, token: str, channels: tuple[str, ...], allowed_hosts: tuple[str, ...],
          include_summary: bool = False, port: int = 8765, env=None):
    server = make_server(store, token, channels, allowed_hosts, include_summary, port)
    stopped = threading.Event()

    def delivery_loop():
        while not stopped.is_set():
            try:
                drain(store, env=env)
            except Exception:
                server.worker_health = {"state": "retrying", "last_error": "delivery_storage_unavailable"}
                stopped.wait(5)
                continue
            server.worker_health = {"state": "running", "last_error": ""}
            stopped.wait(1)

    thread = threading.Thread(target=delivery_loop, name="aan-delivery", daemon=True)
    thread.start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stopped.set()
        server.server_close()
        thread.join(timeout=15)
