# Security and privacy

## Boundaries

This software sends notices and records producer-reported lifecycle state. It has
no approval API, no executable tool payloads, no original-agent credentials, and
no upstream answer/resume channel. Email replies can be imported only as
unverified input candidates. A queued notice, an imported reply, a read
notification, or a `request_resolved` event never grants permission. Original
host enforcement stays
in charge. Do not build automatic approval on notification acknowledgement.

Producer assertions are trusted inputs, not independently verified process
telemetry. A malicious producer can lie about status or emit repeated events.
Only connect workflows you authorize. Explicit identities are needed for
authoritative tracking; built-in hook fallbacks are heuristic.

## Data minimization

Default outbound notices omit summaries, structured action text, and raw IDs.
They include a SHA-256 task reference, generic status, and an optional explicitly
allowlisted review URL.
Summaries, action fields, and imported reply text are redacted before SQLite storage.
`--include-action`/`AAN_INCLUDE_ACTION=1` sends reviewed action text only through
email; desktop/stdout do not receive that action text. The redaction catches common
credential markers, email addresses, URLs, and long values, but **cannot guarantee
removal of sensitive information**. Send no secrets, raw commands, private prompts,
document content, or logs. Only include reviewed ordinary questions in action
reply prompts. Do not put secrets in IDs or paths.

Optional links allow only ordinary HTTPS pages on exact user-selected hosts, with
no userinfo/query/fragment/nonstandard port. Encoded recognized credential
markers are rejected too. Arbitrary opaque path values can still be sensitive;
URL validation is not proof of safety. Never use magic-login or bearer URLs.
Action URLs belong in the validated top-level `action_url`, not embedded in steps.

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

## Email-reply trust boundary

Reply-enabled action emails require a separately configured single bare
`AAN_SMTP_REPLY_TO` mailbox. Request references, revisions, `Reply-To`, X-AAN
correlation headers, and the first-line `AAN-REPLY` marker identify the intended
pending request; they are **not authentication or approval tokens**.

`aan replies ingest --file message.eml` reads an explicitly supplied local message,
not an inbox. Strict bounded parsing rejects malformed/ambiguous messages,
recognized automatic/forwarded mail, unmatched notices, and obsolete request
revisions. Neither these filters nor an exact `In-Reply-To` prove a human sent
the reply. Accepted imports remain unverified pending input and leave request
state/upstream work unchanged. There is no `--verified` override or approval API.

Do not trust `From`, copied headers, `Authentication-Results`, DKIM/DMARC success,
or a provider's SENT label plus matching From as the owner's authenticated intent.
Raw authentication headers can be forged outside their receiving trust boundary;
domain authentication is not authorization for the requested action. Gmail's API
also supports inserting mailbox messages without sending them. A trusted host
must establish owner intent using an appropriate authenticated source for the
exact current request, apply the original confirmation policy, and separately
report actual resolution. Required platform confirmation forms and credential
handoffs cannot be bypassed by email. See the [email-first guide and official
references](docs/email-first.md#why-headers-are-not-permission).

Treat reply content as untrusted data. Do not execute it, follow embedded
instructions, load attachments, or infer permission from an ordinary answer.
Keep `.eml` exports private; source files can contain raw addresses and text even
when the imported fields are redacted. Protect stored replies and your retention
policy just as you protect the notification database.

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
