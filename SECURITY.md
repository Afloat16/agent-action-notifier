# Security and privacy

## Boundaries

This software sends notices and records producer-reported lifecycle state. It has
no approval API, no executable tool payloads, no original-agent credentials, and
no upstream answer/resume channel. A queued notice, a read notification, or a
`request_resolved` event never grants permission. Original host enforcement stays
in charge. Do not build automatic approval on notification acknowledgement.

Producer assertions are trusted inputs, not independently verified process
telemetry. A malicious producer can lie about status or emit repeated events.
Only connect workflows you authorize. Explicit identities are needed for
authoritative tracking; built-in hook fallbacks are heuristic.

## Data minimization

Default outbound notices omit summaries and raw IDs. They include a SHA-256 task
reference, generic status, and an optional explicitly allowlisted review URL.
Summaries are redacted before SQLite storage. The opt-in redaction catches common
credential markers, email addresses, URLs, and long values, but **cannot guarantee
removal of sensitive information**. Send no secrets, raw commands, prompts,
question contents, document content, or logs. Do not put secrets in IDs or paths.

Optional links allow only ordinary HTTPS pages on exact user-selected hosts, with
no userinfo/query/fragment/nonstandard port. Encoded recognized credential
markers are rejected too. Arbitrary opaque path values can still be sensitive;
URL validation is not proof of safety. Never use magic-login or bearer URLs.

The database is not encrypted. New POSIX files use mode 0600; symlinks and
existing group/world-accessible database files are rejected. Protect its parent
directory, backups, local user account, and Windows ACLs yourself. This tool
does not change security settings. Network filesystems with unreliable SQLite
locking are not supported. No automatic retention purge occurs; choose a
retention/backup policy appropriate for your data.

SMTP, recipient inboxes, desktop histories, and stdout sinks can retain notices.
Desktop argument strings may be visible to other processes. TLS protects SMTP
transport, not the recipient's inbox. No external error text, host response,
credentials, or notification content is included in delivery error codes.

## Credential and access setup

SMTP configuration is read from the worker environment. Credentials are not
written to SQLite, source, or CLI arguments. Do not commit `.env` or credentials.
Use a protected environment or secret manager. Creating app passwords, OAuth
grants, credentials, persistent services, public endpoints or tunnels requires
your own explicit authorization and setup. This project performs none of those.

The webhook requires a separately supplied bearer token of at least 32 characters.
It is **only** bound to IPv4 loopback. Authentication is required for reads and
writes; browser Origin requests are rejected and responses are not cached.
Concurrency is bounded to 16 connections, with inactivity/absolute socket
deadlines and redacted protocol/storage errors. These limits do not make it a
public or safety-critical server; storage already underway may finish after a
client disconnect, so retry uncertain ingestion with the same event ID/content.
Do not forward this HTTP listener onto a network. A reviewed secure TLS proxy,
authentication, authorization, limits and network policy would be separate work.
Never put the token in a URL. Local tokens protect against unauthorized callers,
not a compromised same-user process or administrator.

Email uses certificate-verified SSL or mandatory STARTTLS. Plaintext fallback,
certificate-warning bypass, arbitrary notification commands, shell execution,
automatic package installation, and permission changes are prohibited.

## Reliability limits

The outbox is at-least-once, not exactly-once. Crashes after SMTP/desktop acceptance
but before a committed receipt can cause duplicates. Outgoing notices already in
an external transport cannot be recalled. Success proves transport acceptance
only, not display, inbox placement, human attention, or upstream resumption.

Foreground processes must be running. A stopped worker, sleeping desktop,
unavailable network, changed agent API, missing event, lost host mapping, or absent
notification permissions can prevent timely notice. Inspect delivery failures;
test actual delivery before relying on it. The notifier does not provide a
guaranteed emergency or safety-critical alerting service.

## Reporting

Do not post credentials, raw database files, private event content, or other
personal information in public issues. For a non-sensitive reproducible defect,
open an issue with a minimized synthetic event and generic error code. For a
security concern, use the maintainer's private security-reporting option if
available; otherwise ask for a private contact without posting the exploit or
sensitive data. This file does not claim a private reporting channel is enabled.
