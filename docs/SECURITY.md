# Security And Privacy

This repository is intended to be safe for public sharing only when private runtime state is excluded.

## Never Commit

- `.env` or any file containing real Telegram bot tokens.
- `OPENCLAW_GATEWAY_TOKEN` values.
- Telegram chat ids or user ids.
- SSH usernames, passwords, private keys, jump hosts, or internal IP addresses.
- `workspace/` runtime data, inbox files, document review files, task history, or personal memory exports.
- `gateway-data/` Gateway state, device identity, dashboard sessions, or SQLite files.
- Qdrant snapshots or database volumes.
- Ollama or HuggingFace model caches.
- Backup archives such as `.tar.gz`, `.zip`, `.sqlite`, `.db`, or `.jsonl`.

## Safe To Commit

- Source code in `app/`, `runtime/`, `scraper/`, and `whisper/`.
- `compose.yaml` when it uses environment variables and localhost bindings.
- `.env.example` with blank token fields.
- Documentation that uses placeholders such as `<gb10-host>` and `<user>`.
- Small synthetic fixtures that contain no personal data.

## Recommended Deployment Posture

- Bind local AI services to `127.0.0.1`.
- Use Telegram chat id allowlisting.
- Use SSH tunnels for Gateway dashboard access.
- Generate a new random Gateway token per deployment.
- Rotate Telegram and Gateway tokens immediately if they were ever committed.

## Pre-Publish Scan

Run this before pushing:

```bash
rg -n "(<known-private-password-fragment>|<known-private-host>|<known-private-ip>|<known-private-user>@|<known-private-chat-id>|<known-private-token-fragment>)" . \
  -g '!docs/SECURITY.md' \
  -g '!docs/PUBLISH_CHECKLIST.md' \
  -g '!PUBLICATION_PRIVACY_REVIEW.md'
find . -type f \( -name '.env' -o -name '*.sqlite' -o -name '*.db' -o -name '*.jsonl' -o -name '*.tar.gz' -o -name '*.pyc' -o -name '*.exp' \) -print
```

Expected result: no private deployment hits and no private runtime state files.

Broad security audits may search for words such as `TOKEN`, `TELEGRAM`, `chat_id`, or `password`, but those are not pass/fail checks for this repository because they also match normal source identifiers and placeholder examples.
