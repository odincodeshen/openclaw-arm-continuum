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

/mem list tag:<word>
/mem list done tag:<word>
Scope the list to items carrying that exact tag. Combine with "done" in
either order.

Example: /mem list tag:work
Example: /mem list done tag:health

/mem done <id>
Mark an item done (moves it from the active list to the done list).

/mem rm <id>
/mem delete <id>
Delete an item permanently.

/mem snooze <id> <3d|1w|YYYY-MM-DD>
Push an item's due date out. Accepts a relative amount (3d = 3 days,
1w = 1 week) or an absolute YYYY-MM-DD date. Works on items with no due
date yet (gives them one), and reactivates a done item back to active --
no need to /mem done then re-save to push something out.

Example: /mem snooze a1b2c3d4 3d
Example: /mem snooze a1b2c3d4 2026-10-01

/mem edit <id> <new text>
Replace an item's text (and re-embed it, so /rag keeps finding it by
meaning). due:/tag: tokens in the new text override the stored ones;
omitting them keeps whatever was already there. status and created_at
are untouched.

Example: /mem edit a1b2c3d4 renew passport and international driving permit
Example: /mem edit a1b2c3d4 renew passport due:2027-01-15

/mem digest
A reminder summary: overdue items, items due soon, and stale undated
items. Meant to be run on a schedule (see "Proactive reminders" below),
but works fine typed directly too.

/mem digest tag:<word>
Scope the digest to one tag -- handy for a per-topic cron job so a
"work" reminder and a "home" reminder don't get mixed into one push.

Example: /mem digest tag:work
```

`<id>` is the short ID shown by `/mem list` and after every `/mem` save (the
first 8 hex characters of the underlying Qdrant point ID).

The same tag scoping works on semantic search too, and combines with a
date range:

```text
/rag tag:<word> <question>
/rag since:YYYY-MM-DD <question>
/rag before:YYYY-MM-DD <question>
/rag since:YYYY-MM-DD before:YYYY-MM-DD <question>
```

`tag:` scopes the *default* `/rag` search (no `#<category>`) to tracker
items carrying that exact tag, and skips the knowledge-base collection
entirely (it has no `tags` field, so there is nothing there a tag filter
could match). `since:`/`before:` filter by `created_at` (when the item was
saved / ingested) and apply to **both** tracker and knowledge -- both have
`created_at`. `since:` is inclusive, `before:` is exclusive (so
`before:2026-09-15` means "before that day, not including it"); give both
for a range. All three can be combined, in any order, as leading tokens
before the question -- e.g. `tag:work since:2026-09-01`. An invalid date
(`since:not-a-date`) is left in the question text untouched, same
philosophy as `/mem`'s `due:` parsing.

`/rag #<category> ...` is a separate, unrelated mechanism and unaffected --
category collections don't carry tags, and these prefixes are only
recognized before a `#<category>` token, not after it. See
`docs/CATEGORY_RAG.md`.

Example: `/rag tag:work what did I save about the deadline?`
Example: `/rag since:2026-09-01 what have I saved this month?`

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

`list`, `done`, `rm` / `delete`, `snooze`, `edit`, and `digest` are reserved
only as the exact first whitespace-delimited word of the content. `/mem
listen to the new episode` still saves normally -- `listen` is not `list`.

`tag:<word>` inside `/mem list ...` or `/mem digest ...` is a scope filter,
not new content -- it matches a tag exactly, using Qdrant's list-payload
match semantics (the tag must be one of the item's `tags`, not a substring
or a partial word). It only narrows; it never selects nothing when there
is no such tag anywhere -- that just prints "No active memory items
tagged \"...\"." or, for digest, the usual all-caught-up message.

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

`/mem list` / `done` / `rm` / `snooze` never touch the vector -- listing is a
payload `scroll` filter (`kind=tracker_memory`, `status=...`), done and
snooze are a payload merge (`QdrantClient.set_payload`), delete is
`QdrantClient.delete_points`. `/mem edit` is the one exception: it
re-embeds the new text and does a full point replace (same point ID, via
`QdrantClient.upsert_text(point_id=...)`), because the old vector would
otherwise keep matching the old wording. Plain `/rag` (no category) still
vector-searches this collection as before, so saved items remain findable
by meaning, not just by browsing the list.

## Proactive reminders

`/mem digest` categorizes every active item into:

- **Overdue** -- `due` is in the past. Always included; repeats every run
  by design (that is what a reminder is for).
- **Due in the next `OPENCLAW_MEM_DIGEST_DUE_SOON_DAYS` days** (default 7) --
  `due` is between today and the window. Also repeats every run.
- **Stale** -- no `due`, and `updated_at` (or `created_at`) is older than
  `OPENCLAW_MEM_DIGEST_STALE_DAYS` (default 14). These get a cooldown:
  once shown, `last_reminded_at` is set, and the item is skipped for
  `OPENCLAW_MEM_DIGEST_REMIND_COOLDOWN_DAYS` (default 7) so an undated
  item doesn't get nagged about every single run.

An item with a `due` far beyond the window, or an undated item touched
recently, appears in none of the sections -- it isn't due yet.

If none of the three sections have anything, the answer is a plain
"You're all caught up" message and the `SkillResult` is marked
`suppress_if_routine`. **Interactively this still shows the message as
normal; it only changes what a scheduled caller (cron) does with it** --
see below.

### Wire it to a daily push

```text
/cron add daily 08:00 Memory digest :: /mem digest
```

A tag-scoped digest works the same way as its own job, so "work" and
"home" reminders can land as separate, independently-silenced pushes:

```text
/cron add daily 08:00 Work digest :: /mem digest tag:work
/cron add daily 09:00 Home digest :: /mem digest tag:home
```

The cron worker treats a `suppress_if_routine` result as a no-op: it still
records the run (`lastRunStatus: "skipped"`, `consecutiveSkipped`
increments), but does **not** push a Telegram message -- so a day with
nothing due or stale is silent, not a "nothing to report" ping every
morning.

### Data model addendum

| Field | Type | Meaning |
| --- | --- | --- |
| `last_reminded_at` | epoch seconds, or absent | set by `/mem digest` when a *stale* item is included; the cooldown check |

This is the data model the earlier structured-memory work put in place
(`status`, `due`, `tags`, `short_id`) plus `last_reminded_at` for the
cooldown -- see `docs/FUTURE_TODO.md` "Personal Memory Deepening".

## Validation

- `tests/test_memory_write.py` -- metadata parsing, write/list/done/rm, the
  reserved-word edge case, unknown-ID handling, `MemorySnoozeTest`
  (relative/absolute targets, reactivating a done item, undated items,
  invalid input), `MemoryEditTest` (text replacement, re-embedding,
  preserving status/created_at, due/tag override vs. preserve), tag-filter
  tests on `/mem list` and `/mem digest` (including the `FakeQdrant`
  list-payload match semantics), and `MemoryDigestTest` (overdue/due-soon/
  stale categorization, cooldown, suppression). Unit-level with an
  in-memory fake Qdrant.
- `tests/test_qdrant_client.py` -- the new `scroll_by_filters` / `set_payload`
  / `delete_points` / `upsert_text(point_id=...)` methods, and `search`'s
  optional `filters` param and `since`/`before` range params (omitted,
  each alone, combined, and alongside `filters`), HTTP-call level with a
  mocked `request_json`.
- `tests/test_category_rag_retrieve.py` -- `split_rag_filter_prefix`
  parsing (`tag:`/`since:`/`before:`, any order and combination, an
  invalid date or repeated key stopping the prefix run, "not a prefix
  mid-sentence"), `/rag tag:<word>` only searching the tracker collection,
  `/rag since:`/`before:` searching both tracker and knowledge with the
  range applied, and `tag:` + `since:` combining, unit-level with a
  `FakeQdrant` that records the filters/since/before each collection was
  searched with.
- `tests/test_scenarios_integration.py::TrackerMemoryManagementScenario` --
  the full write/list/done/rm round trip, the digest overdue/due-soon
  categorization, and `test_list_and_digest_tag_filter_against_real_qdrant`
  (proves Qdrant's list-payload match -- "the tag is one of the item's
  tags" -- against a real server, not just the fake), against a real
  Qdrant (L2).
- `tests/test_scenarios_integration.py::KnowledgeAndMemoryScenario::
  test_rag_tag_prefix_scopes_to_tracker_items_with_that_tag_against_real_qdrant`
  -- two differently-tagged `/mem` items, `/rag tag:work ...` and
  `/rag tag:home ...` each only surface their own item, against a real
  Qdrant (L2).
- `tests/test_scenarios_integration.py::KnowledgeAndMemoryScenario::
  test_rag_date_range_filters_against_real_qdrant` -- proves the
  `created_at` range condition (`since:` inclusive, `before:` exclusive)
  against a real Qdrant: an item saved "now" is included by
  `since:today`, excluded by `before:today`, excluded by `since:tomorrow`,
  and included by a range spanning both sides of today (L2).
- `tests/test_cron_worker.py::RunDynamicJobTest` /
  `WriteGatewayRunbackTest` -- a `suppress_if_routine` result is recorded but
  not pushed; the ok/error/skipped status is three-way and each counter
  resets correctly.
