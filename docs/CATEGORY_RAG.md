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
#WorkNotes this is the Q1 planning doc
#機房 server rack photo, note the cabling
```

A category name is **one word, no spaces** (`WorkNotes`, `work-notes`,
`工作筆記`); a name with a space is refused. A multi-word name created before
this rule keeps working -- refer to it in `[brackets]` (`#[Bus trip]`) and
rename it with `/cat rename [Bus trip] bustrip`. Any text after the
name is kept as a note and (for photos) used as extra context for the vision
model.

### 2. Two-step (send file, then the category)

Send the file with no `#` caption. The bot replies with a 檔案｜選擇分類
card: tap one of the existing categories, tap **+ New category** (the bot
asks for the name and opens the reply box), or just type a name. The
buttons disappear once the file is filed, cancelled or expired.

**Type just the name** (e.g. `trip`), not `#trip` and not `#trip <note>`.
The two-step reply has no note field -- a reply that starts with `#`/`＃` is
parsed leniently (the `#` is stripped, and the category is the first word) so
`#trip <anything>` still resolves to `trip`, but the `<anything>` is dropped,
not attached as a note. If you want a note, use the caption shortcut instead.

- `/cancel`, the **Cancel** button, or a bare `cancel` / `skip` drops a
  waiting file (a document then goes to the general knowledge base).
- If you don't answer within `OPENCLAW_CATEGORY_PENDING_TTL_SECONDS`
  (default 10 min), a waiting document is filed into the general knowledge
  base automatically.

### 3. Reply to any text message with `#<name>`

Reply to **any** text message -- your own, another bot's, or one posted
by an external script (e.g. a Google Apps Script that calls
`sendMessage` with the bot's own token to post a video summary) -- with
`#<name>` and the *replied-to* message's text gets filed into that
category. No file upload needed; the text itself becomes the document.

```
(some earlier message with the content you want saved)
you (replying to it): #trip
```

This exists specifically because a bot can never receive its own
outgoing messages back as input -- Telegram's Bot API only delivers
messages sent *to* the bot as updates, never an echo of what the bot
itself sent. Replying is the way to pull a specific message in on
demand. A trailing note works the same as the caption shortcut above.

If a bot has [video summary relay](VIDEO_SUMMARY_RELAY.md) configured, the
"external script" case above is exactly how a Gemini-generated video
summary gets filed: send a bare YouTube link, wait for the summary to come
back, then reply to it with `#<name>`.

If a URL is present -- either in the note after the category (e.g.
`#video-notes https://youtu.be/abc123`) or embedded in the replied-to
text itself (e.g. a video summary that lists its own source link) -- it
is recorded as a distinct `URL:` line in the stored document and becomes
the document's citation name, so a later `/rag #<name>` answer's
`Sources:` line shows the actual link instead of the generic "telegram
reply" label.

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
/rag #WorkNotes summarise the Q1 plan
/rag #all  where is the rack diagram
```

- `/rag #<name> <question>` searches **only** that category.
- `/rag #all <question>` searches every category and merges the results.
- `/rag <question>` with no `#` searches the default
  `personal_tracker_memory` + `personal_knowledge_base` collections **and**
  every category (the top 3 hits of each, like `#all`), since most saved
  material ends up in a category. `OPENCLAW_RAG_INCLUDE_CATEGORIES=false`
  restores the older behaviour of leaving categories out. A `tag:` filter
  keeps the search to tracker memory, where tags live.
- `/rag tag:<word> <question>` and `/rag since:YYYY-MM-DD [before:YYYY-MM-DD]
  <question>` are separate, unrelated prefixes: they scope the *default*
  (no `#`) search and do not apply to `#<category>` collections (`tag:`
  because they have no `tags` field; `since:`/`before:` would technically
  work since category items have `created_at` too, but the prefix is only
  recognized before a `#<category>` token, not combined with one -- see
  `docs/TRACKER_MEMORY.md`).

`/rag source:<text> <question>` uses only material whose source -- the
original filename, link, title or stored name shown on the `Sources:` line --
contains `<text>` (case-insensitive). It works on its own, before a
`#<category>` or `#all`, and alongside `tag:`/`since:`/`before:`:

```
/rag source:q1-plan what is still open?
/rag source:youtu.be #影片筆記 what did the talk say about pricing?
```

`/rag digest` is a daily report rather than a question: every document
added yesterday (in `OPENCLAW_CRON_TIMEZONE`) to the knowledge base or any
category, one sentence each, grouped by category. `/mem` notes are not
included. It always reports, including "No new knowledge was added
yesterday.", so it suits a morning push:

```
/cron add daily 07:05 知識｜昨日新增 :: /rag digest
```

`/rag digest week` is the weekly version: the seven days up to yesterday
(Sunday morning covers Sunday–Saturday), titled 知識｜本週新增, capped at 20
documents (the rest are counted as "…and N more"):

```
/cron add weekly sun 07:05 知識｜本週新增 :: /rag digest week
```

Every `/rag` answer ends with a `Sources:` line naming the source documents
the answer drew from. The name shown is the uploader's original filename when
known (recorded in the `.meta.json` sidecar at upload) -- or a replied-to
message's URL, when one was recorded -- otherwise the document's first
heading, otherwise the stored filename.

## Manage categories

```
/cat list                        Show categories and their collection names
/cat rename <old> <new>          Rename a category
/cat merge <source> <target>     Combine two categories into one
/cat delete <name>               Delete a category (asks first)
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

`/cat delete <name>` first shows the category's size (files and chunks) with
**Delete #name** / **Keep it** buttons; nothing changes until you tap
Delete. Then its inbox folder (documents, sidecars and media), its Qdrant
collection and its registry entry are removed. It can't be undone -- use it
for a category created by mistake whose content is already elsewhere; use
`/cat merge` to keep the content.

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
