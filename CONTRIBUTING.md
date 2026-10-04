# Contributing

Keep this project small, notification-only, and dependency-light. Open an issue
or draft pull request with a reproducible synthetic workflow. Avoid uploading
real credentials, email addresses, private logs, or databases.

Run the stdlib tests, compilation, zipapp build and demo documented in README.
Add regression tests for interrupted/repeated flows, durable restart behavior,
false state claims, obsolete deliveries, redacted errors, and hook output.
Mock SMTP and desktop execution; live sends and system changes need explicit
operator authorization. Preserve zero third-party runtime dependencies.

Never add auto-approval, credential extraction, session scraping, implicit
installation/services, insecure TLS fallback, or misleading completion/read
receipts. New host integrations need current official sources, version notes,
data minimization, explicit lifecycle mappings, and honest test coverage.
