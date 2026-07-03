# Publish Checklist

Before publishing `openclaw-arm-continuum` to GitHub:

- Confirm `.env` is not present.
- Confirm `workspace/` contains only `.gitkeep` files.
- Confirm `gateway-data/` contains only `.gitkeep`.
- Confirm no backup archives are present.
- Confirm no SQLite, database, JSONL, log, or pyc files are present.
- Confirm no private SSH host, user, password, IP, Telegram chat id, or token is present.
- Confirm README says `OpenClaw Arm Continuum`.
- Confirm license is Apache-2.0.
- Confirm `docs/PLATFORMS.md` marks non-DGX profiles as planned/experimental until verified.

Suggested checks:

```bash
rg -n "(<known-private-password-fragment>|<known-private-host>|<known-private-ip>|<known-private-user>@|<known-private-chat-id>|<known-private-token-fragment>)" . \
  -g '!docs/SECURITY.md' \
  -g '!docs/PUBLISH_CHECKLIST.md' \
  -g '!PUBLICATION_PRIVACY_REVIEW.md'
find . -type f \( -name '.env' -o -name '*.sqlite' -o -name '*.db' -o -name '*.jsonl' -o -name '*.tar.gz' -o -name '*.pyc' -o -name '*.exp' \) -print
```

Expected result: no private deployment hits and no private runtime state files.

The pass/fail scan should look for known private values, not generic variable names. Generic names such as `OPENCLAW_GATEWAY_TOKEN` and `chat_id` are expected in the source tree.
