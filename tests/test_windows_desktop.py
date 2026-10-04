"""Windows adapter tests are mocked; they never invoke PowerShell or show UI."""

import base64
import ctypes
import json
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

from agent_action_notifier import transports
from agent_action_notifier.transports import DeliveryError, Notification, deliver


ROOT = r"C:\Windows"
POWERSHELL = ROOT + r"\System32\WindowsPowerShell\v1.0\powershell.exe"


class WindowsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT
        self.kernel32 = Mock()
        self.get_directory = self.kernel32.GetSystemWindowsDirectoryW
        self.get_directory.side_effect = self.fill_directory
        self.dll_patch = patch.object(transports.ctypes, "WinDLL", return_value=self.kernel32,
                                      create=True)
        self.file_patch = patch.object(transports.os.path, "isfile", return_value=True)
        self.which_patch = patch.object(transports.shutil, "which")
        self.dll = self.dll_patch.start()
        self.isfile = self.file_patch.start()
        self.which = self.which_patch.start()
        for patcher in (self.dll_patch, self.file_patch, self.which_patch):
            self.addCleanup(patcher.stop)

    def fill_directory(self, buffer, size):
        buffer.value = self.root
        return len(self.root)

    def test_only_os_reported_fixed_system_executable_is_resolved(self):
        env = {"SystemRoot": r"D:\Untrusted", "WINDIR": r"D:\Untrusted",
               "PATH": r"D:\Untrusted", "AAN_DESKTOP_COMMAND": "private-command"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(transports._windows_runtime(), (ROOT, POWERSHELL))
        self.dll.assert_called_once_with("kernel32.dll", winmode=0x00000800)
        self.assertEqual(self.get_directory.argtypes, (ctypes.c_wchar_p, ctypes.c_uint))
        self.assertEqual(self.get_directory.restype, ctypes.c_uint)
        self.isfile.assert_called_once_with(POWERSHELL)
        self.which.assert_not_called()

    def test_os_root_directory_does_not_use_drive_relative_path(self):
        self.root = "D:"
        self.assertEqual(transports._windows_runtime(),
                         ("D:\\", r"D:\System32\WindowsPowerShell\v1.0\powershell.exe"))

    def test_missing_canonical_executable_has_no_path_fallback(self):
        self.isfile.return_value = False
        self.assertIsNone(transports._windows_runtime())
        self.which.assert_not_called()

    def test_failed_or_oversized_windows_directory_query_is_unavailable(self):
        for result in (0, 32768, 40000):
            with self.subTest(result=result):
                self.get_directory.side_effect = None
                self.get_directory.return_value = result
                self.assertIsNone(transports._windows_runtime())
        self.isfile.assert_not_called()

    def test_untrusted_or_malformed_directory_is_not_used(self):
        for root in ("Windows", r"C:Windows", r"\Windows", r"\\server\Windows",
                     r"C:\Windows\..\Other", r"C:\Windows\.\Other", "C:\\Windows\x1b"):
            with self.subTest(root=root):
                self.root = root
                self.assertIsNone(transports._windows_runtime())
        self.isfile.assert_not_called()

    def test_native_query_and_filesystem_errors_do_not_escape(self):
        for failure in (OSError("private-path"), RuntimeError("private-runtime")):
            with self.subTest(error=type(failure).__name__):
                self.get_directory.side_effect = failure
                self.assertIsNone(transports._windows_runtime())
        self.get_directory.side_effect = self.fill_directory
        self.isfile.side_effect = PermissionError("private-path")
        self.assertIsNone(transports._windows_runtime())


class WindowsDesktopTests(unittest.TestCase):
    def setUp(self):
        self.notification = Notification("Action required", "Please review this request.", "private-id")
        self.platform_patch = patch.object(transports.sys, "platform", "win32")
        self.runtime_patch = patch.object(transports, "_windows_runtime", return_value=(ROOT, POWERSHELL))
        self.run_patch = patch.object(transports.subprocess, "run", return_value=Mock(returncode=0))
        self.which_patch = patch.object(transports.shutil, "which")
        self.env_patch = patch.dict(os.environ, {
            "SystemRoot": r"D:\Untrusted", "WINDIR": r"D:\Untrusted", "PATH": r"D:\Untrusted",
            "PSModulePath": r"D:\UntrustedModules", "TEMP": r"C:\Users\Owner\Temp",
            "USERPROFILE": r"C:\Users\Owner", "APPDATA": r"C:\Users\Owner\AppData\Roaming",
            "AAN_SMTP_PASSWORD": "private-password", "aan_smtp_password": "private-lowercase-secret",
            "AAN_DESKTOP_COMMAND": "private-command", "OTHER_SECRET": "private-other-secret",
            "AAN_DESKTOP_PAYLOAD": "private-stale-payload",
            "PSExecutionPolicyPreference": "AllSigned", "__PSLockdownPolicy": "4",
        }, clear=True)
        self.platform_patch.start()
        self.runtime = self.runtime_patch.start()
        self.run = self.run_patch.start()
        self.which = self.which_patch.start()
        self.env_patch.start()
        for patcher in (self.platform_patch, self.runtime_patch, self.run_patch,
                        self.which_patch, self.env_patch):
            self.addCleanup(patcher.stop)

    def payload(self):
        env = self.run.call_args.kwargs["env"]
        return json.loads(base64.b64decode(env[transports._WINDOWS_PAYLOAD_ENV]).decode("utf-8"))

    def assert_safe_error(self, error, code, retryable):
        self.assertEqual(error.code, code)
        self.assertEqual(error.retryable, retryable)
        self.assertEqual(error.args, (code,))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        for secret in ("private-password", "private-output", "private-error", "private-path",
                       "private-payload", "private-id"):
            self.assertNotIn(secret, repr(error))
            self.assertNotIn(secret, repr(vars(error)))

    def test_fixed_script_and_argv_no_shell_profile_policy_or_install(self):
        self.assertEqual(deliver("desktop", self.notification), "desktop_command_accepted")
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[:-1], [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive",
                                    "-STA", "-EncodedCommand"])
        script = base64.b64decode(argv[-1]).decode("utf-16-le")
        self.assertEqual(script, transports._WINDOWS_SCRIPT)
        self.assertIn("ShowBalloonTip", script)
        self.assertIn("ConvertFrom-Json -InputObject $json", script)
        for forbidden in ("Invoke-Expression", "ExecutionPolicy", "Install-Module", "ToastNotification",
                          "Read-Host", self.notification.title, self.notification.body):
            self.assertNotIn(forbidden, script)
        options = self.run.call_args.kwargs
        self.assertIs(options["shell"], False)
        self.assertIs(options["check"], False)
        self.assertIsNone(options["input"])
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["timeout"], transports.WINDOWS_DESKTOP_TIMEOUT_SECONDS)
        self.assertEqual(options["creationflags"], 0x08000000)
        self.which.assert_not_called()

    def test_payload_is_literal_unicode_json_data_not_source_options_or_identity(self):
        notification = Notification("审批 ' ; $(Write-Error evil)",
                                    '"\n}; Start-Process evil; #\n<img src="private.png">\t🦋', "private-id")
        deliver("desktop", notification)
        self.assertEqual(self.payload(), {"title": notification.title, "body": notification.body})
        for part in self.run.call_args.args[0]:
            self.assertNotIn(notification.title, part)
            self.assertNotIn(notification.body, part)
            self.assertNotIn(notification.message_id, part)
        payload_text = json.dumps(self.payload(), ensure_ascii=False)
        self.assertNotIn(notification.message_id, payload_text)

    def test_environment_has_only_local_paths_trusted_runtime_and_current_payload(self):
        deliver("desktop", self.notification, {"AAN_DESKTOP_COMMAND": "another-private-command"})
        env = self.run.call_args.kwargs["env"]
        self.assertEqual(env["SystemRoot"], ROOT)
        self.assertEqual(env["WINDIR"], ROOT)
        self.assertEqual(env["SystemDrive"], "C:")
        self.assertEqual(env["PATH"], ROOT + r"\System32;" + ROOT)
        self.assertEqual(env["PSModulePath"], ROOT + r"\System32\WindowsPowerShell\v1.0\Modules")
        self.assertEqual(env["TEMP"], r"C:\Users\Owner\Temp")
        self.assertEqual(env["PSEXECUTIONPOLICYPREFERENCE"], "AllSigned")
        self.assertEqual(env["__PSLOCKDOWNPOLICY"], "4")
        for key in ("AAN_SMTP_PASSWORD", "aan_smtp_password", "AAN_DESKTOP_COMMAND", "OTHER_SECRET"):
            self.assertNotIn(key, env)
        self.assertNotIn("private-", repr(env))
        self.assertEqual(self.payload(), {"title": self.notification.title, "body": self.notification.body})

    def test_script_has_runtime_session_guard_finite_pump_and_finally_cleanup(self):
        script = transports._WINDOWS_SCRIPT
        self.assertIn("$PSVersionTable.PSVersion -lt [version]'5.1'", script)
        self.assertIn("[System.Environment]::UserInteractive", script)
        self.assertIn("$clock.ElapsedMilliseconds -lt 10000", script)
        self.assertIn("[System.Threading.Thread]::Sleep(50)", script)
        self.assertIn("finally", script)
        self.assertIn("$notificationIcon.Visible = $false", script)
        self.assertIn("$notificationIcon.Dispose()", script)
        self.assertGreater(transports.WINDOWS_DESKTOP_TIMEOUT_SECONDS, 10)

    def test_windows_length_limits_clip_with_ellipsis_without_splitting_surrogates(self):
        notification = Notification("a" * 62 + "🦋", "b" * 253 + "🦋z", "private-id")
        deliver("desktop", notification)
        payload = self.payload()
        self.assertEqual(payload["title"], "a" * 62 + "…")
        self.assertEqual(payload["body"], "b" * 253 + "…")
        self.assertLessEqual(len(payload["title"].encode("utf-16-le")) // 2, 63)
        self.assertLessEqual(len(payload["body"].encode("utf-16-le")) // 2, 255)

    def test_exact_utf16_limits_do_not_truncate(self):
        notification = Notification("a" * 61 + "🦋", "b" * 253 + "🦋", "private-id")
        deliver("desktop", notification)
        self.assertEqual(self.payload(), {"title": notification.title, "body": notification.body})

    def test_title_only_or_blank_body_notice_repeats_title(self):
        for body in ("", "\n\t "):
            with self.subTest(body=body):
                deliver("desktop", Notification("Review needed", body, "private-id"))
                self.assertEqual(self.payload()["body"], "Review needed")

    def test_invalid_payload_is_rejected_before_runtime_lookup_or_execution(self):
        for title, body in (("title\r\n", "body"), ("title", "body\x00"), ("title", "\ud800")):
            with self.subTest(title=repr(title), body=repr(body)):
                with self.assertRaises(DeliveryError) as caught:
                    deliver("desktop", Notification(title, body, "private-id"))
                self.assert_safe_error(caught.exception, "invalid_notification", False)
        self.runtime.assert_not_called()
        self.run.assert_not_called()

    def test_missing_canonical_runtime_is_nonretryable(self):
        self.runtime.return_value = None
        with self.assertRaises(DeliveryError) as caught:
            deliver("desktop", self.notification)
        self.assert_safe_error(caught.exception, "desktop_unavailable", False)
        self.run.assert_not_called()
        self.which.assert_not_called()

    def test_known_missing_runtime_or_session_exit_is_nonretryable(self):
        self.run.return_value = Mock(returncode=64, stdout="private-output", stderr="private-error")
        with self.assertRaises(DeliveryError) as caught:
            deliver("desktop", self.notification)
        self.assert_safe_error(caught.exception, "desktop_unavailable", False)

    def test_process_timeout_and_failures_are_generic_and_redacted(self):
        failures = [
            (subprocess.TimeoutExpired([POWERSHELL, "private-payload"], 20,
                                       output="private-output", stderr="private-error"),
             "desktop_timeout", True),
            (FileNotFoundError("private-path"), "desktop_unavailable", False),
            (PermissionError("private-path"), "desktop_unavailable", False),
            (OSError("private-path"), "desktop_command_failed", True),
            (RuntimeError("private-password"), "desktop_command_failed", True),
        ]
        for failure, code, retryable in failures:
            with self.subTest(code=code, failure=type(failure).__name__):
                self.run.side_effect = failure
                with self.assertRaises(DeliveryError) as caught:
                    deliver("desktop", self.notification)
                self.assert_safe_error(caught.exception, code, retryable)

    def test_generic_nonzero_exit_does_not_become_delivery_success(self):
        for code in (1, 2, 65):
            with self.subTest(code=code):
                self.run.return_value = Mock(returncode=code, stdout="private-output", stderr="private-error")
                with self.assertRaises(DeliveryError) as caught:
                    deliver("desktop", self.notification)
                self.assert_safe_error(caught.exception, "desktop_command_failed", True)


if __name__ == "__main__":
    unittest.main()
