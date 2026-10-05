"""Small, TLS-only email and same-machine desktop notification transports.

The return values are evidence of transport acceptance, not receipts proving that
a notification appeared, reached an inbox, or was read. Payloads must already be
privacy-safe, rendered notifications; this module never includes configuration
or credentials in the notification or in an error message.
"""

from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formatdate
import hashlib
import html
import ipaddress
import json
import ntpath
import os
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
from typing import Mapping


SMTP_TIMEOUT_SECONDS = 15
DESKTOP_TIMEOUT_SECONDS = 10
WINDOWS_DESKTOP_TIMEOUT_SECONDS = 20
_WINDOWS_UNAVAILABLE_EXIT = 64
_WINDOWS_PAYLOAD_ENV = "AAN_DESKTOP_PAYLOAD"
_WINDOWS_CREATE_NO_WINDOW = 0x08000000

_ERROR_CODES = frozenset(
    {
        "delivery_failed",
        "unsupported_channel",
        "invalid_notification",
        "invalid_email_configuration",
        "email_reply_configuration_required",
        "smtp_authentication_failed",
        "smtp_tls_unavailable",
        "smtp_tls_verification_failed",
        "smtp_tls_failed",
        "smtp_feature_unavailable",
        "smtp_recipients_rejected",
        "smtp_sender_rejected",
        "smtp_data_rejected",
        "smtp_connection_rejected",
        "smtp_response_error",
        "smtp_disconnected",
        "smtp_timeout",
        "smtp_host_unavailable",
        "smtp_connection_failed",
        "smtp_delivery_failed",
        "desktop_unsupported",
        "desktop_unavailable",
        "desktop_timeout",
        "desktop_command_failed",
        "stdout_failed",
    }
)

_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_MAIL_LOCAL_PART = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+\Z")
_APPLESCRIPT = (
    "on run argv\n"
    "    set notificationTitle to item 1 of argv\n"
    "    set notificationBody to item 2 of argv\n"
    "    display notification notificationBody with title notificationTitle\n"
    "end run\n"
)

# This source is constant. Notification text is decoded only as JSON data from
# a separate environment variable; it never becomes PowerShell source or argv.
# Existing built-in Windows PowerShell/.NET only, with no policy overrides,
# installs, WinRT registration, links, buttons, or approval handlers.
_WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion -lt [version]'5.1') { exit 64 }
try {
    Import-Module -Name ([System.IO.Path]::Combine($PSHOME,
        'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1'))
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
} catch { exit 64 }
if (-not [System.Environment]::UserInteractive) { exit 64 }
$notificationIcon = $null
$exitCode = 0
try {
    $json = [System.Text.Encoding]::UTF8.GetString(
        [System.Convert]::FromBase64String($env:AAN_DESKTOP_PAYLOAD))
    $payload = ConvertFrom-Json -InputObject $json
    [System.Environment]::SetEnvironmentVariable('AAN_DESKTOP_PAYLOAD', $null, 'Process')
    if ($payload.title -isnot [string] -or $payload.body -isnot [string]) { throw 'Invalid payload' }
    $notificationIcon = New-Object System.Windows.Forms.NotifyIcon
    $notificationIcon.Icon = [System.Drawing.SystemIcons]::Information
    $notificationIcon.Text = 'Agent action notifier'
    $notificationIcon.Visible = $true
    [System.Windows.Forms.Application]::DoEvents()
    $notificationIcon.ShowBalloonTip(10000, $payload.title, $payload.body,
        [System.Windows.Forms.ToolTipIcon]::Info)
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    while ($clock.ElapsedMilliseconds -lt 10000) {
        [System.Windows.Forms.Application]::DoEvents()
        [System.Threading.Thread]::Sleep(50)
    }
} catch { $exitCode = 1 }
finally {
    if ($null -ne $notificationIcon) {
        try { $notificationIcon.Visible = $false } catch {}
        try { $notificationIcon.Dispose() } catch {}
    }
}
exit $exitCode
"""
_WINDOWS_ENCODED_SCRIPT = base64.b64encode(_WINDOWS_SCRIPT.encode("utf-16-le")).decode("ascii")


@dataclass(frozen=True)
class Notification:
    """An already rendered message with a stable application identity."""

    title: str
    body: str
    message_id: str
    reply_reference: str = ""
    reply_revision: int = 0


class DeliveryError(Exception):
    """A redacted error that can be safely stored or displayed.

    Only known generic codes are retained. Neither server responses nor command
    output, addresses, credentials, payloads, or underlying exceptions are saved.
    """

    def __init__(self, code: str, retryable: bool = True) -> None:
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else "delivery_failed"
        self.retryable = bool(retryable)
        super().__init__(self.code)


@dataclass(frozen=True)
class _EmailSettings:
    host: str
    port: int
    security: str
    sender: str = field(repr=False)
    recipient: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    reply_to: str = field(repr=False)


def _is_utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _contains_controls(value: str, allowed: str = "") -> bool:
    return any((ord(char) < 32 or ord(char) == 127) and char not in allowed for char in value)


def _validate_notification(notification: Notification) -> None:
    if not isinstance(notification, Notification):
        raise DeliveryError("invalid_notification", retryable=False)
    if (not isinstance(notification.reply_reference, str)
            or type(notification.reply_revision) is not int
            or (notification.reply_reference and (not re.fullmatch(r"[0-9a-f]{24}", notification.reply_reference)
                                                   or notification.reply_revision < 1))
            or (not notification.reply_reference and notification.reply_revision != 0)):
        raise DeliveryError("invalid_notification", retryable=False)
    if any(not isinstance(value, str) for value in (
        notification.title, notification.body, notification.message_id
    )):
        raise DeliveryError("invalid_notification", retryable=False)
    if (
        not notification.title.strip()
        or not notification.message_id
        or _contains_controls(notification.title)
        or _contains_controls(notification.body, allowed="\n\t")
        or not all(_is_utf8(value) for value in (
            notification.title, notification.body, notification.message_id
        ))
    ):
        raise DeliveryError("invalid_notification", retryable=False)


def _is_domain(value: str) -> bool:
    # Bare ASCII hostnames and IP addresses only. URLs, ports and whitespace
    # cannot be smuggled into the connection host or the message's headers.
    if not value or not value.isascii() or len(value) > 253:
        return False
    return all(_DNS_LABEL.fullmatch(label) for label in value.split("."))


def _is_host(value: str) -> bool:
    if not value.isascii() or _contains_controls(value) or any(char.isspace() for char in value):
        return False
    # Scope identifiers are unnecessary for normal SMTP servers and Python's
    # IP parser otherwise accepts arbitrary text after an IPv6 percent sign.
    if "%" in value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return _is_domain(value.removesuffix("."))
    return True


def _is_mailbox(value: str) -> bool:
    # Intentionally one bare mailbox, not a display name or recipient list.
    if not value.isascii() or len(value) > 254 or value.count("@") != 1:
        return False
    local_part, domain = value.split("@")
    return bool(
        0 < len(local_part) <= 64
        and _MAIL_LOCAL_PART.fullmatch(local_part)
        and not local_part.startswith(".")
        and not local_part.endswith(".")
        and ".." not in local_part
        and _is_domain(domain)
    )


def _email_settings(env: Mapping[str, str]) -> _EmailSettings:
    names = (
        "AAN_SMTP_HOST", "AAN_SMTP_PORT", "AAN_SMTP_SECURITY",
        "AAN_SMTP_USERNAME", "AAN_SMTP_PASSWORD", "AAN_SMTP_FROM", "AAN_SMTP_TO",
        "AAN_SMTP_REPLY_TO",
    )
    defaults = {"AAN_SMTP_PORT": "465", "AAN_SMTP_SECURITY": "ssl"}
    values = {name: env.get(name, defaults.get(name, "")) for name in names}
    if any(not isinstance(value, str) for value in values.values()):
        raise DeliveryError("invalid_email_configuration", retryable=False)
    host = values["AAN_SMTP_HOST"]
    port_text = values["AAN_SMTP_PORT"]
    security = values["AAN_SMTP_SECURITY"]
    sender = values["AAN_SMTP_FROM"]
    recipient = values["AAN_SMTP_TO"]
    username = values["AAN_SMTP_USERNAME"]
    password = values["AAN_SMTP_PASSWORD"]
    reply_to = values["AAN_SMTP_REPLY_TO"]
    credentials_supplied = "AAN_SMTP_USERNAME" in env or "AAN_SMTP_PASSWORD" in env
    if (
        not _is_host(host)
        or not port_text.isascii()
        or not port_text.isdecimal()
        or len(port_text) > 5
        or not 1 <= int(port_text) <= 65535
        or security not in {"ssl", "starttls"}
        or not _is_mailbox(sender)
        or not _is_mailbox(recipient)
        or ("AAN_SMTP_REPLY_TO" in env and not _is_mailbox(reply_to))
        or (credentials_supplied and (not username or not password))
        or not username.isascii()
        or not password.isascii()
        or _contains_controls(username)
        or _contains_controls(password)
    ):
        raise DeliveryError("invalid_email_configuration", retryable=False)
    return _EmailSettings(host, int(port_text), security, sender, recipient, username, password, reply_to)


def email_message_id(message_id: str) -> str:
    """The exact stable wire identity; this is correlation, not authentication."""
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    return f"<aan.{digest}@agent-action-notifier.invalid>"


def _response_retryable(code: object) -> bool:
    # A 5xx reply is a permanent rejection; 4xx and unknown failures can retry.
    return not isinstance(code, int) or not 500 <= code <= 599


def _smtp_error(exc: Exception, phase: str) -> tuple[str, bool]:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "smtp_tls_verification_failed", False
    if isinstance(exc, ssl.SSLError):
        return "smtp_tls_failed", False
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "smtp_authentication_failed", _response_retryable(exc.smtp_code)
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        if phase == "tls":
            return "smtp_tls_unavailable", False
        return "smtp_feature_unavailable", False
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        replies = list(exc.recipients.values())
        retryable = not replies or any(
            not isinstance(reply, tuple) or not reply or _response_retryable(reply[0])
            for reply in replies
        )
        return "smtp_recipients_rejected", retryable
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "smtp_sender_rejected", _response_retryable(exc.smtp_code)
    if isinstance(exc, smtplib.SMTPDataError):
        return "smtp_data_rejected", _response_retryable(exc.smtp_code)
    if isinstance(exc, smtplib.SMTPConnectError):
        return "smtp_connection_rejected", _response_retryable(exc.smtp_code)
    if isinstance(exc, smtplib.SMTPResponseException):
        return "smtp_response_error", _response_retryable(exc.smtp_code)
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "smtp_disconnected", True
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "smtp_timeout", True
    if isinstance(exc, socket.gaierror):
        return "smtp_host_unavailable", exc.errno == socket.EAI_AGAIN
    if isinstance(exc, OSError):
        return "smtp_connection_failed", True
    return "smtp_delivery_failed", True


def _close_smtp(client: smtplib.SMTP | None) -> None:
    if client is None:
        return
    # Once DATA was accepted, QUIT/close failure must not turn success into a
    # retry and duplicate the message. Cleanup output is never exposed.
    try:
        client.quit()
    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass


def _deliver_email(notification: Notification, env: Mapping[str, str]) -> str:
    settings = _email_settings(env)
    if notification.reply_reference and not settings.reply_to:
        raise DeliveryError("email_reply_configuration_required", retryable=False)
    client = None
    failure = None
    phase = "connect"
    try:
        message = EmailMessage(policy=SMTP_POLICY)
        message["Subject"] = notification.title
        message["From"] = Address(addr_spec=settings.sender)
        message["To"] = Address(addr_spec=settings.recipient)
        message["Message-ID"] = email_message_id(notification.message_id)
        message["Date"] = formatdate(localtime=False, usegmt=True)
        message["Auto-Submitted"] = "auto-generated"
        if settings.reply_to:
            message["Reply-To"] = Address(addr_spec=settings.reply_to)
        if notification.reply_reference:
            message["X-AAN-Request-Reference"] = notification.reply_reference
            message["X-AAN-Request-Revision"] = str(notification.reply_revision)
        message.set_content(notification.body, subtype="plain", charset="utf-8", cte="quoted-printable")
        context = ssl.create_default_context()
        if settings.security == "ssl":
            client = smtplib.SMTP_SSL(
                settings.host, settings.port, timeout=SMTP_TIMEOUT_SECONDS, context=context
            )
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=SMTP_TIMEOUT_SECONDS)
            phase = "tls"
            client.ehlo()
            client.starttls(context=context)
            # STARTTLS discards the pre-TLS capability list; renegotiate before
            # authentication or sending any message content.
            client.ehlo()
        phase = "auth"
        if settings.username:
            client.login(settings.username, settings.password)
        phase = "send"
        refused = client.send_message(
            message, from_addr=settings.sender, to_addrs=[settings.recipient]
        )
        if refused:
            # There is exactly one configured recipient, so any refusal means
            # the requested notification was not accepted for that recipient.
            raise smtplib.SMTPRecipientsRefused(refused)
    except Exception as exc:
        failure = _smtp_error(exc, phase)
    finally:
        _close_smtp(client)
    if failure is not None:
        # Raise outside the handler so no secret-bearing exception is retained
        # in __context__ or __cause__, even for callers inspecting the object.
        raise DeliveryError(*failure) from None
    return "smtp_accepted"


def _windows_runtime() -> tuple[str, str] | None:
    """Find only the OS's own Windows PowerShell, without a PATH/config override."""
    try:
        # Ask Windows instead of trusting mutable SystemRoot/WINDIR variables.
        # LOAD_LIBRARY_SEARCH_SYSTEM32 restricts even the kernel32 lookup.
        kernel32 = ctypes.WinDLL("kernel32.dll", winmode=0x00000800)
        get_directory = kernel32.GetSystemWindowsDirectoryW
        get_directory.argtypes = (ctypes.c_wchar_p, ctypes.c_uint)
        get_directory.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_directory(buffer, len(buffer))
        if not 0 < length < len(buffer):
            return None
        root = buffer.value
        drive, tail = ntpath.splitdrive(root)
        if (
            not re.fullmatch(r"[A-Za-z]:", drive)
            or (tail and not tail.startswith("\\"))
            or _contains_controls(root)
            or any(part in {".", ".."} for part in tail.split("\\"))
        ):
            return None
        root = ntpath.normpath(drive + (tail or "\\"))
        executable = ntpath.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if not os.path.isfile(executable):
            return None
        return root, executable
    except Exception:
        # No fallback to a PATH executable, user-supplied command, pwsh, or
        # package installation. Neither OS paths nor exception text leave here.
        return None


def _windows_clip_text(value: str, limit: int) -> str:
    # The shell's buffers use WCHARs, not Python code points. Do not split an
    # astral character's surrogate pair. Leave room for a visible ellipsis.
    encoded = value.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return value
    return encoded[: (limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def _windows_environment(root: str, executable: str, notification: Notification) -> dict[str, str]:
    # PowerShell needs no SMTP/configuration secrets, caller PATH, or modules
    # from a user's arbitrary PSModulePath. Retain ordinary local paths and
    # existing process policy restrictions; never override or loosen them.
    allowed = {"TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
               "PSEXECUTIONPOLICYPREFERENCE", "__PSLOCKDOWNPOLICY"}
    child_env = {key.upper(): value for key, value in os.environ.items() if key.upper() in allowed}
    child_env.update({
        "SystemRoot": root,
        "WINDIR": root,
        "SystemDrive": ntpath.splitdrive(root)[0],
        "PATH": ntpath.join(root, "System32") + ";" + root,
        "PSModulePath": ntpath.join(ntpath.dirname(executable), "Modules"),
    })
    payload = {
        "title": _windows_clip_text(notification.title, 63),
        # ShowBalloonTip rejects an empty body. A title-only notice repeats its
        # existing text rather than failing or inventing additional content.
        "body": _windows_clip_text(notification.body if notification.body.strip()
                                   else notification.title, 255),
    }
    child_env[_WINDOWS_PAYLOAD_ENV] = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return child_env


def _deliver_desktop(notification: Notification) -> str:
    timeout = DESKTOP_TIMEOUT_SECONDS
    process_options = {}
    # Desktop utilities need the current user's session (DISPLAY, D-Bus, HOME),
    # but do not need SMTP secrets or other notifier-specific configuration.
    child_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("AAN_")}
    if sys.platform.startswith("linux"):
        executable = shutil.which("notify-send")
        argv = [executable, "--app-name=Agent action notifier", "--", notification.title,
                html.escape(notification.body, quote=False)]
        script = None
    elif sys.platform == "darwin":
        # This OS utility is fixed, not chosen by payload or configuration.
        executable = shutil.which("/usr/bin/osascript")
        # A literal '-' means read the fixed script from stdin. Everything
        # after it is data for 'on run argv', never AppleScript source.
        argv = [executable, "-l", "AppleScript", "-", notification.title, notification.body]
        script = _APPLESCRIPT
    elif sys.platform == "win32":
        runtime = _windows_runtime()
        if runtime is None:
            raise DeliveryError("desktop_unavailable", retryable=False)
        root, executable = runtime
        argv = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
                "-EncodedCommand", _WINDOWS_ENCODED_SCRIPT]
        script = None
        child_env = _windows_environment(root, executable, notification)
        timeout = WINDOWS_DESKTOP_TIMEOUT_SECONDS
        process_options["creationflags"] = _WINDOWS_CREATE_NO_WINDOW
    else:
        raise DeliveryError("desktop_unsupported", retryable=False)
    if executable is None:
        raise DeliveryError("desktop_unavailable", retryable=False)
    failure = None
    try:
        result = subprocess.run(
            argv, input=script, text=True, shell=False, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, env=child_env, **process_options,
        )
        if sys.platform == "win32" and result.returncode == _WINDOWS_UNAVAILABLE_EXIT:
            failure = ("desktop_unavailable", False)
        elif result.returncode != 0:
            failure = ("desktop_command_failed", True)
    except subprocess.TimeoutExpired:
        failure = ("desktop_timeout", True)
    except (FileNotFoundError, PermissionError):
        failure = ("desktop_unavailable", False)
    except Exception:
        failure = ("desktop_command_failed", True)
    if failure is not None:
        raise DeliveryError(*failure) from None
    return "desktop_command_accepted"


def deliver(
    channel: str, notification: Notification, env: Mapping[str, str] | None = None
) -> str:
    """Submit through an explicitly selected channel and return safe evidence.

    ``env`` supplies SMTP settings, defaulting to the process environment. SMTP
    supports certificate-verified SSL (port 465 by default) or mandatory STARTTLS
    (specify the provider's port, commonly 587); plaintext fallback is forbidden.
    Each email address must be one bare ASCII mailbox. Unset both username and
    password for a TLS relay; supplying either requires both nonempty values.

    Desktop delivery uses only the same machine's existing notification session.
    Stdout contains only the rendered title and body, never configuration or ID.
    """
    if channel not in ("email", "desktop", "stdout"):
        raise DeliveryError("unsupported_channel", retryable=False)
    _validate_notification(notification)
    if channel == "email":
        return _deliver_email(notification, os.environ if env is None else env)
    if channel == "desktop":
        return _deliver_desktop(notification)
    failed = False
    try:
        print(f"{notification.title}\n{notification.body}", flush=True)
    except Exception:
        failed = True
    if failed:
        raise DeliveryError("stdout_failed") from None
    return "stdout"
