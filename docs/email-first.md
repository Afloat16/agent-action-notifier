# Email-first human input

[简体中文](email-first.zh-CN.md) · [README](../README.md) · [Security](../SECURITY.md)

Give the person the needed steps in the email. Use ordinary replies for ordinary
questions, and send official login/action links when the work must happen on a
site. There is no requirement to return to GPT for every interaction. A required
platform confirmation form or handoff still has to happen on its original surface.

This is the **0.2.0 notification/reply-input layer**, not an email approval agent:
no inbox polling, automatic GPT attachment, login automation, or upstream resume
is included. A separately authorized host must verify the owner's intent and
apply the original confirmation policy before using a reply.

## 1. Review and try the event locally

Install `aan` as described in the README, or replace it with
`python /absolute/path/agent-action-notifier.pyz` in these commands.

```sh
aan --db ./email-first-demo.db --channels stdout --include-action emit --file examples/email-action.json
aan --db ./email-first-demo.db status
aan --db ./email-first-demo.db replies list
```

This uses stdout and a local database, without email, credentials, or desktop
commands. The example asks for a non-sensitive PDF/DOCX preference. The action is
stored after conservative redaction; `--include-action` does not put its text into
stdout or desktop notices. Review the event yourself before sending it.

## 2. Configure and send an email deliberately

Use a separate real deployment database, shared by the producer and worker.
Configure your own mail provider and reply mailbox first. These addresses/settings
are placeholders; `.env.example` is never loaded automatically.

```sh
export AAN_DB=/absolute/private/path/state.db
export AAN_CHANNELS=email
export AAN_INCLUDE_ACTION=1
export AAN_SMTP_HOST=smtp.example.com
export AAN_SMTP_PORT=465
export AAN_SMTP_SECURITY=ssl
export AAN_SMTP_FROM=agent-notices@example.com
export AAN_SMTP_TO=owner@example.com
export AAN_SMTP_REPLY_TO=agent-replies@example.com
# Supply SMTP username/password securely through your environment or secret manager.
aan worker --watch
```

In a second terminal with the same producer/database settings:

```sh
aan emit --queue-only --file examples/email-action.json
aan status
```

Global CLI options go before the command. `--include-action` can replace
`AAN_INCLUDE_ACTION=1`; `--channels email` can replace `AAN_CHANNELS=email`.
Channels and action opt-in are chosen when a notice is queued. Setting them only
on the worker cannot add action content or email delivery to an already queued
generic/stdout notice. Use new IDs for a new real run; do not reuse a resolved
request ID. Keep the worker running in the foreground.

FROM, TO, and REPLY_TO each accept one bare ASCII mailbox. A notice with an
included `reply_prompt` requires `AAN_SMTP_REPLY_TO`; it is not silently copied
from FROM or TO. Configure the reply mailbox yourself. The notifier does not read
it. For STARTTLS use `starttls` and your provider's port; plaintext fallback is
not supported. Account credentials and persistent-access setup stay with you.

If a reply-enabled email becomes `dead` with `email_reply_configuration_required`,
configure a valid reply mailbox, then use `aan retry-dead` to requeue still-current
notices. No SMTP send was attempted without the required reply address.

Check a harmless notice in the actual inbox and inspect `aan status` before
relying on this deployment. `smtp_accepted` proves server acceptance only.

## 3. Put full steps and official links in the event

The version-1 canonical event gains an optional `action`, only on
`human_input_required`:

- `kind`: `login`, `question`, `review`, `external_action`, or `host_confirmation`
- `steps`: 1–12 nonempty strings, at most 500 characters each
- `reply_prompt`: optional ordinary-answer instructions, at most 400 characters
- `completion_hint`: optional explanation of how the host checks completion,
  at most 400 characters; it does not change task state
- The entire event remains limited to 16 KiB. Unknown fields are rejected.

`--include-action` transmits reviewed structured action text in **email only**.
It is off by default and separate from `--include-summary`. Do not embed links
in action text: conservative redaction removes them. Put a reviewed official
ordinary page in the top-level `action_url`, which requires an exact allowed
HTTPS host. No userinfo, query, fragment, nonstandard port, magic-login URL,
bearer token, or credential-bearing path is allowed. Subdomains are separate hosts.
URL validation does not establish that an arbitrary path is safe.

For example, a `login` action must have `action_url`. Review and save this as
`login-action.json` only for a workflow that really needs GitHub login:

```json
{
  "version": 1,
  "event_id": "github-example:login-needed",
  "task_id": "github-example",
  "type": "human_input_required",
  "request_id": "github-login",
  "status": "waiting",
  "action_url": "https://github.com/login",
  "action": {
    "kind": "login",
    "steps": [
      "Open the official GitHub sign-in page linked in this email.",
      "Sign in directly on GitHub using your own account.",
      "Complete any passkey or multi-factor check in that browser.",
      "Never send passwords, passkeys, recovery codes, or security codes by email."
    ],
    "completion_hint": "The authorized host checks its actual login state before continuing."
  }
}
```

```sh
aan --channels email --include-action --action-host github.com emit --queue-only --file login-action.json
```

The [GitHub sign-in page](https://github.com/login) is an ordinary official page,
not a tokenized sign-in link. The example does not configure GitHub or verify a
session. Do not invent a task-specific official action URL: confirm it with the
service and give the person the remaining steps in the email. Use
`host_confirmation` to explain a mandatory confirmation surface, with a safe
official page if one exists; it cannot turn an email answer into that confirmation.

## 4. Reply and import the actual message

An email containing a `reply_prompt` includes a 24-hex request reference, current
revision, and a line beginning `AAN-REPLY`. Use **Reply** on that notice. Copy its
whole marker to the first line of the reply, then put your ordinary answer below
it. For illustration only, replace the placeholders with the exact line from the
notice:

```text
AAN-REPLY <24-hex-request-reference> <current-revision>
PDF
```

Do not forward the notice, attach files, paste secrets, or answer by editing a
quoted notice. Export/download the actual received reply as `message.eml`, with
its original headers and plain-text body, then explicitly import it:

```sh
aan --db /absolute/private/path/state.db replies ingest --file message.eml
aan --db /absolute/private/path/state.db replies list
aan --db /absolute/private/path/state.db status
```

Import requires one exact `In-Reply-To` identifying a previously SMTP-accepted
reply-enabled notice, a still-pending current request revision, and the exact
first-line marker (24 lowercase hex characters and a positive revision).
The `.eml` limit is 65,536 bytes; the fresh answer must be nonempty and at most
2,000 characters. Exactly one `Message-ID`, `In-Reply-To`, and `From` is required.
Supported content is plain text or `multipart/alternative` with at most two
direct parts and exactly one `text/plain` part. Attachments, nested MIME,
HTML-only content, malformed/ambiguous messages, and recognized
automatic/forwarded mail are rejected. Detection does not catch all forgery or
automation. A quoted old marker cannot answer a newer revision. These checks
correlate input; they do not authenticate its author.

Successful import stores conservatively redacted **unverified pending input**.
It never resolves a request, grants approval, executes an action, or resumes work.
Ingest reports `state: pending_unverified` and `can_authorize: false`. Listing
returns up to 100 newest pending inputs. Changed or resolved requests mark
associated inputs obsolete; `replies list --include-obsolete` includes them for
inspection. There is no `--verified` flag. A trusted host must separately
establish the owner's authenticated intent for that exact request/revision, validate the
answer, honor cancellation/confirmation policy, and only then report actual
lifecycle changes through canonical events. This project does not supply that
host bridge.

## Why headers are not permission

`From`, request markers, Message-ID, and `In-Reply-To` can be copied or forged.
DKIM/DMARC and `Authentication-Results` are mail-authentication signals, not proof
that the owner approved an action. Even a message labelled SENT with a matching
From address can be API-inserted mailbox data, as documented by
[Gmail's label rules](https://developers.google.com/workspace/gmail/api/guides/labels#types_of_labels).
It follows that mailbox retrieval/labels alone cannot establish owner intent.
Do not turn any of these into
automatic approval or a caller-supplied verification flag.

Official references checked 2026-10-05:

- [RFC 5322 §3.6.4](https://www.rfc-editor.org/rfc/rfc5322.html#section-3.6.4):
  message/reply identification, not owner authentication
- [RFC 3834 §5](https://www.rfc-editor.org/rfc/rfc3834.html#section-5): automatic-response signals
- [RFC 8601 §7](https://www.rfc-editor.org/rfc/rfc8601.html#section-7): forged/misleading authentication headers
- [Gmail `messages.insert`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/insert):
  direct mailbox insertion is distinct from sending a message
- [Gmail system labels](https://developers.google.com/workspace/gmail/api/guides/labels#types_of_labels):
  SENT is also assigned to inserted messages whose From matches the user

SMTP and parser tests are synthetic/mocked. They do not establish delivery to
your inbox, email-client compatibility, owner identity, or end-to-end agent resume.
