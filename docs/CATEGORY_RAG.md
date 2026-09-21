# Category RAG

Keep different kinds of uploaded material in **separate, non-overlapping
knowledge bases** so a query about one topic never pulls in unrelated
documents. Each category is its own Qdrant collection.

## Add material to a category

Upload a photo or a document to the Telegram bot, then name the category
one of two ways:

### 1. Caption shortcut (single message)

Put `#<name>` at the start of the caption:

```
#工作筆記
#[Work Notes] this is the Q1 planning doc
#機房 server rack photo, note the cabling
```

`#[...]` or `#{...}` lets a category name contain spaces. Any text after the
name is kept as a note and (for photos) used as extra context for the vision
model.

### 2. Two-step (send file, then the category)

Send the file with no `#` caption. The bot replies asking for a category;
your next plain-text message is taken as the category name.

**Reply with just the name** (e.g. `trip`), not `#trip` and not `#trip <note>`.
The two-step reply has no note field -- a reply that starts with `#`/`＃` is
parsed leniently (the `#` is stripped, and the category is the first word) so
`#trip <anything>` still resolves to `trip`, but the `<anything>` is dropped,
not attached as a note. If you want a note, use the caption shortcut instead.

- `/cancel` drops a waiting file (a document then goes to the general
  knowledge base).
- If you don't answer within `OPENCLAW_CATEGORY_PENDING_TTL_SECONDS`
  (default 10 min), a waiting document is filed into the general knowledge
  base automatically.

New category names are created on first use. Names are normalized
(whitespace collapsed, case-insensitive) and mapped to a stable collection
id such as `oc_cat_work-notes_1a2b3c4d`.

### Sending several photos at once

Selecting multiple photos in Telegram and sending them together as one
album with one caption works correctly: **all** of them are filed into that
category, not just one. This is handled specially because Telegram itself
only attaches the caption to one message in the album -- every photo
arrives as its own message sharing a `media_group_id`, and only one of
them actually carries the `#<name>` text. The bot buffers photos that
share a `media_group_id` for `OPENCLAW_MEDIA_GROUP_FLUSH_SECONDS` (default
2.5s) after the last one arrives, then files the whole album together
using whichever message actually had the caption.

If the album has no caption at all, the two-step flow (below) still
applies, but as **one** prompt for the whole album -- reply once with the
category name and every photo in the album goes there, not one prompt per
photo.

## Query a category

```
/rag #工作筆記 What are the open action items?
/rag #[Work Notes] summarise the Q1 plan
/rag #all  where is the rack diagram
```

- `/rag #<name> <question>` searches **only** that category.
- `/rag #all <question>` searches every category and merges the results.
- `/rag <question>` with no `#` is unchanged: it searches the default
  `personal_tracker_memory` + `personal_knowledge_base` collections and
  does **not** touch category collections.
- `/rag tag:<word> <question>` and `/rag since:YYYY-MM-DD [before:YYYY-MM-DD]
  <question>` are separate, unrelated prefixes: they scope the *default*
  (no `#`) search and do not apply to `#<category>` collections (`tag:`
  because they have no `tags` field; `since:`/`before:` would technically
  work since category items have `created_at` too, but the prefix is only
  recognized before a `#<category>` token, not combined with one -- see
  `docs/TRACKER_MEMORY.md`).

Every `/rag` answer ends with a `Sources:` line naming the source documents
the answer drew from. The name shown is the uploader's original filename when
known (recorded in the `.meta.json` sidecar at upload), otherwise the
document's first heading, otherwise the stored filename.

## Manage categories

```
/cat list                        Show categories and their collection names
/cat rename <old> <new>          Rename a category
/cat merge <source> <target>     Combine two categories into one
/cat help                        Usage
```

`/cat rename` only changes the stored display name -- the underlying
collection is untouched, so nothing gets re-indexed and it's instant. Because
the collection is keyed by the *original* name, a query using the old name
still resolves to the same category after a rename (it just reports the new
display name back) -- renaming doesn't break old references, it only changes
what's shown.

`/cat merge <source> <target>` moves every file from `<source>` into
`<target>`'s inbox directory (updating each `.meta.json` sidecar), deletes
`<source>`'s Qdrant collection, and removes it from the registry. The moved
files are picked up by the memory watcher's next poll (~10s) and indexed
fresh into `<target>` -- merge does not copy Qdrant points directly, so
there is only ever one ingest path to reason about. This is the fix for a
category that was accidentally split in two (e.g. a two-step reply typo --
see the `/cat` two-step note above): find both with `/cat list`, then merge
the wrong one into the right one instead of losing the indexed content.

## How photos are indexed

A photo has no text, so the `vision` model is asked to produce a detailed
description plus a verbatim transcription of any visible text. That text is
what gets embedded; the original image is kept alongside it in
`inbox/categories/<slug>/media/` and referenced in the chunk payload.

Image indexing quality depends entirely on this model. Configure it in
`models.json` (a model with role `vision` — see `app/models.example.json`),
or with the `OPENCLAW_VLM_*` shortcut in `.env.example`. To keep a text-only
main model, run the second vLLM: `docker compose --profile vision up -d
openclaw-vllm-vision`. If no vision model is configured, image indexing
falls back to the main text model, which cannot read images.

## Storage layout

```
/workspace/inbox/
  .openclaw/categories.json          registry: display name <-> slug <-> collection
  .staging/telegram/                 two-step uploads waiting for a category
  categories/<slug>/                 documents (and image description .md files)
  categories/<slug>/media/           original images
  categories/<slug>/<file>.meta.json per-file sidecar (category, image_path, note)
```

The memory watcher ingests anything under `categories/<slug>/` into
`oc_cat_<slug>`; dotfiles/dot-dirs under the inbox are never ingested.

## Settings

| Env var | Default | Meaning |
|---------|---------|---------|
| `OPENCLAW_CATEGORY_RAG_ENABLED` | `true` | Master switch for the whole feature |
| `OPENCLAW_CATEGORY_COLLECTION_PREFIX` | `oc_cat_` | Prefix for category collections. **Running more than one bot profile? Give each one a different prefix** -- a category collection's name comes from the category's display name, not the profile, so two profiles both using the default and both creating a same-named category (e.g. `trip`) silently share one Qdrant collection. See `docs/PROFILES.md`. |
| `OPENCLAW_CATEGORY_INBOX_DIRNAME` | `categories` | Sub-dir of the inbox |
| `OPENCLAW_CATEGORY_REGISTRY_PATH` | `/workspace/inbox/.openclaw/categories.json` | Shared registry file |
| `OPENCLAW_CATEGORY_PENDING_TTL_SECONDS` | `600` | Two-step wait before falling back |
| `OPENCLAW_CATEGORY_MAX_NAME_CHARS` | `40` | Category name length limit |
| `OPENCLAW_CATEGORY_IMAGE_MAX_TOKENS` | `600` | Max tokens for the image description |
| `OPENCLAW_MEDIA_GROUP_FLUSH_SECONDS` | `2.5` | How long to wait after the last photo in an album before filing the whole group |
