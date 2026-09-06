# Conversational memory

Ordinary Telegram chat keeps the last few turns as context, so follow-up
questions work without repeating yourself:

```
you:  what's the capital of Japan?
bot:  Tokyo.
you:  and its population?          <- "its" resolves to Tokyo
bot:  About 14 million in the city proper.
```

This applies **only to plain chat** (the `ChatAgent`). Explicit commands --
`/rag`, `/search`, `/mem`, `/doc`, `/cron`, `/review` -- are always
single-shot and ignore conversation history. Nothing said in chat is written
to the `/mem` / `/rag` knowledge base or to Qdrant; promotion stays manual.

## Commands

| Command | Effect |
| --- | --- |
| `/new` | Start a new conversation -- clears the stored turns for this chat. |
| `/reset` | Alias for `/new`. |

## Configuration (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENCLAW_CONVERSATION_MEMORY_ENABLED` | `true` | `false` = strict single-turn, no storage. |
| `OPENCLAW_CONVERSATION_STORE_PATH` | `/workspace/.openclaw/conversations` | One `<chat_id>.json` file per chat. |
| `OPENCLAW_CONVERSATION_HISTORY_TURNS` | `6` | Number of user+assistant exchanges replayed. |
| `OPENCLAW_CONVERSATION_CONTEXT_CHARS` | `6000` | Character budget; oldest exchanges are dropped first. |
| `OPENCLAW_CONVERSATION_RETENTION_HOURS` | `72` | A chat's history older than this is ignored and swept. |

Changes take effect on `docker compose restart openclaw-telegram`.

## Design

- **Storage.** One JSON file per `chat_id`, written atomically (temp + rename).
  Separate from `task_history.jsonl` (which only ever holds bounded metadata,
  never conversation bodies) and from Qdrant.
- **Isolation.** A chat only ever sees its own file.
- **Replay.** `ChatAgent.run` calls `ConversationMemory.load(chat_id)` and
  passes the window to `LlmClient.chat(history=...)`, which builds
  `[system, <history>, user]`. The history is in the request payload, so a
  configured model fallback sees exactly the same context.
- **Truncation is deterministic.** Last `HISTORY_TURNS` exchanges, then whole
  exchanges dropped from the front until under `CONTEXT_CHARS` (at least one
  exchange is always kept).
- **Retention.** On read, a file whose `updated_at` is older than
  `RETENTION_HOURS` is treated as empty. `ConversationMemory.sweep()` deletes
  such files.
- **Disabled mode.** `load` returns nothing and `record` is a no-op; no files
  are created.

## Validation

`tests/test_conversation_memory.py` covers roundtrip, chat isolation, the
turn window, the char budget, `/new` clear, disabled mode, expiry, sweep,
corrupt-file recovery, and the `ChatAgent` wiring. `tests/test_telegram_gateway.py`
covers the `/new` and `/reset` command replies.
