# Agent Action Notifier

**Know when an agent needs you, without watching its conversation.**

[简体中文](README.zh-CN.md) · [Email-first setup](docs/email-first.md) · [Agent integrations](docs/integrations.md) ·
[Desktop limitations](docs/desktop.md) · [Security](SECURITY.md)

Python 3.10+ · MIT · No third-party runtime dependencies · Version 0.2.0

An agent can spend minutes waiting for a permission, answer, login, or review
while its owner assumes work is continuing. This small tool turns **explicit
workflow events** into durable email and local desktop notifications, with a
visible inventory of pending requests and delivery failures.

It is a notification layer, not an approval layer. It cannot approve an action,
answer a question, sign in, bypass a security policy, or resume an upstream agent.
Work needing permission stays paused. A producer can separately report that
independent, already-authorized work is continuing.

## What is implemented

- CLI/stdin ingestion and an authenticated, loopback-only JSON webhook
- SQLite transactions for task state, pending requests, and a durable outbox
- Stable event/request IDs, duplicate suppression, obsolete-alert cancellation
- Independent channel delivery, exponential retry/backoff, crash-lease recovery,
  and visible dead deliveries
- Certificate-verified SMTP SSL/STARTTLS; macOS/Linux alerts and Windows notification-area balloons
- Explicit `running`, `waiting`, `blocked`, `completed`, `failed`, `cancelled`, and
  `unknown` states; stale nonterminal reports are flagged
- Short, notification-only Codex/Claude command-hook adapters
- Generic notifications by default; summaries require explicit opt-in
- Opt-in email action steps, safe official links, and ordinary reply prompts
- Local `.eml` reply import as unverified input for a trusted host to review

There is **no automatic ChatGPT attachment**, browser scraping, session-file
reading, background-service installation, cloud tunnel, remote desktop forwarding,
or one-click approval endpoint. Windows uses its existing PowerShell/.NET desktop
runtime; this is a notification-area balloon, not a native WinRT toast.
See the [integration contract](docs/integrations.md).

## Try it safely, without email or credentials

If a downloadable `agent-action-notifier.pyz` is available in this repository's
GitHub Releases, use it; otherwise build from source using Python alone:

```sh
git clone https://github.com/Afloat16/agent-action-notifier.git
cd agent-action-notifier
python scripts/build_zipapp.py
python dist/agent-action-notifier.pyz demo
```

The demo uses a temporary database and stdout. It does not contact a mail server,
run a desktop command, operate an agent, or obtain a user's approval. It shows:
waiting with independent work → duplicate suppression → request cleared with
state `unknown` → producer-confirmed completion.

Install the CLI in a virtual environment if you want the shorter `aan` command:

```sh
python -m venv .venv
# Activate .venv using your platform's normal command, then:
python -m pip install .
aan --version
```

Installation may download the setuptools build backend. The zipapp route needs
no build dependency or package installation. No PyPI publication is assumed.

## Smallest useful workflow

Use one database path in the producer and worker. The default channel is stdout,
so these commands send no email and do not display a desktop notification.

```sh
aan --db ./private-state.db emit --queue-only --file examples/input-required.json
aan --db ./private-state.db worker
aan --db ./private-state.db status
```

For ongoing delivery, run the worker in a **separate foreground terminal**:

```sh
aan --db ./private-state.db worker --watch
```

It polls due outbox entries approximately once per second. It survives queued
work across restarts; it does not run while the computer/process is stopped.
Hooks only enqueue and return, so SMTP or desktop failures cannot make a hook
grant permission or hold up the agent's approval UI.

## Enable email and/or desktop deliberately

Configure the desired channel in the **producer's** environment or arguments:

```sh
export AAN_CHANNELS=email,desktop
```

The selected channels are persisted with each queued notification. Setting this
only on the worker does not retrofit old stdout notifications. On Windows use
PowerShell's `$env:AAN_CHANNELS = 'email,desktop'` syntax.

For the email worker, supply settings through a protected environment or secret
manager. Review `.env.example`; this tool does **not** load it automatically.

```sh
export AAN_SMTP_HOST=smtp.example.com
export AAN_SMTP_PORT=465
export AAN_SMTP_SECURITY=ssl
export AAN_SMTP_FROM=agent-notices@example.com
export AAN_SMTP_TO=owner@example.com
# Supply AAN_SMTP_USERNAME and AAN_SMTP_PASSWORD securely, not as command arguments.
aan --db ./private-state.db worker --watch
```

Use the provider's documented settings. For STARTTLS choose `starttls` and the
correct port (commonly 587); plaintext fallback is prohibited. SMTP passwords,
app-password creation, OAuth grants, account permissions, and operating-system
notification permissions remain your responsibility. Never give this tool a
password in event JSON. A TLS relay may omit both username and password; setting
either requires both nonempty. FROM/TO each accept one bare ASCII mailbox.
Reply-enabled action emails also require a separately configured
`AAN_SMTP_REPLY_TO` mailbox; this tool does not create that mailbox or read its inbox.

Linux needs an existing `notify-send` plus a desktop notification session.
macOS uses built-in `osascript`; notification permissions or Focus settings may
suppress it. The worker must run on your own computer to notify that desktop.
Running it in a cloud container does not notify your laptop. No missing program,
system service, permission, or forwarding tunnel is installed automatically.
Windows requires an interactive logged-in desktop and existing Windows PowerShell
5.1/.NET Windows Forms. It requests a notification-area balloon, clips to the
OS's title/body limits, and keeps a temporary icon alive for 10 seconds before
cleanup. Focus settings, OS suppression or another balloon may hide it.

Windows PowerShell quick start, after downloading the zipapp:

```powershell
$env:AAN_CHANNELS = 'email,desktop'
$env:AAN_DB = "$env:LOCALAPPDATA\AgentActionNotifier\state.db"
python .\agent-action-notifier.pyz worker --watch
# In the producer terminal, inherit the same settings and emit actual events.
```

Both terminals must inherit the same database/channel settings. This does not
install a service, alter execution policy, or configure SMTP credentials.

First test **with a harmless event and the actual inbox/desktop** before relying
on delivery. Check spam filters and `aan status`. Do not treat transport acceptance
as proof that a popup appeared, an email reached the inbox, or a human read it.

## Email-first human input

A reviewed canonical `human_input_required` event can include an `action` with
numbered steps, an ordinary email-reply question, and a safe official login or
action page. Enable `--include-action` or `AAN_INCLUDE_ACTION=1` in the producer
to include that structured action **only in email**. This option leaves action
text out of desktop/stdout notices; action text is omitted from all outbound
notices by default.

```sh
# No email or desktop delivery; inspect local state first.
aan --db ./email-first-demo.db --channels stdout --include-action emit --file examples/email-action.json
aan --db ./email-first-demo.db status
```

For email delivery, configure SMTP and a real reply mailbox first, then run a
worker and emit the event with `--channels email --include-action`. The full
[email-first guide](docs/email-first.md) covers both sides of that setup,
official login links, and `aan replies ingest --file message.eml` / `aan replies list`.

Ordinary answers can stay in email when an authorized host supports them. Login
credentials stay on the official site. Imported replies are **unverified pending
input**: they never approve, execute, resolve a request, or resume an agent.
A trusted host must authenticate the owner's intent and apply the original
confirmation policy. Mandatory platform forms and handoffs still use their
required surface; the tool does not force every interaction back to GPT.

## Report human input and truthful task state

```json
{
  "version": 1,
  "event_id": "build-42:review-needed",
  "task_id": "build-42",
  "type": "human_input_required",
  "request_id": "review-1",
  "status": "waiting",
  "independent_work": true,
  "summary": "Review the proposed publication in the original workflow."
}
```

`independent_work=true` must mean the producer actually knows independent
authorized work is continuing. It does not change `waiting` into `running`.
The notifier does not verify that assertion against an agent process.

When the original workflow confirms the request was answered or cleared, emit
`request_resolved` with the same request ID and `outcome=handled`, `cancelled`, or
`superseded`. This updates the notifier only. `handled` includes a decline; it
does not mean permission was granted. If no requests remain, state becomes
`unknown` until the producer reports the next actual task status.

The tool refuses `running` or terminal status while requests remain pending.
Use new task/request IDs for new runs and new decisions; resolved request IDs
cannot be reused. Terminal tasks cannot be silently reopened.

Full lifecycle, request-resolution, webhook, and supported vendor-hook examples:
[docs/integrations.md](docs/integrations.md). Hook-only request tracking is
heuristic: missing producer IDs and missing upstream resolution events require
an explicit host integration for authoritative tracking.

## Privacy-safe review links and summaries

Notifications contain a hashed task reference and generic status by default.
An optional ordinary, authenticated review link must use HTTPS on an exact
allowlisted host. It cannot contain userinfo, query parameters, fragments,
nonstandard ports, or recognized credential markers.

```sh
export AAN_ACTION_HOSTS=chatgpt.com,github.com
```

No host is allowed by default. Never supply magic-login, bearer-token, or approval
URLs; arbitrary IDs in paths can still be sensitive. The producer must select a
safe review page. The link itself performs no action.

`--include-summary` or `AAN_INCLUDE_SUMMARY=1` opts into summaries with conservative
heuristic redaction. Redaction is **not a complete secret detector**. Avoid
private text, commands, logs, or credentials even with it on. Reviewed ordinary
questions belong in the separate opt-in `action`, not an unreviewed summary.
SQLite keeps redacted summaries/actions and validated links locally, but this is
not encryption. Imported reply text is also conservatively redacted.
See [SECURITY.md](SECURITY.md).

## Delivery, retries, and honesty

- Accepted ingestion means durably stored/queued, not delivered.
- `smtp_accepted` means the SMTP server accepted the message.
- `desktop_command_accepted` means the desktop utility returned success.
- Both are weaker than delivery/read receipts.
- Delivery is at-least-once. A crash after external acceptance but before local
  commit can cause a duplicate; stable Message-ID is a hint, not an exactly-once
  guarantee.
- Retryable failures back off from roughly 5 seconds to about 18 minutes maximum
  including jitter, with at most 8 attempts. Permanent failures become `dead`.
- Inspect `aan status`; after fixing the cause, `aan retry-dead` requeues still
  relevant dead deliveries. Superseded requests do not revive.
- Newer request/status events cancel obsolete unsent notices. A notification
  already in an external transport cannot be recalled.
- A nonterminal task report older than 5 minutes is marked stale. This does not
  prove failure, completion, or continuing work. Producers may emit identical
  status heartbeats to refresh observation without sending another notice.
- Notices include their report time; delivery of a notice queued over 5 minutes
  adds an explicit stale-queue warning instead of implying current activity.

CLI success codes describe ingestion/inspection, not successful human delivery.
`emit` without `--queue-only` attempts currently due deliveries and includes an
acceptance/retry/dead summary. `status` includes generic delivery error codes.
Worker shutdown/restart is explicit; no hidden daemon is created.

## Development

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src scripts
python scripts/build_zipapp.py
python dist/agent-action-notifier.pyz demo
```

CI runs the stdlib suite and zipapp smoke test on Linux/macOS/Windows and Python
3.10/3.12/3.14. SMTP/subprocess tests are mocked; webhook tests use real loopback
HTTP. This is not an end-to-end test against a user's mailbox, desktop, Codex,
Claude, or ChatGPT. Review [CONTRIBUTING.md](CONTRIBUTING.md) before contributing.

This is an independent community tool, not an official OpenAI or Anthropic
integration. External host APIs can change; review the date-checked official
sources linked in the integration guide.
