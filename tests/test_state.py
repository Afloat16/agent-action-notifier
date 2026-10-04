from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from agent_action_notifier.models import Event, parse_channels
from agent_action_notifier.privacy import ValidationError, redact_text, safe_action_url
from agent_action_notifier.store import Store
from agent_action_notifier.transports import DeliveryError
from agent_action_notifier.worker import drain


def event(event_id="e1", **updates):
    data = {"version": 1, "event_id": event_id, "task_id": "test", "type": "human_input_required",
            "request_id": "review", "summary": "Review public source"}
    data.update(updates)
    return Event.parse(data, ("chatgpt.com", "github.com"))


class ValidationTests(unittest.TestCase):
    def test_reject_invalid_fields(self):
        for data in [None, [], {"version": 2}, {"version": True}, {"version": 1, "transcript": "secret"}]:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                Event.parse(data, ())

    def test_reject_invalid_values(self):
        for patch in [{"event_id": "secret\nheader"}, {"type": []}, {"summary": [1]},
                      {"summary": "x" * 401}, {"independent_work": 1}, {"status": "running"},
                      {"outcome": "approved"}]:
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                event(**patch)

    def test_status_request_id_must_be_empty_string(self):
        for invalid in [None, False, 0, [], {}, "nonempty"]:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                Event.parse({"version": 1, "event_id": "e", "task_id": "t", "type": "task_status",
                             "status": "running", "request_id": invalid}, ())

    def test_url_requires_exact_allowlist_and_no_credentials(self):
        allowed = ("chatgpt.com",)
        self.assertEqual(safe_action_url("https://chatgpt.com/c/123", allowed), "https://chatgpt.com/c/123")
        for url in ["http://chatgpt.com/c/1", "https://chatgpt.com.evil.test/c/1",
                    "https://user:password@chatgpt.com/c/1", "https://chatgpt.com/c/1?token=private",
                    "https://chatgpt.com/c/1#private", "https://chatgpt.com:999/c/1",
                    "https://chatgpt.com/secret=abc", "https://chatgpt.com/c/1\nsecret",
                    "https://chatgpt.com\\evil.test/c/1", "https://github.com/c/1"]:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                safe_action_url(url, allowed)

    def test_redaction_is_conservative(self):
        text = 'password="the private password" Bearer secrettoken contact user@example.com https://example.com/?key=x'
        result = redact_text(text)
        for secret in ["the private password", "secrettoken", "user@example.com", "?key=x"]:
            self.assertNotIn(secret, result)
        self.assertIn("[redacted]", result)

    def test_channels(self):
        self.assertEqual(parse_channels("email,desktop,email"), ("email", "desktop"))
        for value in ["", "push", "email,push"]:
            with self.assertRaises(ValidationError):
                parse_channels(value)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_durable_request_and_deduplication(self):
        self.assertEqual(self.store.emit(event(), ("email", "desktop"), now=100)["queued"], 2)
        self.assertTrue(self.store.emit(event(), ("email", "desktop"), now=101)["duplicate"])
        self.assertEqual(self.store.emit(event("e2"), ("email", "desktop"), now=102)["queued"], 0)
        reloaded = Store(self.path)
        snapshot = reloaded.snapshot(now=103)
        self.assertEqual(snapshot["tasks"][0]["status"], "waiting")
        self.assertEqual(len(snapshot["tasks"][0]["pending_requests"]), 1)
        self.assertEqual(snapshot["delivery_totals"], {"pending": 2})

    def test_conflicting_event_id_does_not_change_state(self):
        self.store.emit(event(), ("stdout",))
        with self.assertRaisesRegex(ValidationError, "event_id_conflict"):
            self.store.emit(event(summary="Different request"), ("stdout",))
        self.assertEqual(self.store.snapshot()["tasks"][0]["summary"], "Review public source")

    def test_changed_request_supersedes_old_delivery(self):
        self.store.emit(event(), ("email",), now=100)
        self.store.emit(event("e2", summary="Updated review"), ("email",), now=101)
        snapshot = self.store.snapshot(now=102)
        self.assertEqual(snapshot["delivery_totals"], {"pending": 1, "superseded": 1})
        self.assertEqual(snapshot["tasks"][0]["pending_requests"][0]["revision"], 2)

    def test_notifications_are_generic_by_default(self):
        self.store.emit(event(summary="Private acquisition planning"), ("email",), now=100)
        row = self.store.claim("worker", 101)
        self.assertNotIn("Private acquisition", row["body"])
        self.assertNotIn("test", row["body"])
        self.assertIn("Work requiring your input is paused.", row["body"])

    def test_summary_requires_opt_in_and_stays_redacted(self):
        self.store.emit(event(summary="token=verysecret user@example.com"), ("email",), include_summary=True, now=100)
        row = self.store.claim("worker", 101)
        self.assertNotIn("verysecret", row["body"])
        self.assertNotIn("user@example.com", row["body"])
        self.assertIn("heuristically redacted", row["body"])

    def test_pending_requests_reject_running_and_terminal_claims(self):
        self.store.emit(event(), ("stdout",))
        for status in ["running", "completed", "failed", "cancelled", "unknown"]:
            state = Event.parse({"version": 1, "event_id": "state:" + status, "task_id": "test",
                                 "type": "task_status", "status": status}, ())
            with self.subTest(status=status), self.assertRaisesRegex(ValidationError, "pending_human_requests"):
                self.store.emit(state, ("stdout",))
        self.assertEqual(self.store.snapshot()["tasks"][0]["status"], "waiting")

    def test_independent_work_keeps_waiting_honest(self):
        self.store.emit(event(independent_work=True), ("stdout",), now=100)
        self.assertEqual(self.store.snapshot(now=100)["tasks"][0]["status"], "waiting")
        row = self.store.claim("worker", 100)
        self.assertIn("independent authorized work is continuing", row["body"])
        self.assertIn("paused", row["body"])

    def resolve(self, event_id="resolved", request_id="review", outcome="handled"):
        return Event.parse({"version": 1, "event_id": event_id, "task_id": "test",
                            "request_id": request_id, "type": "request_resolved", "outcome": outcome}, ())

    def test_resolution_does_not_resume_and_cancels_stale_request(self):
        self.store.emit(event(), ("email",), now=100)
        self.store.emit(self.resolve(), ("email",), now=101)
        snapshot = self.store.snapshot(now=102)
        self.assertEqual(snapshot["tasks"][0]["status"], "unknown")
        self.assertEqual(snapshot["tasks"][0]["pending_requests"], [])
        self.assertEqual(snapshot["delivery_totals"], {"superseded": 1, "pending": 1})
        state = Event.parse({"version": 1, "event_id": "completed", "task_id": "test",
                             "type": "task_status", "status": "completed"}, ())
        self.store.emit(state, ("email",))
        self.assertEqual(self.store.snapshot()["tasks"][0]["status"], "completed")
        self.store.emit(self.resolve("resolved-again"), ("email",))
        self.assertEqual(self.store.snapshot()["tasks"][0]["status"], "completed")

    def test_multi_request_state_is_not_cleared_early(self):
        self.store.emit(event(), ("email",))
        self.store.emit(event("e2", request_id="second"), ("email",))
        self.store.emit(self.resolve(), ("email",))
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["tasks"][0]["status"], "waiting")
        self.assertEqual([r["request_id"] for r in snapshot["tasks"][0]["pending_requests"]], ["second"])

    def test_resolution_conflict_and_closed_id_reuse_rejected(self):
        self.store.emit(event(), ("email",))
        self.store.emit(self.resolve(), ("email",))
        with self.assertRaisesRegex(ValidationError, "resolution_conflict"):
            self.store.emit(self.resolve("conflict", outcome="cancelled"), ("email",))
        with self.assertRaisesRegex(ValidationError, "request_already_resolved"):
            self.store.emit(event("reopened"), ("email",))

    def test_unknown_resolution_rolls_back(self):
        with self.assertRaisesRegex(ValidationError, "unknown_request"):
            self.store.emit(self.resolve(), ("email",))
        self.assertEqual(self.store.snapshot()["tasks"], [])

    def test_stale_running_status_is_explicit(self):
        state = Event.parse({"version": 1, "event_id": "running", "task_id": "test",
                             "type": "task_status", "status": "running"}, ())
        self.store.emit(state, ("stdout",), now=100)
        self.assertFalse(self.store.snapshot(now=399)["tasks"][0]["stale"])
        self.assertTrue(self.store.snapshot(now=401)["tasks"][0]["stale"])

    def test_obsolete_status_delivery_superseded(self):
        state = Event.parse({"version": 1, "event_id": "running", "task_id": "test",
                             "type": "task_status", "status": "running"}, ())
        self.store.emit(state, ("email",), now=100)
        self.store.emit(event(), ("email",), now=101)
        self.assertEqual(self.store.snapshot(now=102)["delivery_totals"], {"superseded": 1, "pending": 1})

    def test_request_notice_refreshes_when_global_work_state_changes(self):
        self.store.emit(event(independent_work=True), ("email",), now=100)
        blocked = Event.parse({"version": 1, "event_id": "blocked", "task_id": "test",
                               "type": "task_status", "status": "blocked", "independent_work": False}, ())
        self.store.emit(blocked, ("email",), now=101)
        self.assertEqual(self.store.snapshot(now=102)["delivery_totals"], {"superseded": 1, "pending": 1})
        row = self.store.claim("worker", 102)
        self.assertIn("Status: blocked", row["body"])
        self.assertNotIn("is continuing", row["body"])
        self.assertIn("paused", row["body"])

    def test_second_request_refreshes_all_older_pending_notices(self):
        self.store.emit(event(independent_work=True), ("email",), now=100)
        self.store.emit(event("second", request_id="other", status="blocked", independent_work=False),
                        ("email",), now=101)
        self.assertEqual(self.store.snapshot(now=102)["delivery_totals"], {"superseded": 1, "pending": 2})
        first = self.store.claim("w1", 102)
        second = self.store.claim("w2", 102)
        for row in [first, second]:
            self.assertIn("Status: blocked", row["body"])
            self.assertNotIn("is continuing", row["body"])

    def test_concurrent_duplicate_ingestion_queues_once(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.store.emit(event(), ("email",)), range(12)))
        self.assertEqual(sum(result["queued"] for result in results), 1)
        self.assertEqual(self.store.snapshot()["delivery_totals"], {"pending": 1})

    def test_private_database_mode(self):
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX mode/symlink test")
    def test_existing_insecure_or_symlink_database_rejected(self):
        other = Path(self.temp.name) / "unsafe.db"
        other.write_text("")
        other.chmod(0o644)
        with self.assertRaisesRegex(ValidationError, "database_permissions_too_open"):
            Store(other)
        link = Path(self.temp.name) / "symlink.db"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValidationError, "unsafe_database_path"):
            Store(link)

    def test_delimiter_ambiguity_does_not_deduplicate_different_requests(self):
        self.store.emit(event(task_id="a:b", request_id="c"), ("email",))
        self.store.emit(event("e2", task_id="a", request_id="b:c"), ("email",))
        self.assertEqual(self.store.snapshot()["delivery_totals"], {"pending": 2})


class WorkerTests(unittest.TestCase):
    setUp = StoreTests.setUp
    tearDown = StoreTests.tearDown
    resolve = StoreTests.resolve
    def test_retries_persist_across_restarts_and_channels_are_independent(self):
        self.store.emit(event(), ("email", "desktop"), now=100)
        sent = []

        def sender(channel, notification, env):
            sent.append(channel)
            if channel == "email":
                raise DeliveryError("smtp_connect_failed", True)
            return "desktop_command_accepted"

        result = drain(self.store, clock=lambda: 100, sender=sender)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["retrying"], 1)
        reloaded = Store(self.path)
        deliveries = reloaded.snapshot()["deliveries"]
        email = next(row for row in deliveries if row["channel"] == "email")
        self.assertGreaterEqual(email["next_attempt"], 105)
        self.assertLessEqual(email["next_attempt"], 106)
        self.assertEqual(drain(reloaded, clock=lambda: 104, sender=sender)["retrying"], 0)
        retry = drain(reloaded, clock=lambda: 107, sender=lambda *args: "smtp_accepted")
        self.assertEqual(retry["accepted"], 1)

    def test_permanent_failure_is_visible_and_can_be_requeued(self):
        self.store.emit(event(), ("email",), now=100)

        def sender(*args):
            raise DeliveryError("smtp_configuration_missing", False)

        self.assertEqual(drain(self.store, clock=lambda: 100, sender=sender)["dead"], 1)
        self.assertEqual(self.store.snapshot()["delivery_totals"], {"dead": 1})
        self.assertEqual(self.store.retry_dead(), 1)
        self.assertEqual(self.store.snapshot()["delivery_totals"], {"pending": 1})

    def test_resolved_dead_request_does_not_reappear_on_retry(self):
        self.store.emit(event(), ("email",), now=100)

        def sender(*args):
            raise DeliveryError("smtp_configuration_missing", False)

        drain(self.store, clock=lambda: 100, sender=sender)
        self.store.emit(self.resolve(), ("stdout",), now=101)
        self.assertEqual(self.store.retry_dead(), 0)

    def test_crashed_processing_delivery_is_recovered_after_lease(self):
        self.store.emit(event(), ("email",), now=100)
        first = self.store.claim("crashed", 100, lease_seconds=60)
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.claim("other", 159))
        second = Store(self.path).claim("restarted", 161)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["attempts"], 2)
        self.assertFalse(self.store.active(first["id"], "crashed"))

    def test_success_is_acceptance_not_proof_of_reading(self):
        self.store.emit(event(), ("email",), now=100)
        self.assertEqual(drain(self.store, clock=lambda: 100, sender=lambda *args: "smtp_accepted")["accepted"], 1)
        row = self.store.snapshot()["deliveries"][0]
        self.assertEqual(row["state"], "accepted")
        self.assertEqual(row["evidence"], "smtp_accepted")

    def test_delayed_notice_does_not_present_old_report_as_current(self):
        self.store.emit(event(), ("email",), now=100)
        captured = []

        def sender(channel, notification, env):
            captured.append(notification)
            return "smtp_accepted"

        drain(self.store, clock=lambda: 500, sender=sender)
        self.assertIn("queued over 5 minutes ago", captured[0].body)
        self.assertIn("Reported at UTC:", captured[0].body)
        self.assertIn("not proof of current activity", captured[0].body)

    def test_maximum_retries_become_dead(self):
        self.store.emit(event(), ("email",), now=100)

        def sender(*args):
            raise DeliveryError("smtp_connect_failed", True)

        drain(self.store, clock=lambda: 100, sender=sender, max_attempts=1)
        self.assertEqual(self.store.snapshot()["deliveries"][0]["state"], "dead")

    def test_unexpected_exception_is_redacted(self):
        self.store.emit(event(), ("email",), now=100)

        def sender(*args):
            raise RuntimeError("password=private-secret smtp://private.example")

        drain(self.store, clock=lambda: 100, sender=sender)
        rendered = json.dumps(self.store.snapshot())
        self.assertNotIn("private-secret", rendered)
        self.assertNotIn("private.example", rendered)
        self.assertIn("transport_internal_error", rendered)

    def test_live_send_renews_lease_without_concurrent_duplicate(self):
        self.store.emit(event(), ("email",))
        entered = threading.Event()
        release = threading.Event()
        result = []

        def slow_sender(*args):
            entered.set()
            release.wait(2)
            return "smtp_accepted"

        def first_worker():
            result.append(drain(self.store, sender=slow_sender, lease_seconds=0.15, heartbeat_interval=0.02))

        thread = threading.Thread(target=first_worker)
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            time.sleep(0.25)
            second = drain(self.store, sender=lambda *args: self.fail("concurrent duplicate sent"))
            self.assertEqual(second["accepted"], 0)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["accepted"], 1)

    def test_superseded_inflight_receipt_is_not_falsely_counted(self):
        self.store.emit(event(), ("email",))

        def sender(*args):
            self.store.emit(self.resolve(), ("email",))
            return "smtp_accepted"

        result = drain(self.store, sender=sender, limit=1)
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["superseded"], 1)

    def test_invalid_lease_interval_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid_lease_configuration"):
            drain(self.store, lease_seconds=1, heartbeat_interval=10)


if __name__ == "__main__":
    unittest.main()
