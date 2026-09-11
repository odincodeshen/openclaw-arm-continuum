# Tracker memory: list, complete, delete

`/mem` used to be write-only: content went into Qdrant and you could never see
it again. It now supports listing, marking done, and deleting, plus inline
`due:` / `tag:` metadata -- turning `personal_tracker_memory` into an actual
tracker instead of a write-only log.

## Commands

```text
/mem <content>
Save a memory item. Add due:YYYY-MM-DD and/or one or more tag:<word> tokens
anywhere in the text; they are stripped from the saved text and stored as
structured fields.

Example: /mem renew passport due:2026-12-01 tag:admin
Example: /mem call the dentist tag:health tag:this-week
Example: /mem OpenClaw preference: use /mem for memory writes.

/mem list
List active items, soonest due date first, undated items last.

/mem list done
List completed items.

/mem done <id>
Mark an item done (moves it from the active list to the done list).

/mem rm <id>
/mem delete <id>
Delete an item permanently.
```

`<id>` is the short ID shown by `/mem list` and after every `/mem` save (the
first 8 hex characters of the underlying Qdrant point ID).

## Metadata syntax

- `due:YYYY-MM-DD` -- only the **first** token that parses as a real ISO date
  is consumed. An invalid value (`due:tomorrow`) or a second `due:` token is
  left in the saved text untouched, since it likely wasn't meant as metadata.
- `tag:<word>` -- every occurrence is consumed; a word is whitespace-delimited
  (no spaces inside a tag).
- Tokens must be their own whitespace-delimited word (`due:2026-09-20`, not
  `due:2026-09-20,` or embedded mid-word). Order and position in the message
  do not matter.
- Content that is *only* metadata (no text left after stripping) is rejected:
  "Add some content to remember, not just due:/tag: metadata."

## Reserved sub-commands

`list`, `done`, and `rm` / `delete` are reserved only as the exact first
whitespace-delimited word of the content. `/mem listen to the new episode`
still saves normally -- `listen` is not `list`.

## Data model

Stored in `OPENCLAW_TRACKER_COLLECTION` (default `personal_tracker_memory`),
same collection `/mem` always used. New payload fields on top of the existing
`text` / `source` / `kind` / `created_at`:

| Field | Type | Meaning |
| --- | --- | --- |
| `short_id` | string | first 8 hex chars of the point ID; how `/mem done` / `/mem rm` address an item |
| `status` | `"active"` \| `"done"` | |
| `due` | `"YYYY-MM-DD"` or absent | |
| `tags` | list of strings, or absent | |
| `updated_at` | epoch seconds | set on write and on `/mem done` |

`/mem list` / `done` / `rm` never touch the vector -- listing is a payload
`scroll` filter (`kind=tracker_memory`, `status=...`), done is a payload merge
(`QdrantClient.set_payload`), delete is `QdrantClient.delete_points`. Plain
`/rag` (no category) still vector-searches this collection as before, so
saved items remain findable by meaning, not just by browsing the list.

## Foundation for proactive reminders

This is the data model the next feature (cron scanning `due` / `status` to
push "these are due this week" without being asked) builds on -- see
`docs/FUTURE_TODO.md` "Personal Memory Deepening".

## Validation

- `tests/test_memory_write.py` -- metadata parsing, write/list/done/rm, the
  reserved-word edge case, unknown-ID handling. Unit-level with an in-memory
  fake Qdrant.
- `tests/test_qdrant_client.py` -- the new `scroll_by_filters` / `set_payload`
  / `delete_points` / `upsert_text(point_id=...)` methods, HTTP-call level
  with a mocked `request_json`.
- `tests/test_scenarios_integration.py::TrackerMemoryManagementScenario` --
  the full write/list/done/rm round trip against a real Qdrant (L2).
