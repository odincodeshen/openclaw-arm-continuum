# Conversational memory

Ordinary Telegram chat keeps the last few turns as context, so follow-up
questions work without repeating yourself:

```
you:  what's the capital of Japan?
bot:  Tokyo.
you:  and its population?          <- "its" resolves to Tokyo
bot:  About 14 million in the city proper.
```

When a conversation runs longer than that window, the turns rolling out of it
are folded into a running **summary** instead of being silently dropped, so
older context is compressed, not lost. `/keep` lets you pin a fact that stays
in context regardless of window size, for something you don't want compressed
away or forgotten mid-conversation.

This applies **only to plain chat** (the `ChatAgent`). Explicit commands --
`/rag`, `/search`, `/mem`, `/doc`, `/cron`, `/review` -- are always
single-shot and ignore conversation history. Nothing said in chat is written
to the `/mem` / `/rag` knowledge base or to Qdrant; promotion stays manual --
`/keep` pins a fact for *this conversation*, not forever. For something that
should survive `/new` or matters long-term, use `/mem` instead.

## Commands

| Command | Effect |
| --- | --- |
| `/new` | Start a new conversation -- clears the stored turns, summary, and pinned facts for this chat. |
| `/reset` | Alias for `/new`. |
| `/keep <fact>` | Pin a fact so it always rides along in context, even after it would otherwise roll into the summary. Cleared by `/new`. |
| `/history` | Read-only preview of what's currently stored for this chat: pinned facts, the rolling summary, and the raw recent turns. Changes nothing. |

## Configuration (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENCLAW_CONVERSATION_MEMORY_ENABLED` | `true` | `false` = strict single-turn, no storage. |
| `OPENCLAW_CONVERSATION_STORE_PATH` | `/workspace/.openclaw/conversations` | One `<chat_id>.json` file per chat. |
| `OPENCLAW_CONVERSATION_HISTORY_TURNS` | `6` | Number of user+assistant exchanges replayed verbatim. |
| `OPENCLAW_CONVERSATION_CONTEXT_CHARS` | `6000` | Character budget for the replayed window; oldest exchanges trimmed first. |
| `OPENCLAW_CONVERSATION_RETENTION_HOURS` | `72` | A chat's history older than this is ignored and swept. |
| `OPENCLAW_CONVERSATION_SUMMARY_ENABLED` | `true` | `false` = turns rolling out of the window are just dropped (pre-rolling-summary behaviour), no extra LLM call. |
| `OPENCLAW_CONVERSATION_SUMMARY_MAX_TOKENS` | `200` | `max_tokens` for the summarization call. |
| `OPENCLAW_CONVERSATION_SUMMARY_MAX_CHARS` | `2000` | Hard cap on the stored summary's length, regardless of what the model returns. |
| `OPENCLAW_CONVERSATION_KEEP_MAX_ITEMS` | `20` | Max pinned facts per chat; oldest pins drop first once full. |

Changes take effect on `docker compose restart openclaw-telegram`.

## Design

- **Storage.** One JSON file per `chat_id`, written atomically (temp + rename):
  `{"version", "turns", "summary", "pinned", "updated_at"}`. Separate from
  `task_history.jsonl` (bounded metadata only, never conversation bodies) and
  from Qdrant.
- **Isolation.** A chat only ever sees its own file.
- **Replay.** `ConversationMemory.load(chat_id)` returns an optional leading
  synthetic `[user, assistant]` pair carrying pinned facts and/or the summary
  (`"(context, not a new message)\n\nPinned facts:\n...\n\nSummary of earlier
  conversation:\n..."` / `"Noted, I'll keep that in mind."`), followed by the
  raw recent window. `ChatAgent.run` passes the whole thing to
  `LlmClient.chat(history=...)`, which builds `[system, <history>, user]` --
  the history rides in the request payload, so a configured model fallback
  sees exactly the same context.
- **Rolling summary.** `record()` keeps the window at exactly
  `HISTORY_TURNS` exchanges. When a new exchange would push it over, the
  *oldest* exchange is folded into the running summary via one
  `LlmClient.chat()` call (the same client `ChatAgent` already has -- no new
  model policy), then dropped from the raw window. The prompt includes the
  existing summary (if any) plus the exchange being folded in, and asks for
  a concise (~120 word), factual, third-person update. The summary is
  hard-capped to `SUMMARY_MAX_CHARS` regardless of what comes back.
- **Failure is non-fatal.** If the summarization call raises or returns
  nothing, the *previous* summary is kept as-is and the overflow exchange is
  just dropped for that cycle (same as `SUMMARY_ENABLED=false`) -- a
  summarization hiccup never blocks the chat reply itself, since it runs
  after the reply has already been sent.
- **`/keep`** appends to a capped list (`KEEP_MAX_ITEMS`, oldest first out)
  that rides in every `load()` regardless of the window. It has no
  independent expiry -- only `/new` clears it.
- **`/history`** calls `ConversationMemory.preview(chat_id)`, a read-only
  sibling of `load()`: same expiry/disabled checks, but returns the raw
  `{"pinned", "summary", "turns"}` snapshot instead of an LLM-ready message
  list (no synthetic context pair, no window truncation -- the whole stored
  turn list, since it's already capped to `HISTORY_TURNS` by `record()`).
  `format_history_preview()` in the gateway renders it as plain text.
- **Truncation within the window is still deterministic**, same as before:
  the raw window is capped to `HISTORY_TURNS` exchanges, then whole exchanges
  dropped from the front until under `CONTEXT_CHARS` (at least one exchange
  is always kept).
- **Retention.** On read, a file whose `updated_at` is older than
  `RETENTION_HOURS` is treated as empty (summary and pinned facts included).
  `ConversationMemory.sweep()` deletes such files.
- **Disabled mode.** `load` returns nothing and `record`/`pin` are no-ops; no
  files are created.
- **Backward compatible.** A file written before this feature (no `summary`/
  `pinned` keys) loads fine; the missing fields default to empty.

## Validation

`tests/test_conversation_memory.py` covers roundtrip, chat isolation, the
turn window, the char budget, `/new` clear, disabled mode, expiry, sweep,
corrupt-file recovery, `ChatAgent` wiring, rolling-summary folding (with and
without an `llm`, summarizer failure, the char cap), pinned facts (cap,
survival alongside real turns, rejection when empty/disabled),
`HistoryPreviewTest` (`preview()` returns pinned+summary+turns, `None` when
empty/disabled/expired, never mutates the stored file), and loading a
pre-rolling-summary (v1) file. `tests/test_telegram_gateway.py` covers the
`/new`, `/reset`, `/keep`, and `/history` command replies, plus
`format_history_preview()`'s rendering (all three sections, turns-only,
empty snapshot).
`tests/test_scenarios_integration.py::ChatMemoryRollingSummaryScenario`
exercises the summarization prompt through a real `LlmClient.chat()` call
against the fake OpenAI-shaped server (L2) -- not just a stand-in, an actual
request/response round trip -- and confirms the resulting summary is
replayed in the next turn's context.
