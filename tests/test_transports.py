"""Transport tests use mocks only: no sockets or actual desktop notifications."""

from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError
from email.message import EmailMessage
import hashlib
import io
import os
import smtplib
import socket
import ssl
import subprocess
import unittest
from unittest.mock import Mock, patch

from agent_action_notifier import transports
from agent_action_notifier.transports import DeliveryError, Notification, deliver


class TransportTestCase(unittest.TestCase):
    def setUp(self):
        self.notification = Notification(
            "Action required", "A task needs your approval.\nPlease review it.", "stable-event-123"
        )

    def assert_safe_error(self, error, code, retryable, secrets=()):
        self.assertEqual(error.code, code)
        self.assertEqual(error.retryable, retryable)
        self.assertEqual(error.args, (code,))
        self.assertEqual(str(error), code)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        for secret in secrets:
            self.assertNotIn(secret, repr(error))
            self.assertNotIn(secret, repr(vars(error)))


class ValidationTests(TransportTestCase):
    def test_notification_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            self.notification.title = "changed"

    def test_unknown_error_code_cannot_become_secret_message(self):
        error = DeliveryError("password=top-secret", False)
        self.assert_safe_error(error, "delivery_failed", False, ("top-secret",))

    def test_unsupported_channel_is_nonretryable_and_redacted(self):
        with self.assertRaises(DeliveryError) as caught:
            deliver("private-channel-name", self.notification, {})
        self.assert_safe_error(caught.exception, "unsupported_channel", False, ("private-channel",))

    def test_invalid_notifications_do_not_reach_transports(self):
        notifications = [
            Notification("", "body", "id"),
            Notification("title\r\nBcc: injected@example.test", "body", "id"),
            Notification("title", "body\x00", "id"),
            Notification("title", "\x1b[2J", "id"),
            Notification("title", "body", ""),
            Notification("title", "\ud800", "id"),
            Notification("title", None, "id"),
            {"title": "title", "body": "body", "message_id": "id"},
        ]
        for notification in notifications:
            with self.subTest(notification=notification):
                with patch.object(transports, "_deliver_email") as email:
                    with self.assertRaises(DeliveryError) as caught:
                        deliver("email", notification, {})
                    self.assert_safe_error(caught.exception, "invalid_notification", False)
                    email.assert_not_called()


class EmailTests(TransportTestCase):
    def setUp(self):
        super().setUp()
        self.env = {
            "AAN_SMTP_HOST": "smtp.example.test",
            "AAN_SMTP_FROM": "notifier@example.test",
            "AAN_SMTP_TO": "owner@example.test",
            "AAN_SMTP_USERNAME": "private-username",
            "AAN_SMTP_PASSWORD": "private-password",
        }
        self.smtp = Mock(spec=smtplib.SMTP)
        self.smtp.send_message.return_value = {}
        self.smtp.ehlo.return_value = (250, b"Hello")
        self.ssl_patch = patch.object(transports.smtplib, "SMTP_SSL", return_value=self.smtp)
        self.plain_patch = patch.object(transports.smtplib, "SMTP", return_value=self.smtp)
        self.context_patch = patch.object(transports.ssl, "create_default_context", return_value=object())
        self.ssl_client = self.ssl_patch.start()
        self.plain_client = self.plain_patch.start()
        self.context = self.context_patch.start()
        self.addCleanup(self.ssl_patch.stop)
        self.addCleanup(self.plain_patch.stop)
        self.addCleanup(self.context_patch.stop)

    def sent_message(self):
        return self.smtp.send_message.call_args.args[0]

    def test_ssl_default_port_verified_context_timeout_and_plain_text_message(self):
        self.assertEqual(deliver("email", self.notification, self.env), "smtp_accepted")
        self.context.assert_called_once_with()
        self.ssl_client.assert_called_once_with(
            "smtp.example.test", 465, timeout=transports.SMTP_TIMEOUT_SECONDS,
            context=self.context.return_value,
        )
        self.plain_client.assert_not_called()
        self.smtp.starttls.assert_not_called()
        self.smtp.login.assert_called_once_with("private-username", "private-password")
        message = self.sent_message()
        self.assertIsInstance(message, EmailMessage)
        self.assertEqual(str(message["Subject"]), self.notification.title)
        self.assertEqual(str(message["From"]), self.env["AAN_SMTP_FROM"])
        self.assertEqual(str(message["To"]), self.env["AAN_SMTP_TO"])
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertEqual(message.get_content().rstrip("\n"), self.notification.body)
        self.assertEqual(message["Content-Transfer-Encoding"], "quoted-printable")
        self.assertFalse(message.is_multipart())
        self.smtp.send_message.assert_called_once_with(
            message, from_addr="notifier@example.test", to_addrs=["owner@example.test"]
        )
        self.smtp.quit.assert_called_once_with()
        self.smtp.close.assert_called_once_with()
        self.smtp.set_debuglevel.assert_not_called()
        for secret in ("private-username", "private-password"):
            self.assertNotIn(secret, message.as_string())

    def test_starttls_authentication_is_after_tls_and_second_ehlo(self):
        env = dict(self.env, AAN_SMTP_SECURITY="starttls", AAN_SMTP_PORT="587")
        self.assertEqual(deliver("email", self.notification, env), "smtp_accepted")
        self.plain_client.assert_called_once_with(
            "smtp.example.test", 587, timeout=transports.SMTP_TIMEOUT_SECONDS
        )
        self.ssl_client.assert_not_called()
        names = [call[0] for call in self.smtp.method_calls]
        self.assertEqual(names[:5], ["ehlo", "starttls", "ehlo", "login", "send_message"])
        self.smtp.starttls.assert_called_once_with(context=self.context.return_value)

    def test_tls_relay_can_omit_both_credentials(self):
        env = {key: value for key, value in self.env.items() if key not in (
            "AAN_SMTP_USERNAME", "AAN_SMTP_PASSWORD"
        )}
        self.assertEqual(deliver("email", self.notification, env), "smtp_accepted")
        self.smtp.login.assert_not_called()

    def test_stable_hashed_message_id_not_raw_header_input(self):
        notification = Notification("Action required", "body", "id\r\nBcc: secret@example.test")
        deliver("email", notification, self.env)
        first = str(self.sent_message()["Message-ID"])
        expected = hashlib.sha256(notification.message_id.encode()).hexdigest()
        self.assertEqual(first, f"<aan.{expected}@agent-action-notifier.invalid>")
        self.assertIsNone(self.sent_message()["Bcc"])
        deliver("email", notification, self.env)
        self.assertEqual(str(self.sent_message()["Message-ID"]), first)
        self.assertNotIn("secret@example.test", self.sent_message().as_string())

    def test_non_ascii_content_and_body_header_like_text_are_literal(self):
        notification = Notification("承認が必要です", "Résumé\nBcc: somebody@example.test\n<b>text</b>", "id")
        deliver("email", notification, self.env)
        message = self.sent_message()
        self.assertEqual(str(message["Subject"]), notification.title)
        self.assertEqual(message.get_content().rstrip("\n"), notification.body)
        self.assertIsNone(message["Bcc"])

    def test_invalid_settings_rejected_before_connect(self):
        invalid_overrides = [
            {"AAN_SMTP_HOST": ""},
            {"AAN_SMTP_HOST": "smtp.example.test\r\nsecret"},
            {"AAN_SMTP_HOST": "https://smtp.example.test"},
            {"AAN_SMTP_HOST": "smtp.example.test.."},
            {"AAN_SMTP_HOST": "::1%bad\ncommand"},
            {"AAN_SMTP_PORT": "0"},
            {"AAN_SMTP_PORT": "65536"},
            {"AAN_SMTP_PORT": "not-a-port"},
            {"AAN_SMTP_PORT": "４６５"},
            {"AAN_SMTP_SECURITY": "none"},
            {"AAN_SMTP_SECURITY": "plaintext"},
            {"AAN_SMTP_USERNAME": ""},
            {"AAN_SMTP_PASSWORD": ""},
            {"AAN_SMTP_USERNAME": "", "AAN_SMTP_PASSWORD": ""},
            {"AAN_SMTP_PASSWORD": "private-password\r\nAUTH injected"},
            {"AAN_SMTP_USERNAME": "Üser"},
            {"AAN_SMTP_TO": "owner@example.test, other@example.test"},
            {"AAN_SMTP_TO": "Owner <owner@example.test>"},
            {"AAN_SMTP_TO": "owner@example.test."},
            {"AAN_SMTP_TO": "owner@example.test.."},
            {"AAN_SMTP_TO": "owner@example.test\r\nBcc: other@example.test"},
            {"AAN_SMTP_FROM": ".notifier@example.test"},
            {"AAN_SMTP_FROM": "notifier..bad@example.test"},
            {"AAN_SMTP_TO": None},
        ]
        for override in invalid_overrides:
            with self.subTest(override=override):
                with self.assertRaises(DeliveryError) as caught:
                    deliver("email", self.notification, dict(self.env, **override))
                self.assert_safe_error(
                    caught.exception, "invalid_email_configuration", False,
                    ("private-password", "other@example.test"),
                )
        self.ssl_client.assert_not_called()
        self.plain_client.assert_not_called()

    def test_numeric_ip_and_absolute_hostname_are_supported(self):
        for host in ("127.0.0.1", "::1", "smtp.example.test."):
            with self.subTest(host=host):
                self.assertEqual(deliver("email", self.notification, dict(self.env, AAN_SMTP_HOST=host)),
                                 "smtp_accepted")

    def test_explicit_empty_environment_does_not_fall_back_to_process_secrets(self):
        with patch.dict(os.environ, self.env):
            with self.assertRaises(DeliveryError) as caught:
                deliver("email", self.notification, {})
        self.assert_safe_error(caught.exception, "invalid_email_configuration", False)
        self.ssl_client.assert_not_called()

    def test_single_credential_key_even_empty_is_invalid(self):
        base = {key: value for key, value in self.env.items() if key not in (
            "AAN_SMTP_USERNAME", "AAN_SMTP_PASSWORD"
        )}
        for key in ("AAN_SMTP_USERNAME", "AAN_SMTP_PASSWORD"):
            for value in ("", "private-credential"):
                with self.subTest(key=key, present=bool(value)):
                    with self.assertRaises(DeliveryError) as caught:
                        deliver("email", self.notification, dict(base, **{key: value}))
                    self.assert_safe_error(caught.exception, "invalid_email_configuration", False,
                                           ("private-credential",))
        self.ssl_client.assert_not_called()

    def test_environment_is_used_if_not_explicitly_supplied(self):
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(deliver("email", self.notification), "smtp_accepted")

    def test_starttls_unavailable_never_authenticates_or_sends(self):
        self.smtp.starttls.side_effect = smtplib.SMTPNotSupportedError("private-password")
        with self.assertRaises(DeliveryError) as caught:
            deliver("email", self.notification, dict(self.env, AAN_SMTP_SECURITY="starttls"))
        self.assert_safe_error(caught.exception, "smtp_tls_unavailable", False, ("private-password",))
        self.smtp.login.assert_not_called()
        self.smtp.send_message.assert_not_called()

    def test_certificate_verification_failure_is_permanent_and_redacted(self):
        self.ssl_client.side_effect = ssl.SSLCertVerificationError("private-password certificate failure")
        with self.assertRaises(DeliveryError) as caught:
            deliver("email", self.notification, self.env)
        self.assert_safe_error(
            caught.exception, "smtp_tls_verification_failed", False, ("private-password",)
        )
        self.smtp.login.assert_not_called()
        self.smtp.send_message.assert_not_called()

    def test_starttls_certificate_failure_never_authenticates_or_sends(self):
        self.smtp.starttls.side_effect = ssl.SSLCertVerificationError("private-password")
        with self.assertRaises(DeliveryError) as caught:
            deliver("email", self.notification, dict(self.env, AAN_SMTP_SECURITY="starttls"))
        self.assert_safe_error(caught.exception, "smtp_tls_verification_failed", False,
                               ("private-password",))
        self.smtp.login.assert_not_called()
        self.smtp.send_message.assert_not_called()

    def test_authentication_failure_does_not_send_message(self):
        self.smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"private-password")
        with self.assertRaises(DeliveryError) as caught:
            deliver("email", self.notification, self.env)
        self.assert_safe_error(caught.exception, "smtp_authentication_failed", False,
                               ("private-password",))
        self.smtp.send_message.assert_not_called()

    def test_smtp_failures_classify_without_server_response_or_secret_leakage(self):
        failures = [
            (smtplib.SMTPAuthenticationError(535, b"private-password"), "smtp_authentication_failed", False),
            (smtplib.SMTPAuthenticationError(454, b"private-password"), "smtp_authentication_failed", True),
            (smtplib.SMTPDataError(550, b"private-password"), "smtp_data_rejected", False),
            (smtplib.SMTPDataError(451, b"private-password"), "smtp_data_rejected", True),
            (smtplib.SMTPSenderRefused(550, b"private-password", "private-sender"), "smtp_sender_rejected", False),
            (smtplib.SMTPRecipientsRefused({"private-recipient": (550, b"private-password")}),
             "smtp_recipients_rejected", False),
            (smtplib.SMTPRecipientsRefused({"private-recipient": (450, b"private-password")}),
             "smtp_recipients_rejected", True),
            (smtplib.SMTPConnectError(421, b"private-password"), "smtp_connection_rejected", True),
            (smtplib.SMTPResponseException(554, b"private-password"), "smtp_response_error", False),
            (smtplib.SMTPServerDisconnected("private-password"), "smtp_disconnected", True),
            (TimeoutError("private-password"), "smtp_timeout", True),
            (socket.gaierror(socket.EAI_AGAIN, "private-password"), "smtp_host_unavailable", True),
            (socket.gaierror(socket.EAI_NONAME, "private-password"), "smtp_host_unavailable", False),
            (ConnectionRefusedError("private-password"), "smtp_connection_failed", True),
            (ssl.SSLError("private-password"), "smtp_tls_failed", False),
            (smtplib.SMTPNotSupportedError("private-password"), "smtp_feature_unavailable", False),
            (RuntimeError("private-password"), "smtp_delivery_failed", True),
        ]
        for failure, code, retryable in failures:
            with self.subTest(code=code, retryable=retryable):
                self.smtp.send_message.side_effect = failure
                with self.assertRaises(DeliveryError) as caught:
                    deliver("email", self.notification, self.env)
                self.assert_safe_error(
                    caught.exception, code, retryable,
                    ("private-password", "private-sender", "private-recipient"),
                )

    def test_refused_recipient_return_is_not_success(self):
        self.smtp.send_message.return_value = {"owner@example.test": (550, b"private-password")}
        with self.assertRaises(DeliveryError) as caught:
            deliver("email", self.notification, self.env)
        self.assert_safe_error(
            caught.exception, "smtp_recipients_rejected", False,
            ("private-password", "owner@example.test"),
        )

    def test_success_survives_post_acceptance_quit_and_close_failures(self):
        self.smtp.quit.side_effect = smtplib.SMTPServerDisconnected("private-password")
        self.smtp.close.side_effect = OSError("private-password")
        self.assertEqual(deliver("email", self.notification, self.env), "smtp_accepted")
        self.smtp.close.assert_called_once_with()

    def test_cleanup_failure_does_not_mask_original_delivery_error(self):
        self.smtp.send_message.side_effect = smtplib.SMTPDataError(550, b"private-password")
        self.smtp.quit.side_effect = OSError("private-password cleanup")
        self.smtp.close.side_effect = OSError("private-password close")
        with self.assertRaises(DeliveryError) as caught:
            deliver("email", self.notification, self.env)
        self.assert_safe_error(caught.exception, "smtp_data_rejected", False, ("private-password",))


class DesktopTests(TransportTestCase):
    def setUp(self):
        super().setUp()
        self.which_patch = patch.object(transports.shutil, "which", return_value="/usr/bin/notify-send")
        self.run_patch = patch.object(transports.subprocess, "run", return_value=Mock(returncode=0))
        self.which = self.which_patch.start()
        self.run = self.run_patch.start()
        self.addCleanup(self.which_patch.stop)
        self.addCleanup(self.run_patch.stop)

    def test_linux_fixed_argv_shell_false_timeout_and_no_secret_environment(self):
        with patch.object(transports.sys, "platform", "linux"):
            with patch.dict(os.environ, {"AAN_SMTP_PASSWORD": "private-password", "DISPLAY": ":1"}):
                self.assertEqual(deliver("desktop", self.notification), "desktop_command_accepted")
        self.which.assert_called_once_with("notify-send")
        argv = self.run.call_args.args[0]
        self.assertEqual(argv, ["/usr/bin/notify-send", "--app-name=Agent action notifier", "--",
                                self.notification.title, self.notification.body])
        kwargs = self.run.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], transports.DESKTOP_TIMEOUT_SECONDS)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertIsNone(kwargs["input"])
        self.assertEqual(kwargs["env"]["DISPLAY"], ":1")
        self.assertNotIn("AAN_SMTP_PASSWORD", kwargs["env"])

    def test_linux_payload_cannot_be_options_shell_or_active_markup(self):
        notification = Notification(
            "--icon=/tmp/private; $(touch /tmp/evil)",
            '<img src="/tmp/private.png"/> & $(touch /tmp/evil)\n<script>x</script>', "id"
        )
        with patch.object(transports.sys, "platform", "linux"):
            self.assertEqual(deliver("desktop", notification), "desktop_command_accepted")
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[2], "--")
        self.assertEqual(argv[3], notification.title)
        self.assertEqual(argv[4],
                         '&lt;img src="/tmp/private.png"/&gt; &amp; $(touch /tmp/evil)\n'
                         '&lt;script&gt;x&lt;/script&gt;')
        self.assertIs(self.run.call_args.kwargs["shell"], False)

    def test_macos_fixed_script_has_only_literal_argv_payload(self):
        self.which.return_value = "/usr/bin/osascript"
        notification = Notification(
            '--title=" & do shell script "touch /tmp/evil" & "',
            '"\nend run\ndo shell script "touch /tmp/evil"\non run argv\n"', "id"
        )
        with patch.object(transports.sys, "platform", "darwin"):
            self.assertEqual(deliver("desktop", notification), "desktop_command_accepted")
        self.which.assert_called_once_with("/usr/bin/osascript")
        self.assertEqual(self.run.call_args.args[0], ["/usr/bin/osascript", "-l", "AppleScript", "-",
                                                    notification.title, notification.body])
        script = self.run.call_args.kwargs["input"]
        self.assertIn("on run argv", script)
        self.assertIn("display notification notificationBody with title notificationTitle", script)
        self.assertNotIn(notification.title, script)
        self.assertNotIn(notification.body, script)
        self.assertNotIn("do shell script", script)
        self.assertIs(self.run.call_args.kwargs["shell"], False)

    def test_missing_program_is_nonretryable_and_no_command_runs(self):
        self.which.return_value = None
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                with patch.object(transports.sys, "platform", platform):
                    with self.assertRaises(DeliveryError) as caught:
                        deliver("desktop", self.notification)
                self.assert_safe_error(caught.exception, "desktop_unavailable", False)
        self.run.assert_not_called()

    def test_other_platforms_explicitly_unsupported(self):
        for platform in ("freebsd14", "openbsd7"):
            with self.subTest(platform=platform):
                with patch.object(transports.sys, "platform", platform):
                    with self.assertRaises(DeliveryError) as caught:
                        deliver("desktop", self.notification)
                self.assert_safe_error(caught.exception, "desktop_unsupported", False)
        self.which.assert_not_called()
        self.run.assert_not_called()

    def test_command_timeout_redacts_argv_and_process_output(self):
        self.run.side_effect = subprocess.TimeoutExpired(
            ["notify-send", "private-payload"], 10,
            output="private-output", stderr="private-error",
        )
        with patch.object(transports.sys, "platform", "linux"):
            with self.assertRaises(DeliveryError) as caught:
                deliver("desktop", self.notification)
        self.assert_safe_error(caught.exception, "desktop_timeout", True,
                               ("private-payload", "private-output", "private-error"))

    def test_executable_disappears_or_has_no_permission(self):
        for error in (FileNotFoundError("private-path"), PermissionError("private-path")):
            with self.subTest(error=type(error).__name__):
                self.run.side_effect = error
                with patch.object(transports.sys, "platform", "linux"):
                    with self.assertRaises(DeliveryError) as caught:
                        deliver("desktop", self.notification)
                self.assert_safe_error(caught.exception, "desktop_unavailable", False, ("private-path",))

    def test_nonzero_exit_is_failure_without_output_leak(self):
        self.run.return_value = Mock(returncode=1, stdout="private-output", stderr="private-error")
        with patch.object(transports.sys, "platform", "linux"):
            with self.assertRaises(DeliveryError) as caught:
                deliver("desktop", self.notification)
        self.assert_safe_error(caught.exception, "desktop_command_failed", True,
                               ("private-output", "private-error"))

    def test_unexpected_subprocess_failure_is_redacted(self):
        self.run.side_effect = RuntimeError("private-password")
        with patch.object(transports.sys, "platform", "linux"):
            with self.assertRaises(DeliveryError) as caught:
                deliver("desktop", self.notification)
        self.assert_safe_error(caught.exception, "desktop_command_failed", True, ("private-password",))


class StdoutTests(TransportTestCase):
    def test_stdout_contains_only_rendered_title_and_body(self):
        output = io.StringIO()
        env = {"AAN_SMTP_PASSWORD": "private-password", "AAN_SMTP_TO": "private-recipient"}
        with redirect_stdout(output):
            self.assertEqual(deliver("stdout", self.notification, env), "stdout")
        self.assertEqual(output.getvalue(), self.notification.title + "\n" + self.notification.body + "\n")
        for secret in ("private-password", "private-recipient", self.notification.message_id):
            self.assertNotIn(secret, output.getvalue())

    def test_stdout_failure_is_redacted(self):
        output = Mock()
        output.write.side_effect = OSError("private-password broken pipe")
        with redirect_stdout(output):
            with self.assertRaises(DeliveryError) as caught:
                deliver("stdout", self.notification)
        self.assert_safe_error(caught.exception, "stdout_failed", True, ("private-password",))


if __name__ == "__main__":
    unittest.main()
