# Agent integrations

`aan` is a notification and local request-state utility. It observes explicitly
supplied events; it does not automatically connect to ChatGPT, discover agents,
read conversation/session files, approve permissions, answer questions, or resume
upstream work. A reviewed canonical action can provide full email steps, official
links, and ordinary reply prompts. There is no blanket requirement to return to
GPT; mandatory host confirmation surfaces still apply. Imported `.eml` replies
remain unverified pending input. See [email-first setup](email-first.md)
([简体中文](email-first.zh-CN.md)) for the trusted-host boundary and runnable steps.

## Documentation and version snapshot

The external contracts below were checked on **2026-10-04** against official
documentation. The release snapshots were [Codex CLI
0.160.0](https://github.com/openai/codex/releases/tag/rust-v0.160.0) and [Claude Code
2.1.289, released October 3,
2026](https://code.claude.com/docs/en/changelog). These are research
snapshots, not a claim that this project ran those agents or established a minimum
compatible version. Check `codex --version` and `claude --version`, review the
linked contracts, and verify hooks in the actual host before relying on alerts.

The examples assume `aan` is installed and on the hook process's `PATH`. Otherwise
replace `aan` with its absolute executable path. Replace the database path with
one absolute path shared by the hooks and worker. Keep existing hook configuration
when adding these entries; do not overwrite unrelated settings.

## Start delivery separately

Hooks persist events and queued notifications, then return. They do not wait for
SMTP or desktop commands. Run this in a separate foreground process:

```sh
aan --db /absolute/path/state.db worker --watch
```

The default channel is `stdout`. To select email and desktop delivery, make
`AAN_CHANNELS=email,desktop` available to the **producer/hook processes**, which
choose the channels when enqueueing. Configure email settings in the worker's
environment as described in the README. Setting channels only on the worker does
not change notifications that were already queued for another channel.

```sh
export AAN_CHANNELS=email,desktop
aan --db /absolute/path/state.db worker --watch
```

Launching an agent from a different terminal, IDE, or service may not inherit that
environment. Verify it there, or put `--channels email,desktop` before `hook` in
each hook command. Neither notification transport acceptance nor exit status
proves an inbox delivery, a visible desktop alert, or a person reading it.

## Codex CLI command hooks

Add the following entries to `~/.codex/hooks.json` (or merge them into an existing
file). Codex also supports a project `.codex/hooks.json` and inline TOML hooks.
Non-managed hooks need trust review; project hooks also require project trust.
[Official Codex hook configuration](https://learn.chatgpt.com/docs/hooks#where-codex-looks-for-hooks)

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "aan --db /absolute/path/state.db hook codex",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "aan --db /absolute/path/state.db hook codex",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Codex command hooks supply one JSON object on stdin. `PermissionRequest` is an
approval signal. `Stop` is only a turn-ended signal. The adapter returns `{}` on
stdout, exits 0 for accepted events, and never returns a permission decision.
Keep these short enqueue hooks synchronous: background hooks can be cancelled
when a session ends. [Official input/output and background-hook
contract](https://learn.chatgpt.com/docs/hooks#common-input-fields)

**Legacy `notify` is a different interface:** Codex passes JSON as a command-line
argument and documents only `agent-turn-complete`. Do not connect it directly to
`aan hook codex`, which reads stdin, or expect it to report permission requests.
[Official `notify` documentation](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications)

## Claude Code command hooks

Merge this `hooks` object into `~/.claude/settings.json`, or the intended
project/local settings scope. Verify the entries with `/hooks` in Claude Code.
[Official setup guide](https://code.claude.com/docs/en/hooks-guide#set-up-your-first-hook)

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "aan --db /absolute/path/state.db hook claude",
            "timeout": 5
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "^AskUserQuestion$",
        "hooks": [
          {
            "type": "command",
            "command": "aan --db /absolute/path/state.db hook claude",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "aan --db /absolute/path/state.db hook claude",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Claude supplies JSON on stdin. `PermissionRequest` reports permissions;
`PreToolUse` for `AskUserQuestion` reports a structured question. The adapter
observes without returning answers or decisions. In a non-interactive run without
a permission host, an alert does not create somewhere to answer: upstream can
deny the request. Sandbox network prompts do not fire `PermissionRequest`.
[Official Claude hook contracts](https://code.claude.com/docs/en/hooks#permissionrequest)

Claude's `Notification` event is a separate interface. This adapter does not map
it. A custom bridge can map selected notification types to canonical events, but
delayed notifications are not comprehensive or authoritative request-state
tracking. Do not register every notification as a human-input request: some are
authentication, completion, or quota-status signals.
[Official notification types](https://code.claude.com/docs/en/hooks-guide#get-notified-when-claude-needs-input)

## What the supplied adapters record

| Adapter input | Canonical event | Meaning |
| --- | --- | --- |
| Either agent: `PermissionRequest` | `human_input_required`, `waiting` | Use the host's required permission flow |
| Claude: `PreToolUse`, `tool_name=AskUserQuestion` | `human_input_required`, `waiting` | Answer through the host's supported input flow |
| Either agent: `Stop` | `task_status`, `unknown`, or existing `waiting`/`blocked` | A turn ended; task completion is unconfirmed |
| Other well-formed hook events | Ignored | Not supported by these adapters |

The adapters use a hashed session identity for `task_id`. They do not forward
tool inputs, question text, commands, transcripts, working-directory paths, or
assistant responses as notification content. A vendor-provided `tool_use_id`,
when available, supports duplicate suppression. Otherwise the adapter hashes
the event inputs into **30-second time buckets**. Repeats across a bucket boundary
may alert again; identical simultaneous requests may collapse. This is bounded
noise suppression, not an authoritative inventory of all outstanding requests.

The supplied hooks do not generate structured actions or collect email answers.
To send full reviewed steps/questions, a host must emit canonical `action` data
deliberately. `--include-action` does not extract private tool inputs or create
an upstream answer channel from a generic hook.

Generic hooks do not give this utility a complete upstream resolution feed.
Neither `Stop`, a successful unrelated tool call, time passing, nor a local alert
acknowledgement proves a request was resolved. For authoritative tracking, use a
host/wrapper with stable request identities and emit its lifecycle events instead
of mixing them with the same session's heuristic hook requests.

## Canonical events from your own workflow

`aan emit` accepts one JSON object from stdin, or `--file PATH`. Use `--queue-only`
when a separate worker handles delivery:

```sh
aan --db /absolute/path/state.db emit --queue-only <<'JSON'
{
  "version": 1,
  "event_id": "workflow:build-42:review-requested",
  "task_id": "workflow:build-42",
  "type": "human_input_required",
  "request_id": "workflow:build-42:review-1",
  "status": "waiting",
  "independent_work": false,
  "summary": "A review is needed in the original workflow."
}
JSON
```

Only emit resolution after the authoritative workflow confirms that particular
request is no longer pending:

```sh
aan --db /absolute/path/state.db emit --queue-only <<'JSON'
{
  "version": 1,
  "event_id": "workflow:build-42:review-resolved",
  "task_id": "workflow:build-42",
  "type": "request_resolved",
  "request_id": "workflow:build-42:review-1",
  "outcome": "handled"
}
JSON
```

`handled` records lifecycle resolution, not permission approval or task success.
Resolution does not send an answer, approve a tool, or resume an agent. If no
other requests remain, task state becomes `unknown`; emit a separate
authoritative `task_status` when the workflow actually runs or ends.

The version-1 contract is:

- Every event requires `version=1`, `event_id`, `task_id`, and `type`. IDs are
  strings of 1-128 ASCII letters/digits/`_`/`.`/`:`/`-`, beginning with a letter or
  digit. A retry must reuse its `event_id` and identical content.
- `human_input_required` requires `request_id`; `status` is `waiting` (default)
  or `blocked`. `independent_work` defaults to `false`; set it `true` only when
  the producer knows useful independent work can continue.
- `request_resolved` requires the same `task_id`/`request_id` and an `outcome` of
  `handled`, `cancelled`, or `superseded`. Do not include summary, action URL,
  action, status, or independent-work data in resolution events.
- `task_status` requires `status`: `running`, `waiting`, `blocked`, `completed`,
  `failed`, `cancelled`, or `unknown`. It has no `request_id` or `outcome`.
- `summary` is optional, at most 400 characters. Notifications omit it by default;
  `--include-summary`/`AAN_INCLUDE_SUMMARY=1` opts into heuristic-redacted content,
  which is not a guarantee that sensitive text has been removed.
- `action_url` is optional. It must be HTTPS on an exact host allowed by
  `--action-host` or `AAN_ACTION_HOSTS`; it cannot have userinfo, a query,
  fragment, or a nonstandard port. Use an ordinary authenticated review page,
  never a secret-bearing URL. Subdomains are separate hosts.
- `action` is optional and valid only on `human_input_required`. Its `kind` is
  `login`, `question`, `review`, `external_action`, or `host_confirmation`;
  `steps` contains 1–12 nonempty strings of at most 500 characters each.
  Optional `reply_prompt` and `completion_hint` strings are at most 400
  characters each. `login` requires a validated top-level `action_url`.
  The complete event remains limited to 16 KiB. Fields are conservatively
  redacted before storage; links must use `action_url`, not action text.
  `--include-action`/`AAN_INCLUDE_ACTION=1` includes reviewed action text only
  in email, and only when chosen by the producer at enqueue time.

For a reply-enabled action, configure `AAN_SMTP_REPLY_TO` in the email worker.
The email supplies a request reference, revision, first-line `AAN-REPLY` marker,
and correlation headers. `aan replies ingest --file message.eml` parses the
actual local reply; `aan replies list` exposes imported **unverified** input.
Neither command polls the inbox, resolves the request, authenticates an owner,
approves an action, or resumes the host. Only a trusted host can verify owner
intent and apply its original confirmation policy. Full setup and official
sources are in the [email-first guide](email-first.md).

Example separate status event:

```json
{
  "version": 1,
  "event_id": "workflow:build-42:finished",
  "task_id": "workflow:build-42",
  "type": "task_status",
  "status": "completed"
}
```

Emit that only when the defined workflow objective has actually completed, not
merely because an assistant stopped speaking.

## Local authenticated webhook

For a producer that can send canonical events, `aan serve` listens on
`127.0.0.1:8765` by default and runs delivery in a separate worker thread. It
requires a user-supplied `AAN_WEBHOOK_TOKEN` of at least 32 non-whitespace
characters. Configure it through your own protected environment; do not commit
it, put it in event JSON, URLs, screenshots, or logs.

```sh
aan --db /absolute/path/state.db serve --port 8765
```

- `POST /v1/events`: `Authorization: Bearer <AAN_WEBHOOK_TOKEN>` and
  `Content-Type: application/json`; body is one canonical version-1 event.
- `GET /v1/status`: the same bearer header; authenticated read-only state.
- Events are limited to 16 KiB. Accepted ingestion returns HTTP 202; this means
  stored/queued, not delivered. Retry uncertain ingestion with the same canonical
  event identity and content. Conflicting reuse of an event ID is rejected.

**Do not point Claude HTTP hooks directly at `/v1/events`.** Claude posts raw
vendor hook JSON, while this route accepts only the canonical schema above. A
user-built bridge must authenticate, map/minimize the raw hook into a canonical
event, and forward it. Its reply to Claude should be empty successful 2xx or `{}`,
not the notifier ingestion record or permission fields. Claude documents bearer
header environment interpolation with `headers` plus `allowedEnvVars`.
[Official HTTP-hook contract](https://code.claude.com/docs/en/hooks#http-hook-fields)

There is no raw-vendor webhook route, cloud tunnel, browser-based approval UI, or
automatic host transport ownership in this project.

## Optional Codex app-server bridge contract

This section specifies a **bridge you must implement in an authorized host**;
`aan` does not launch, attach to, or take over app-server. A host can observe its
own transport and emit canonical events. Do not read session files to reconstruct
the protocol. App-server uses bidirectional JSON-RPC, with JSONL on stdio and an
initialization handshake. Its approval/input messages are requests, not ordinary
notifications. [Official app-server
protocol](https://learn.chatgpt.com/docs/app-server#protocol)

Generate version-matched schemas when implementing a bridge:

```sh
codex app-server generate-json-schema --out ./codex-schemas
```

For the checked 0.160.0 snapshot, consult the pinned [server request
schema](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/app-server-protocol/schema/json/ServerRequest.json)
and [server notification
schema](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/app-server-protocol/schema/json/ServerNotification.json).
The request-user-input surface is experimental; generated fields can differ
from rolling documentation or later releases.

### Observe requests

Map only these server-initiated methods to `human_input_required`:

| Upstream method | Generic safe summary |
| --- | --- |
| `item/commandExecution/requestApproval` | A command or network approval is needed |
| `item/fileChange/requestApproval` | A file-change approval is needed |
| `item/permissions/requestApproval` | A permissions decision is needed |
| `item/tool/requestUserInput` | A human answer is needed |
| `mcpServer/elicitation/request` | An MCP interaction is needed |

Use `params.threadId` for task correlation and the **top-level JSON-RPC `id`**
for request correlation. The ID may be an integer or string: retain its type.
Namespace it with a stable host/server-instance scope and thread identity, and
persist the mapping before publishing the event. Normalize or hash arbitrary
upstream IDs into canonical valid IDs; do not use `itemId` alone, since more than
one request can concern the same item. Preserve mappings during reconnects and
distinguish actual new requests from replayed ones.

For example, a host can record the mapping
`(host-demo, thr_example, integer 77) -> codex:host-demo:77`. Use a separate stable
`event_id` for each lifecycle transition. The transport payload remains inside
the authorized host; the notifier only needs its minimized event.

```json
{
  "version": 1,
  "event_id": "codex:host-demo:77:required",
  "task_id": "codex:thr_example",
  "type": "human_input_required",
  "request_id": "codex:host-demo:77",
  "status": "waiting",
  "independent_work": false,
  "summary": "A human answer is needed in the original Codex host."
}
```

Do not forward approval reasons, shell commands, question contents, requested
schemas, authentication messages, or arbitrary upstream URLs by default. A host can
deliberately add separately reviewed non-sensitive `action` steps and ordinary
questions for email; it must supply any official action URL safely and obtain
authenticated owner intent before processing a reply. Do not
map every server request to human-input-required: some request host execution or
authentication plumbing rather than a decision by a person.

### Observe resolution

The authoritative closure notification has this shape:

```json
{
  "method": "serverRequest/resolved",
  "params": {
    "threadId": "thr_example",
    "requestId": 77
  }
}
```

Look up the original scoped request mapping using `threadId` and typed
`requestId`, then emit `request_resolved` through `aan emit --queue-only` or
`POST /v1/events`. The notification means the request was answered **or cleared**;
it does not carry an approval verdict or reason. The host should use its own
recorded lifecycle to select `handled` for a processed response (including a
decline), `cancelled` for cancellation, or `superseded` for a request cleared by a
replacement/turn transition. Do not invent the cause from this notification
alone. [Official resolution behavior](https://learn.chatgpt.com/docs/app-server#approvals)

If the host recorded a processed response, the example mapping is:

```json
{
  "version": 1,
  "event_id": "codex:host-demo:77:resolved",
  "task_id": "codex:thr_example",
  "type": "request_resolved",
  "request_id": "codex:host-demo:77",
  "outcome": "handled"
}
```

Unknown/orphaned resolution IDs require host reconciliation, not a guessed match
or fabricated request. The bridge must retain an outbox/retry record for
canonical ingestion failures. If it loses the upstream feed, report uncertainty
instead of asserting that work is running or every request is closed.

The host continues to own all original UI, permission enforcement, human
responses, and JSON-RPC replies. Receiving a webhook acknowledgement or recording
resolution in `aan` must never trigger an upstream approval/answer automatically.

### Observe task status separately

`turn/started` can establish a running turn; `turn/completed` describes the end of
that turn. A multi-turn workflow must not become `completed` just because that
notification arrived. Emit `unknown` after an ordinary ended turn, preserving
outstanding request state, until the host has evidence of the actual task's
next state. Use terminal task statuses only when the host's defined objective
is confirmed completed, failed, or cancelled. Define turn-scoped tasks explicitly
if that narrower boundary is what you intend to track.

## Other hosts and notification availability

Claude Agent SDK hosts can instrument their own `canUseTool` callback for
permissions and `AskUserQuestion`, and emit canonical request/resolution events
around their human UI. This requires a host integration; the notification-only
hook does not create that UI or return a response.
[Official SDK user-input API](https://code.claude.com/docs/en/agent-sdk/user-input)

Desktop alerts appear on the machine/session running the delivery worker:

- Linux requires `notify-send` and a user-session notification service. Headless
  containers and remote SSH sessions commonly lack it. The [freedesktop
  specification](https://specifications.freedesktop.org/notification/latest-single/)
  explicitly does not guarantee a notification service is available.
- macOS uses `osascript`; notification settings can suppress it even when the
  command succeeds. Check Script Editor notification permission and run a local
  test. [Official Claude notification
  troubleshooting](https://code.claude.com/docs/en/hooks-guide#get-notified-when-claude-needs-input)
- Windows uses existing Windows PowerShell 5.1/.NET `NotifyIcon.ShowBalloonTip`
  on an interactive logged-in desktop. This is a finite notification-area
  balloon request with OS-limited text, not a native WinRT toast or modal dialog.
  No app registration, installation or policy override is performed. See
  [desktop prerequisites and official API references](desktop.md). Actual popup
  display has not been established by mocked tests.

For remote agents, run an authorized producer bridge and a reachable notifier in
your own deployment. A cloud worker's desktop alert does not automatically reach
your laptop or phone. Email requires separately configured SMTP service and
recipient; this project does not configure or authenticate an email account for
you.
