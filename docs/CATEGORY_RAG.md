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

- `/cancel` drops a waiting file (a document then goes to the general
  knowledge base).
- If you don't answer within `OPENCLAW_CATEGORY_PENDING_TTL_SECONDS`
  (default 10 min), a waiting document is filed into the general knowledge
  base automatically.

New category names are created on first use. Names are normalized
(whitespace collapsed, case-insensitive) and mapped to a stable collection
id such as `oc_cat_work-notes_1a2b3c4d`.

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

## Manage categories

```
/cat list     Show categories and their collection names
/cat help     Usage
```

## How photos are indexed

A photo has no text, so the configured vision model
(`OPENCLAW_VLM_MODEL` / `OPENCLAW_VLM_BASE_URL`) is asked to produce a
detailed description plus a verbatim transcription of any visible text.
That text is what gets embedded; the original image is kept alongside it in
`inbox/categories/<slug>/media/` and referenced in the chunk payload.

Image indexing quality depends entirely on the vision model. See
[the vision model section in DEPLOYMENT](DEPLOYMENT.md) and
`.env.example` (`OPENCLAW_VLM_*`, `--profile vision`).

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
| `OPENCLAW_CATEGORY_COLLECTION_PREFIX` | `oc_cat_` | Prefix for category collections |
| `OPENCLAW_CATEGORY_INBOX_DIRNAME` | `categories` | Sub-dir of the inbox |
| `OPENCLAW_CATEGORY_REGISTRY_PATH` | `/workspace/inbox/.openclaw/categories.json` | Shared registry file |
| `OPENCLAW_CATEGORY_PENDING_TTL_SECONDS` | `600` | Two-step wait before falling back |
| `OPENCLAW_CATEGORY_MAX_NAME_CHARS` | `40` | Category name length limit |
| `OPENCLAW_CATEGORY_IMAGE_MAX_TOKENS` | `600` | Max tokens for the image description |
