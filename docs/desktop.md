# Desktop notification scope and limitations

Desktop notifications run on **the same machine and in the same user session as
the notifier**. Running the notifier on a remote server, in a container, or over
SSH does not forward notifications to your laptop. This project does not install
notification tools, start a background service, change notification permissions,
or modify operating-system policies.

## Supported platforms

- **Linux:** an existing `notify-send` executable and a working desktop
  notification session are required. The command receives a fixed option list,
  an option terminator, and separate title/body arguments. Body markup characters
  are escaped, so supplied text cannot load an image or create a markup link.
- **macOS:** the built-in `/usr/bin/osascript` utility runs one fixed AppleScript
  read from standard input. The rendered title and body are separate arguments to
  its `run` handler; neither is inserted into AppleScript source.
- **Windows:** existing Windows PowerShell 5.1 and .NET Framework Windows Forms
  issue a notification-area **balloon request** using `NotifyIcon.ShowBalloonTip`.
  This is not a registered WinRT toast implementation. An interactive logged-in
  Windows desktop is required. See the Windows details below.
- **Other platforms:** desktop delivery returns the nonretryable
  `desktop_unsupported` error. Channels never silently fall back to one another.

No command shell is used. Linux/macOS commands have a 10-second timeout; Windows
has a 20-second process timeout around a finite 10-second message pump. Command
output is discarded, and failures expose only generic codes. SMTP-specific
environment variables are not inherited by desktop utilities. Only an existing,
trusted `notify-send` from
the process's normal command path is used on Linux.

## Windows details

The adapter asks Windows for its system directory with
`GetSystemWindowsDirectoryW`, then uses only the existing fixed executable at
`System32\WindowsPowerShell\v1.0\powershell.exe` beneath that directory. `PATH`,
`SystemRoot`, `WINDIR`, or notifier configuration cannot select another command.
There is no PowerShell 7, third-party module, download, installation, registry,
startup/task-scheduler, permission, or execution-policy fallback.

The command uses `-NoProfile`, `-NonInteractive`, and `-STA` with a constant
UTF-16LE encoded script. Encoding is only a quoting mechanism, not encryption.
The title and body are UTF-8 JSON in a separate base64 environment variable, never
interpolated into source or command arguments. Only ordinary local runtime paths,
existing process-policy restrictions, the fixed Windows/PowerShell paths, and this
payload reach the child process. Built-in utility commands are loaded from that
PowerShell installation; arbitrary caller module paths and unrelated environment
secrets are not passed along.

Windows shell buffers allow 63 UTF-16 units for the title and 255 for the body.
Long text is shortened with an ellipsis without splitting a surrogate pair, so
long review links or instructions may be cut off. An empty or whitespace-only
body repeats the supplied title. Keep desktop notices short, and retain the
original request in the workflow's authenticated review surface.

The script creates a temporary notification-area icon with the built-in
information icon, requests the balloon, pumps messages for 10 seconds, then hides
and disposes the temporary icon in `finally`. There are no click-to-approve
handlers, dialogs, links opened automatically, or persistent tray process.
Microsoft documents `ShowBalloonTip`'s display timeout as deprecated and governed
by system accessibility settings. Notifications may be queued behind another
balloon, suppressed, or removed during cleanup before becoming visible. The
10-second pump does not promise a 10-second visible popup or notification history.

Run the notifier worker directly on the Windows desktop you want to notify.
Running it under WSL uses the Linux adapter; containers, an SSH session, services,
and scheduled tasks outside the interactive user desktop must not be relied on
to forward Windows desktop notices. Missing PowerShell, an older runtime,
unavailable built-in assemblies, or a detected noninteractive session return nonretryable
`desktop_unavailable`. Other process failures remain generic and retryable.

The Python-to-process boundary, source/payload separation, clipping, and failures
are covered by mocked tests, configured to run in Windows CI too. No actual
Windows runtime or visible-popup check was performed during implementation. Before relying on
this adapter, explicitly test a harmless event in your own interactive desktop.

## What success means

`desktop_command_accepted` means only that the local command exited successfully.
It does **not** prove that a popup appeared, remained visible, or was read. Desktop
settings, Focus/Do Not Disturb, notification permissions, daemon behavior, and
session availability may suppress notifications. The notifier does not alter any
of these settings.

`desktop_unavailable` is nonretryable: the program/runtime is absent, cannot
execute, or Windows detects that its required interactive session is unavailable.
`desktop_timeout` and `desktop_command_failed` are retryable transport failures.
Do not automatically retry unsupported platforms or install missing tools.

## Privacy

Send only already rendered, privacy-safe notification text. Desktop systems may
retain notifications in history or logs. Linux/macOS title/body command arguments
and the Windows payload environment may be visible to processes on the same
machine. Base64 is not a privacy boundary. Apart from Windows buffer-length
clipping, the adapter does not inspect or redact arbitrary secrets embedded in
your own rendered text. It rejects control-character payloads rather than
forwarding terminal escape sequences or injected header lines. The stdout channel
likewise prints only the rendered title
and body, not SMTP settings or the application message ID.

## References

- [Apple's notification command documentation](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/DisplayNotifications.html)
- [Desktop Notifications Specification](https://specifications.freedesktop.org/notification/latest-single/)
- [Ubuntu's `notify-send` manual](https://manpages.ubuntu.com/manpages/jammy/man1/notify-send.1.html)
- [Microsoft's `NotifyIcon.ShowBalloonTip` API and display-time limitations](https://learn.microsoft.com/en-us/dotnet/api/system.windows.forms.notifyicon.showballoontip?view=netframework-4.8.1)
- [Microsoft's `NOTIFYICONDATAW` text-buffer limits](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-notifyicondataw)
- [Microsoft's Windows PowerShell command-line interface](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1)
- [Microsoft's system Windows directory API](https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-getsystemwindowsdirectoryw)
- [Microsoft's interactive-session check](https://learn.microsoft.com/en-us/dotnet/api/system.environment.userinteractive?view=netframework-4.8.1)
- [Python's subprocess interface](https://docs.python.org/3.10/library/subprocess.html)

The unit tests mock subprocess execution. They never display real notifications,
request permissions, or change the host's desktop configuration.
