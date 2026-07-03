# Publication Privacy Review

This release directory was created as a clean public candidate from the private working deployment.

## Included

| Path | Reason |
|---|---|
| `app/` | Runtime source code. |
| `runtime/`, `scraper/`, `whisper/` | Docker build contexts. |
| `compose.yaml` | Generic compose stack with localhost bindings and environment variables. |
| `.env.example` | Blank token fields only. |
| `README.md`, `docs/` | Generic public documentation. |
| `tests/openclaw-rag-fixture.pdf` | Synthetic fixture with no personal data. |

## Excluded From Public Release

| Private path in working tree | Reason |
|---|---|
| `scripts/*.exp` | Contains private SSH passwords, jump host, internal host, and deployment-specific commands. |
| `backups/` | Backup archives may contain `.env`, runtime state, and old private configs. |
| `backup/BACKUP_INDEX.md` | Contains private implementation history, local paths, and host details. |
| `remote_patch/` | Development patch staging, redundant with release source and may contain deployment assumptions. |
| `remote-configs/` | Private/old remote service configs. |
| `workspace/` | Runtime inbox, documents, memories, cron state, and task history. |
| `gateway-data/` | Gateway identity, state, dashboard sessions, and SQLite files. |

## Sanitized

| Item | Sanitization |
|---|---|
| README host details | Replaced private host, jump host, username, and home path with generic placeholders. |
| HuggingFace cache path | Replaced `/home/<user>/.cache/huggingface` with `${HF_CACHE_DIR:-./.cache/huggingface}`. |
| Tokens | Kept only blank fields in `.env.example`. |

## Before Publishing

Run:

```bash
rg -n "(<known-private-password-fragment>|<known-private-host>|<known-private-ip>|<known-private-user>@|<known-private-chat-id>|<known-private-token-fragment>)" . \
  -g '!docs/SECURITY.md' \
  -g '!docs/PUBLISH_CHECKLIST.md' \
  -g '!PUBLICATION_PRIVACY_REVIEW.md'
find . -type f \( -name '.env' -o -name '*.sqlite' -o -name '*.db' -o -name '*.jsonl' -o -name '*.tar.gz' -o -name '*.pyc' -o -name '*.exp' \) -print
```

Expected result: no private deployment hits and no private runtime state files.

Do not use broad terms such as `TOKEN`, `TELEGRAM`, or `chat_id` as a pass/fail scan. Those terms are expected to appear in source code and examples as variable names.
