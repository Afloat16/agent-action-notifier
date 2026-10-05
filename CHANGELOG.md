# Changelog

## 0.2.0 — 2026-10-05

- Structured human-input actions with bounded steps, reply prompts and completion hints
- Explicit email-only action opt-in; desktop/stdout action text remains generic
- Safe allowlisted official links, SMTP Reply-To and request/revision correlation
- Local `.eml` reply import/list as unverified input, without inbox polling or auto-approval
- Bilingual email-first setup and trusted-host security guidance

## 0.1.0 — 2026-10-04

- Canonical CLI and authenticated loopback webhook ingestion
- Durable SQLite request state and at-least-once outbox
- Deduplication, retry/backoff, crash lease recovery and stale-state flags
- TLS SMTP, local macOS/Linux alerts, Windows notification-area balloons and stdout
- Notification-only Codex/Claude command hooks and explicit host contract
- Generic-by-default notices, optional redacted summaries, validated review links
- Dependency-free zipapp, bilingual setup guide and multi-platform CI
