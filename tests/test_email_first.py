"""Synthetic email-first regressions; no SMTP or real inbox is contacted."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from email.message import EmailMessage
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_action_notifier.cli import main
from agent_action_notifier.models import Action, Event
from agent_action_notifier.privacy import ValidationError, request_reference
from agent_action_notifier.replies import MAX_REPLY_BYTES, Reply
from agent_action_notifier.store import Store
from agent_action_notifier.transports import DeliveryError, Notification, deliver, email_message_id


def action_event(event_id="needed", **updates):
    value = {"version": 1, "event_id": event_id, "task_id": "example-task",
             "type": "human_input_required", "request_id": "format",
             "action": {"kind": "question", "steps": ["Choose the document format."],
                        "reply_prompt": "PDF or DOCX?", "completion_hint": "An ordinary answer is available for review."}}
    value.update(updates)
    return Event.parse(value, ("github.com",))


def raw_reply(notice, answer="PDF please.", message_id="<reply-1@example.test>",
              reference=None, revision=None, **headers):
    message = EmailMessage()
    message["From"] = "owner@example.test"
    message["To"] = "reply-inbox@example.test"
    message["Subject"] = "Re: Human input needed"
    message["Message-ID"] = message_id
    message["In-Reply-To"] = notice["reply_message_id"]
    for name, value in headers.items():
        if name in message:
            message.replace_header(name, value)
        else:
            message[name] = value
    message.set_content(f"AAN-REPLY {reference or notice['reply_reference']} "
                        f"{revision if revision is not None else notice['reply_revision']}\n{answer}")
    return message.as_bytes()


class ActionValidationTests(unittest.TestCase):
    def test_action_is_typed_and_roundtrips(self):
        event = action_event()
        self.assertIsInstance(event.action, Action)
        self.assertEqual(Event.parse(event.dictionary(), ()), event)

    def test_original_events_keep_original_digest_fields(self):
        original = Event.parse({"version": 1, "event_id": "e", "task_id": "t",
                                "type": "human_input_required", "request_id": "r"}, ())
        self.assertNotIn("action", original.dictionary())
        self.assertEqual(set(original.dictionary()), {"version", "event_id", "task_id", "type", "request_id",
                                                     "summary", "action_url", "status", "independent_work", "outcome"})

    def test_action_shape_and_size_validation(self):
        for value in [None, [], {}, {"kind": "execute", "steps": ["Do it"]},
                      {"kind": [], "steps": ["Do it"]}, {"kind": "question", "steps": "Do it"},
                      {"kind": "question", "steps": []}, {"kind": "question", "steps": ["x"] * 13},
                      {"kind": "question", "steps": [" "]}, {"kind": "question", "steps": [False]},
                      {"kind": "question", "steps": ["\x00\x1b"]},
                      {"kind": "question", "steps": ["x" * 501]}, {"kind": "question", "steps": ["\ud800"]},
                      {"kind": "question", "steps": ["x"], "reply_prompt": "x" * 401},
                      {"kind": "question", "steps": ["x"], "completion_hint": None},
                      {"kind": "question", "steps": ["x"], "owner_verified": True}]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                action_event(action=value)

    def test_actions_only_apply_to_human_requests(self):
        for kind in ["task_status", "request_resolved"]:
            value = action_event().dictionary()
            value.update(type=kind)
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                Event.parse(value, ())

    def test_login_requires_allowlisted_ordinary_https_destination(self):
        action = {"kind": "login", "steps": ["Sign in at the official service."]}
        with self.assertRaisesRegex(ValidationError, "login_requires_official_action_url"):
            action_event(action=action)
        self.assertEqual(action_event(action=action, action_url="https://github.com/login").action.kind, "login")
        for url in ["https://github.com/login?token=private", "https://github.com.evil.test/login",
                    "http://github.com/login", "https://github.com/secret=private"]:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                action_event(action=action, action_url=url)

    def test_steps_prompts_and_hints_are_redacted_before_storage(self):
        event = action_event(action={"kind": "question", "steps": ["password=step-secret"],
                                     "reply_prompt": "token=prompt-secret", "completion_hint": "owner@example.test"})
        payload = json.dumps(event.dictionary())
        for private in ["step-secret", "prompt-secret", "owner@example.test"]:
            self.assertNotIn(private, payload)

    def test_request_reference_is_namespaced_without_delimiter_collision(self):
        self.assertNotEqual(request_reference("a:b", "c"), request_reference("a", "b:c"))
        self.assertEqual(len(request_reference("a", "b")), 24)


class EmailFirstStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def accepted_notice(self, event=None, now=100):
        self.store.emit(event or action_event(), ("email",), now=now, include_action=True)
        notice = self.store.claim("synthetic-worker", now)
        self.assertIsNotNone(notice)
        self.assertTrue(self.store.finish(notice["id"], "synthetic-worker", state="accepted", now=now,
                                          evidence="smtp_accepted"))
        return notice

    def test_action_text_is_opt_in_and_email_only(self):
        self.store.emit(action_event(), ("email", "desktop", "stdout"), now=100, include_action=True)
        notices = [self.store.claim(str(index), 100) for index in range(3)]
        for notice in notices:
            with self.subTest(channel=notice["channel"]):
                self.assertEqual("Choose the document format." in notice["body"], notice["channel"] == "email")
                self.assertEqual(bool(notice["reply_reference"]), notice["channel"] == "email")
        self.store.emit(action_event("second", task_id="second"), ("email",), now=101)
        notice = self.store.claim("next", 101)
        self.assertNotIn("Choose the document format.", notice["body"])
        self.assertEqual(notice["reply_message_id"], "")

    def test_login_steps_and_exact_url_are_in_email(self):
        event = action_event(action={"kind": "login", "steps": ["Open the official page.", "Complete sign-in there."]},
                             action_url="https://github.com/login")
        self.store.emit(event, ("email",), now=100, include_action=True)
        notice = self.store.claim("worker", 100)
        self.assertIn("1. Open the official page.", notice["body"])
        self.assertIn("2. Complete sign-in there.", notice["body"])
        self.assertIn("https://github.com/login", notice["body"])
        self.assertIn("Never email passwords", notice["body"])
        self.assertNotIn("original agent or approval interface", notice["body"])

    def test_host_confirmation_limit_is_explicit(self):
        self.store.emit(action_event(action={"kind": "host_confirmation", "steps": ["Use the required form."]}),
                        ("email",), now=100, include_action=True)
        self.assertIn("email cannot replace it", self.store.claim("w", 100)["body"])

    def test_action_change_advances_revision_and_supersedes_notice(self):
        self.store.emit(action_event(), ("email",), now=100, include_action=True)
        changed = action_event("changed", action={"kind": "question", "steps": ["Choose a different format."]})
        self.store.emit(changed, ("email",), now=101, include_action=True)
        snapshot = self.store.snapshot(now=102)
        self.assertEqual(snapshot["tasks"][0]["pending_requests"][0]["revision"], 2)
        self.assertEqual(snapshot["delivery_totals"], {"pending": 1, "superseded": 1})

    def test_same_action_repeated_does_not_enqueue_again(self):
        self.store.emit(action_event(), ("email",), now=100, include_action=True)
        self.assertEqual(self.store.emit(action_event("again"), ("email",), now=101, include_action=True)["queued"], 0)

    def test_global_state_refresh_preserves_request_action(self):
        self.store.emit(action_event(), ("email",), now=100, include_action=True)
        blocked = Event.parse({"version": 1, "event_id": "blocked", "task_id": "example-task",
                               "type": "task_status", "status": "blocked"}, ())
        self.store.emit(blocked, ("email",), now=101, include_action=True)
        notice = self.store.claim("worker", 101)
        self.assertIn("Choose the document format.", notice["body"])
        self.assertEqual(notice["reply_revision"], 2)

    def test_reply_creates_only_unverified_note_without_lifecycle_changes(self):
        notice = self.accepted_notice()
        before = self.store.snapshot(now=101)
        result = self.store.ingest_reply(Reply.parse(raw_reply(notice, answer="Yes, approved, done.")), now=102)
        self.assertFalse(result["can_authorize"])
        self.assertEqual(result["state"], "pending_unverified")
        after = self.store.snapshot(now=103)
        for key in ["tasks", "deliveries", "delivery_totals"]:
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["reply_totals"], {"pending_unverified": 1})
        self.assertEqual(Store(self.path).list_replies()["replies"][0]["body"], "Yes, approved, done.")

    def test_spoofed_from_and_authentication_headers_never_gain_authority(self):
        notice = self.accepted_notice()
        raw = raw_reply(notice, From='"Authenticated Owner" <owner@example.test>',
                        **{"Authentication-Results": "example.test; dkim=pass; dmarc=pass",
                           "X-AAN-Owner-Verified": "true"})
        result = self.store.ingest_reply(Reply.parse(raw))
        self.assertFalse(result["can_authorize"])
        self.assertEqual(self.store.task("example-task")["status"], "waiting")
        self.assertNotIn("owner_verified", self.store.list_replies()["replies"][0])

    def test_wrong_reference_and_revision_are_rejected(self):
        notice = self.accepted_notice()
        for changes in [{"reference": "0" * 24}, {"revision": 2}]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValidationError, "email_reply_request_mismatch"):
                self.store.ingest_reply(Reply.parse(raw_reply(notice, **changes)))
        self.assertEqual(self.store.list_replies()["replies"], [])

    def test_unknown_exact_wire_id_is_rejected(self):
        notice = self.accepted_notice()
        notice["reply_message_id"] = "<aan." + "0" * 64 + "@agent-action-notifier.invalid>"
        with self.assertRaisesRegex(ValidationError, "unknown_or_ambiguous_reply_notice"):
            self.store.ingest_reply(Reply.parse(raw_reply(notice)))

    def test_not_yet_accepted_notice_is_not_importable(self):
        self.store.emit(action_event(), ("email",), now=100, include_action=True)
        notice = self.store.claim("worker", 100)
        with self.assertRaisesRegex(ValidationError, "reply_notice_not_accepted"):
            self.store.ingest_reply(Reply.parse(raw_reply(notice)))

    def test_transport_evidence_must_be_smtp_acceptance(self):
        self.store.emit(action_event(), ("email",), now=100, include_action=True)
        notice = self.store.claim("worker", 100)
        self.store.finish(notice["id"], "worker", state="accepted", now=101, evidence="stdout")
        with self.assertRaisesRegex(ValidationError, "reply_notice_not_accepted"):
            self.store.ingest_reply(Reply.parse(raw_reply(notice)))

    def test_old_revision_rejects_reply_and_obsoletes_existing_notes(self):
        notice = self.accepted_notice()
        reply = Reply.parse(raw_reply(notice))
        self.store.ingest_reply(reply)
        self.store.emit(action_event("changed", summary="Updated request"), ("email",), include_action=True)
        with self.assertRaisesRegex(ValidationError, "email_reply_request_obsolete"):
            self.store.ingest_reply(reply)
        self.assertEqual(self.store.list_replies()["replies"], [])
        self.assertEqual(self.store.list_replies(include_obsolete=True)["replies"][0]["state"], "obsolete")

    def test_resolved_request_rejects_reply_without_reopening(self):
        notice = self.accepted_notice()
        resolution = Event.parse({"version": 1, "event_id": "resolved", "task_id": "example-task",
                                  "type": "request_resolved", "request_id": "format", "outcome": "handled"}, ())
        self.store.emit(resolution, ("stdout",))
        with self.assertRaisesRegex(ValidationError, "email_reply_request_obsolete"):
            self.store.ingest_reply(Reply.parse(raw_reply(notice)))
        self.assertEqual(self.store.task("example-task")["status"], "unknown")

    def test_identical_import_is_idempotent(self):
        notice = self.accepted_notice()
        reply = Reply.parse(raw_reply(notice))
        first = self.store.ingest_reply(reply)
        second = Store(self.path).ingest_reply(reply)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["reply_id"], second["reply_id"])
        self.assertEqual(len(self.store.list_replies()["replies"]), 1)

    def test_content_replay_with_new_message_id_is_idempotent_and_tracks_identity(self):
        notice = self.accepted_notice()
        self.store.ingest_reply(Reply.parse(raw_reply(notice)))
        self.assertTrue(self.store.ingest_reply(Reply.parse(raw_reply(notice, message_id="<reply-2@example.test>")))["duplicate"])
        with self.assertRaisesRegex(ValidationError, "email_reply_message_id_conflict"):
            self.store.ingest_reply(Reply.parse(raw_reply(notice, answer="Changed answer", message_id="<reply-2@example.test>")))

    def test_conflicting_message_identity_is_rejected(self):
        notice = self.accepted_notice()
        self.store.ingest_reply(Reply.parse(raw_reply(notice)))
        with self.assertRaisesRegex(ValidationError, "email_reply_message_id_conflict"):
            self.store.ingest_reply(Reply.parse(raw_reply(notice, answer="Different answer")))

    def test_concurrent_replays_create_one_note(self):
        notice = self.accepted_notice()
        reply = Reply.parse(raw_reply(notice))
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.store.ingest_reply(reply), range(12)))
        self.assertEqual(sum(not result["duplicate"] for result in results), 1)
        self.assertEqual(len(self.store.list_replies()["replies"]), 1)

    def test_reply_text_is_minimized_and_raw_headers_are_not_retained(self):
        notice = self.accepted_notice()
        self.store.ingest_reply(Reply.parse(raw_reply(notice, answer="token=private-token user@example.test\n\n> raw quote private-secret")))
        output = json.dumps(self.store.list_replies())
        for private in ["private-token", "user@example.test", "private-secret", "owner@example.test"]:
            self.assertNotIn(private, output)

    def test_cli_ingestion_and_listing_do_not_deliver_anything(self):
        notice = self.accepted_notice()
        path = Path(self.temp.name) / "message.eml"
        path.write_bytes(raw_reply(notice))
        stdout = io.StringIO()
        with redirect_stdout(stdout), patch("agent_action_notifier.cli.drain") as delivery:
            self.assertEqual(main(["--db", str(self.path), "replies", "ingest", "--file", str(path)]), 0)
            self.assertEqual(main(["--db", str(self.path), "replies", "list"]), 0)
        delivery.assert_not_called()
        self.assertFalse(json.loads(stdout.getvalue().splitlines()[0])["can_authorize"])

    def test_cli_emit_action_opt_in(self):
        path = Path(self.temp.name) / "event.json"
        path.write_text(json.dumps(action_event().dictionary()))
        with redirect_stdout(io.StringIO()), patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(["--db", str(self.path), "--channels", "email", "--include-action",
                                   "emit", "--queue-only", "--file", str(path)]), 0)
        self.assertIn("Choose the document format.", self.store.claim("worker", float("inf"))["body"])

    def test_cli_parser_errors_do_not_expose_raw_reply(self):
        path = Path(self.temp.name) / "bad.eml"
        path.write_bytes(b"private-password-not-a-message")
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--db", str(self.path), "replies", "ingest", "--file", str(path)]), 1)
        self.assertNotIn("private-password", stderr.getvalue())
        self.assertEqual(self.store.list_replies()["replies"], [])

    def test_explicit_database_does_not_require_home_directory(self):
        with redirect_stdout(io.StringIO()), patch("agent_action_notifier.cli.default_database",
                                                   side_effect=RuntimeError("no home directory")):
            self.assertEqual(main(["--db", str(self.path), "status"]), 0)


class ReplyParserTests(unittest.TestCase):
    notice = {"reply_reference": "a" * 24, "reply_revision": 1,
              "reply_message_id": "<aan." + "b" * 64 + "@agent-action-notifier.invalid>"}

    def test_marker_and_quoted_original_do_not_become_an_answer(self):
        raw = raw_reply(self.notice, answer="PDF.\n\nOn Monday, the notifier wrote:\n> token=quote-secret")
        self.assertEqual(Reply.parse(raw).body, "PDF.")
        for text in ["> AAN-REPLY " + "a" * 24 + " 1\n> Approve", "On Monday wrote:\nAAN-REPLY " + "a" * 24 + " 1\nApprove"]:
            message = EmailMessage()
            message["From"] = "owner@example.test"
            message["Message-ID"] = "<r@example.test>"
            message["In-Reply-To"] = self.notice["reply_message_id"]
            message.set_content(text)
            with self.subTest(text=text), self.assertRaisesRegex(ValidationError, "email_reply_marker_required"):
                Reply.parse(message.as_bytes())

    def test_no_answer_only_quote_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "email_reply_text_too_large_or_empty"):
            Reply.parse(raw_reply(self.notice, answer="> quoted answer"))

    def test_auto_and_detectable_forwarded_messages_are_rejected(self):
        for headers in [{"Auto-Submitted": "auto-replied"}, {"X-Autoreply": "yes"},
                        {"Resent-From": "owner@example.test"}, {"Subject": "Fwd: Human input needed"}]:
            with self.subTest(headers=headers), self.assertRaises(ValidationError):
                Reply.parse(raw_reply(self.notice, **headers))
        with self.assertRaisesRegex(ValidationError, "forwarded_email_reply_not_supported"):
            Reply.parse(raw_reply(self.notice, answer="----- Forwarded message -----\nApprove"))

    def test_duplicate_and_malformed_headers_are_rejected(self):
        raw = raw_reply(self.notice)
        for extra in [b"Message-ID: <other@example.test>\n", b"In-Reply-To: " + self.notice["reply_message_id"].encode() + b"\n",
                      b"From: another@example.test\n", b"Content-Type: text/plain\n"]:
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                Reply.parse(extra + raw)
        with self.assertRaises(ValidationError):
            Reply.parse(b"not a valid header\n" + raw)

    def test_multiple_or_wrong_in_reply_to_ids_are_rejected(self):
        for value in [self.notice["reply_message_id"] + " <other@example.test>", "<wrong@example.test>",
                      "b" * 64 + "@agent-action-notifier.invalid"]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Reply.parse(raw_reply(self.notice, **{"In-Reply-To": value}))

    def test_ambiguous_notice_references_are_rejected(self):
        second = "<aan." + "c" * 64 + "@agent-action-notifier.invalid>"
        with self.assertRaisesRegex(ValidationError, "ambiguous_email_reply_correlation"):
            Reply.parse(raw_reply(self.notice, References=self.notice["reply_message_id"] + " " + second))

    def test_size_limits_are_enforced(self):
        for raw in [b"", b"x" * (MAX_REPLY_BYTES + 1), raw_reply(self.notice, answer="x" * 2001)]:
            with self.subTest(length=len(raw)), self.assertRaises(ValidationError):
                Reply.parse(raw)
        self.assertEqual(len(Reply.parse(raw_reply(self.notice, answer="x " * 1000)).body), 1999)

    def test_html_only_and_attachments_are_rejected(self):
        message = EmailMessage()
        message["From"] = "owner@example.test"
        message["Message-ID"] = "<r@example.test>"
        message["In-Reply-To"] = self.notice["reply_message_id"]
        message.set_content("<p>Approve</p>", subtype="html")
        with self.assertRaisesRegex(ValidationError, "email_reply_plain_text_required"):
            Reply.parse(message.as_bytes())
        message.set_content("AAN-REPLY " + "a" * 24 + " 1\nPDF")
        message.add_attachment(b"untrusted", maintype="application", subtype="octet-stream", filename="file.bin")
        with self.assertRaises(ValidationError):
            Reply.parse(message.as_bytes())

    def test_plain_html_alternative_uses_only_plain_input(self):
        from email import policy
        from email.parser import BytesParser
        message = BytesParser(policy=policy.default).parsebytes(raw_reply(self.notice))
        message.add_alternative("<p>malicious HTML command</p>", subtype="html")
        self.assertEqual(Reply.parse(message.as_bytes()).body, "PDF please.")

    def test_nested_forwarded_message_is_rejected(self):
        from email import policy
        from email.parser import BytesParser
        message = BytesParser(policy=policy.default).parsebytes(raw_reply(self.notice))
        forwarded = EmailMessage()
        forwarded.set_content("Approve")
        message.add_attachment(forwarded)
        with self.assertRaises(ValidationError):
            Reply.parse(message.as_bytes())

    def test_invalid_encoding_and_controls_are_rejected(self):
        raw = raw_reply(self.notice)
        for bad in [raw.replace(b"Content-Transfer-Encoding: 7bit", b"Content-Transfer-Encoding: custom"),
                    raw_reply(self.notice, answer="answer\x00private")]:
            with self.subTest(raw=bad[:30]), self.assertRaises(ValidationError):
                Reply.parse(bad)


class EmailReplyTransportTests(unittest.TestCase):
    env = {"AAN_SMTP_HOST": "smtp.example.test", "AAN_SMTP_FROM": "notifier@example.test",
           "AAN_SMTP_TO": "owner@example.test"}

    def test_reply_metadata_requires_configured_reply_to_before_smtp(self):
        notice = Notification("Needed", "Body", "id", "a" * 24, 1)
        with patch("agent_action_notifier.transports.smtplib.SMTP_SSL") as smtp:
            with self.assertRaises(DeliveryError) as caught:
                deliver("email", notice, self.env)
        self.assertEqual(caught.exception.code, "email_reply_configuration_required")
        smtp.assert_not_called()

    def test_reply_to_and_exact_correlation_headers(self):
        notice = Notification("Needed", "Body", "id", "a" * 24, 1)
        client = Mock()
        client.send_message.return_value = {}
        with patch("agent_action_notifier.transports.smtplib.SMTP_SSL", return_value=client):
            self.assertEqual(deliver("email", notice, dict(self.env, AAN_SMTP_REPLY_TO="replies@example.test")), "smtp_accepted")
        message = client.send_message.call_args.args[0]
        self.assertEqual(str(message["Message-ID"]), email_message_id(notice.message_id))
        self.assertEqual(str(message["Reply-To"]), "replies@example.test")
        self.assertEqual(str(message["X-AAN-Request-Reference"]), "a" * 24)
        self.assertEqual(str(message["X-AAN-Request-Revision"]), "1")

    def test_reply_to_must_be_one_bare_mailbox(self):
        for value in ["", "Display <owner@example.test>", "owner@example.test,other@example.test",
                      "owner@example.test\nBcc: other@example.test"]:
            with self.subTest(value=value), patch("agent_action_notifier.transports.smtplib.SMTP_SSL") as smtp:
                with self.assertRaises(DeliveryError):
                    deliver("email", Notification("Needed", "Body", "id"), dict(self.env, AAN_SMTP_REPLY_TO=value))
                smtp.assert_not_called()

    def test_reply_metadata_cannot_inject_headers(self):
        for reference, revision in [("a" * 24 + "\nBcc: other@example.test", 1), ("a" * 24, True),
                                    ("a" * 24, 0), ("", 1), (None, 0)]:
            with self.subTest(reference=reference, revision=revision), self.assertRaises(DeliveryError):
                deliver("email", Notification("Needed", "Body", "id", reference, revision), self.env)


class MigrationTests(unittest.TestCase):
    def test_v1_database_preserves_pending_state_delivery_and_event_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v1.db"
            path.touch(mode=0o600)
            event = Event.parse({"version": 1, "event_id": "old", "task_id": "t",
                                 "type": "human_input_required", "request_id": "r"}, ())
            digest = hashlib.sha256(json.dumps(event.dictionary(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript("""
                CREATE TABLE events (event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, received_at REAL NOT NULL);
                CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT NOT NULL, independent_work INTEGER NOT NULL,
                  summary TEXT NOT NULL, action_url TEXT NOT NULL, observed_at REAL NOT NULL);
                CREATE TABLE requests (task_id TEXT NOT NULL, request_id TEXT NOT NULL, state TEXT NOT NULL,
                  revision INTEGER NOT NULL, summary TEXT NOT NULL, action_url TEXT NOT NULL,
                  opened_at REAL NOT NULL, updated_at REAL NOT NULL, outcome TEXT NOT NULL, PRIMARY KEY(task_id, request_id));
                CREATE TABLE outbox (id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL, task_id TEXT NOT NULL,
                  request_id TEXT NOT NULL, channel TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                  message_id TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  next_attempt REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0, lease_owner TEXT NOT NULL DEFAULT '',
                  last_error TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                  UNIQUE(dedupe_key, channel));
                INSERT INTO tasks VALUES ('t','waiting',0,'','',100);
                INSERT INTO requests VALUES ('t','r','pending',1,'','',100,100,'');
                INSERT INTO outbox VALUES (1,'old-notice','t','r','email','Title','Body','original-id','pending',0,100,0,'','','',100);
                PRAGMA user_version = 1;
                """)
                conn.execute("INSERT INTO events VALUES (?,?,?)", ("old", digest, 100))
                conn.commit()
            store = Store(path)
            self.assertTrue(store.emit(event, ("email",), now=101)["duplicate"])
            snapshot = store.snapshot(now=102)
            self.assertEqual(snapshot["tasks"][0]["status"], "waiting")
            self.assertEqual(snapshot["tasks"][0]["pending_requests"][0]["revision"], 1)
            self.assertEqual(snapshot["tasks"][0]["pending_requests"][0]["action"], None)
            notice = store.claim("worker", 102)
            self.assertEqual(notice["message_id"], "original-id")
            self.assertEqual(notice["reply_reference"], "")
            with store.connection() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(Store(path).list_replies()["replies"], [])
