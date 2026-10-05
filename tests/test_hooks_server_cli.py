import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_action_notifier.cli import main
from agent_action_notifier.hooks import ingest_hook
from agent_action_notifier.privacy import ValidationError
from agent_action_notifier.server import make_server, serve
from agent_action_notifier.store import Store


class HooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.db")

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, **changes):
        data = {"session_id": "session-123", "hook_event_name": "PermissionRequest",
                "tool_name": "Bash", "tool_input": {"command": "password=private-secret"},
                "transcript_path": "/must/never/be/read"}
        data.update(changes)
        return data

    def test_bounded_fallback_deduplication(self):
        result = ingest_hook(self.store, "codex", self.payload(), ("email",), (), now=100)
        self.assertEqual(result["queued"], 1)
        self.assertTrue(ingest_hook(self.store, "codex", self.payload(), ("email",), (), now=101)["duplicate"])
        self.assertEqual(ingest_hook(self.store, "codex", self.payload(), ("email",), (), now=130)["queued"], 1)
        self.assertNotIn("private-secret", json.dumps(self.store.snapshot()))

    def test_explicit_producer_id_is_stable(self):
        payload = self.payload(tool_use_id="tool-123")
        result = ingest_hook(self.store, "claude", payload, ("email",), (), now=100)
        duplicate = ingest_hook(self.store, "claude", payload, ("email",), (), now=1000)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(result["request_id"], duplicate["request_id"])

    def test_clarification_hook(self):
        payload = self.payload(hook_event_name="PreToolUse", tool_name="AskUserQuestion")
        self.assertEqual(ingest_hook(self.store, "claude", payload, ("email",), (), now=100)["queued"], 1)
        task = self.store.snapshot(now=100)["tasks"][0]
        self.assertEqual(task["status"], "waiting")
        self.assertIn("human answer", task["summary"])

    def test_unrelated_hook_is_ignored(self):
        payload = self.payload(hook_event_name="PreToolUse", tool_name="Read")
        self.assertTrue(ingest_hook(self.store, "claude", payload, ("email",), ())["ignored"])
        self.assertEqual(self.store.snapshot()["tasks"], [])

    def test_stop_never_means_completed(self):
        payload = self.payload(hook_event_name="Stop")
        ingest_hook(self.store, "codex", payload, ("email",), (), now=100)
        self.assertEqual(self.store.snapshot(now=100)["tasks"][0]["status"], "unknown")

    def test_stop_cannot_clear_pending_request(self):
        ingest_hook(self.store, "codex", self.payload(), ("email",), (), now=100)
        ingest_hook(self.store, "codex", self.payload(hook_event_name="Stop"), ("email",), (), now=101)
        task = self.store.snapshot(now=101)["tasks"][0]
        self.assertEqual(task["status"], "waiting")
        self.assertEqual(len(task["pending_requests"]), 1)

    def test_invalid_hook_fields(self):
        for payload in [None, {}, self.payload(session_id=[]), self.payload(tool_name=[]),
                        self.payload(tool_use_id=["bad"])]:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ingest_hook(self.store, "codex", payload, ("email",), ())

    def test_hook_cli_stdout_is_only_empty_protocol_object(self):
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(self.payload()).encode()))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(["--db", str(self.store.path), "--channels", "stdout", "hook", "codex"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {})
        self.assertNotIn("private-secret", stderr.getvalue())
        self.assertEqual(self.store.snapshot()["delivery_totals"], {"pending": 1})

    def test_hook_cli_rejects_bad_input_without_permission_decision(self):
        stdin = io.TextIOWrapper(io.BytesIO(b'{"private":"secret"}'))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(["--db", str(self.store.path), "hook", "codex"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), {})
        self.assertNotIn("secret", stderr.getvalue())

    def test_demo_uses_only_temporary_state(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            self.assertEqual(main(["demo"]), 0)
        report = json.loads(stdout.getvalue().splitlines()[-1])
        self.assertTrue(report["synthetic"])
        self.assertEqual(report["waiting_status"], "waiting")
        self.assertEqual(report["after_resolution"], "unknown")
        self.assertEqual(report["final_status"], "completed")


class ServerTests(unittest.TestCase):
    token = "local-test-token-not-a-credential-0123456789"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.db")
        self.server = make_server(self.store, self.token, ("stdout",), ("chatgpt.com",), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = None if payload is None else json.dumps(payload).encode()
        base = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        if headers:
            base.update(headers)
        conn.request(method, path, body=data, headers=base)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def event(self, **changes):
        data = {"version": 1, "event_id": "e1", "task_id": "task", "type": "human_input_required",
                "request_id": "review"}
        data.update(changes)
        return data

    def test_ingest_auth_status_and_duplicate(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, result = self.request("POST", "/v1/events", self.event())
        self.assertEqual(status, 202)
        self.assertEqual(result["queued"], 1)
        self.assertTrue(self.request("POST", "/v1/events", self.event())[1]["duplicate"])
        status, snapshot = self.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["tasks"][0]["status"], "waiting")

    def test_opt_in_email_action_uses_real_authenticated_loopback_ingestion(self):
        server = make_server(self.store, self.token, ("email", "desktop"), (), port=0, include_action=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            payload = self.event(action={"kind": "question", "steps": ["Choose a format."], "reply_prompt": "PDF or DOCX?"})
            connection.request("POST", "/v1/events", body=json.dumps(payload).encode(), headers={
                "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 202)
            self.assertEqual(json.loads(response.read())["queued"], 2)
            connection.close()
            for owner in ["email-worker", "desktop-worker"]:
                row = self.store.claim(owner, float("inf"))
                self.assertEqual("Choose a format." in row["body"], row["channel"] == "email")
                self.assertEqual(bool(row["reply_reference"]), row["channel"] == "email")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_bearer_is_required_for_reads_and_writes(self):
        for method, path in [("GET", "/v1/status"), ("POST", "/v1/events")]:
            self.assertEqual(self.request(method, path, self.event(), {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.store.snapshot()["tasks"], [])

    def test_browser_origins_are_rejected(self):
        self.assertEqual(self.request("POST", "/v1/events", self.event(), {"Origin": "https://evil.test"})[0], 403)

    def test_conflict_is_409(self):
        self.request("POST", "/v1/events", self.event())
        self.assertEqual(self.request("POST", "/v1/events", self.event(summary="Different"))[0], 409)

    def test_invalid_json_schema_and_links_are_400(self):
        for event in [[], self.event(type=[]), self.event(action_url="https://chatgpt.com/?token=private")]:
            self.assertEqual(self.request("POST", "/v1/events", event)[0], 400)

    def test_nonstring_status_request_ids_are_400_and_never_commit(self):
        for invalid in [None, False, 0, [], {}]:
            status = self.event(type="task_status", status="running", request_id=invalid)
            self.assertEqual(self.request("POST", "/v1/events", status)[0], 400)
        self.assertEqual(self.store.snapshot()["tasks"], [])

    def test_status_storage_errors_are_redacted(self):
        with patch.object(self.store, "snapshot", side_effect=sqlite3.OperationalError("secret storage path")):
            status, result = self.request("GET", "/v1/status")
        self.assertEqual(status, 503)
        self.assertEqual(result, {"error": "event_storage_unavailable"})

    def test_delivery_loop_recovers_from_storage_failure_without_traceback(self):
        import time

        fake_server = make_server(self.store, self.token, ("stdout",), (), port=0)
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("private-password-in-error")
            return {"accepted": 0, "retrying": 0, "dead": 0, "superseded": 0}

        def loop(poll_interval):
            deadline = time.monotonic() + 7
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)

        stderr = io.StringIO()
        with patch("agent_action_notifier.server.make_server", return_value=fake_server), \
                patch("agent_action_notifier.server.drain", side_effect=flaky), \
                patch.object(fake_server, "serve_forever", side_effect=loop), patch("sys.stderr", stderr):
            serve(self.store, self.token, ("stdout",), ())
        self.assertGreaterEqual(len(calls), 2)
        self.assertNotIn("private-password", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(fake_server.worker_health["state"], "running")

    def test_vendor_hook_payload_requires_explicit_mapping(self):
        raw_hook = {"session_id": "session", "hook_event_name": "PermissionRequest"}
        self.assertEqual(self.request("POST", "/v1/events", raw_hook)[0], 400)

    def test_content_type_and_query_route_rejected(self):
        self.assertEqual(self.request("POST", "/v1/events", self.event(), {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("GET", "/v1/status?secret=private")[0], 404)

    def test_unexpected_method_error_is_generic_json(self):
        status, result = self.request("PRIVATE_SECRET_METHOD", "/v1/events", self.event())
        self.assertEqual(status, 501)
        self.assertEqual(result, {"error": "unsupported_http_request"})

    def test_slow_preauth_client_does_not_block_authenticated_ingestion(self):
        import socket

        slow = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            slow.sendall(b"POST /v1/events HTTP/1.1\r\nHost: localhost\r\n")
            self.assertEqual(self.request("POST", "/v1/events", self.event())[0], 202)
        finally:
            slow.close()

    def test_oversized_input_rejected(self):
        self.assertEqual(self.request("POST", "/v1/events", self.event(summary="x" * 17000))[0], 413)

    def test_small_or_missing_webhook_token_rejected(self):
        for token in ["", "too-short", "x" * 33 + "\n"]:
            with self.assertRaises(ValidationError):
                make_server(self.store, token, ("stdout",), (), port=0)


if __name__ == "__main__":
    unittest.main()
