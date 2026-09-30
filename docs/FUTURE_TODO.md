# Future TODO List

This document tracks candidate work items for `openclaw-arm-continuum`.
Current release: **v1.23**.

The runtime is intentionally stable and text-first. The items below are
future-facing and should be implemented incrementally without breaking the
existing Telegram, memory, RAG, search, cron, and Gateway workflows.

## Delivered since v1.2

The scheduling language in the sections below was written against a
v1.2 baseline. What has actually shipped since then:

- **v1.3 (branch, no release tag) — multi-model deployment configs.**
  Bounded multi-model engineering-review workflow; O6 and DGX multi-model
  deployment configurations; `models.*.example.json` catalog examples.
  These commits merged forward and shipped as part of v1.4.
- **v1.4 — Category RAG + vision decoupling.** `/rag #<category>` and
  `/rag #all` retrieval, per-category Qdrant collections, two-step
  caption/pending upload flow, `/cat` command, full-width `＃` support,
  and a `Sources:` citation line on every `/rag` answer. Introduced the
  `VisionClient` seam (`app/openclaw_runtime/vision_client.py`) that
  decouples the Telegram image path from any specific model or endpoint.
- **v1.5 — multi-model catalog + expert routing.** `models.json` catalog
  (`OPENCLAW_MODEL_CATALOG`), per-skill routing to an expert model, and an
  optional LLM intent classifier for plain-language messages that match no
  command or keyword (`OPENCLAW_INTENT_ROUTER_ENABLED`). See
  `docs/MODEL_ROUTING.md`.
- **v1.6 — English-only user-facing strings.** Finished the i18n pass:
  Category RAG replies, ingest confirmations, and `/help` are English;
  Chinese-only test-step docs removed. Input recognition (full-width `＃`,
  Chinese category names and queries) is unchanged.
- **v1.7 — chat continuity, ops lifecycle, CI + test layers.**
  Per-chat conversational memory (`ChatAgent` replays recent turns; `/new`;
  `OPENCLAW_CONVERSATION_*`; `docs/CONVERSATION_MEMORY.md`). Telegram photos
  now go through the `VisionClient` seam (no longer bypassed to the text
  model); `scripts/vision_smoke.py`; `docs/VISION_SETUP.md`.
  `bin/openclawctl` + `OPENCLAW_BOOT_MODE` split core services from the model
  engine, with graceful Telegram degradation when the engine is down
  (`docs/RUNTIME_LIFECYCLE.md`). `OPENCLAW_REPLY_LANGUAGE` sets the default
  answer language. CI: `.github/workflows/ci.yml` (lint + unit + static) and
  `integration.yml` (L2 scenarios against real Qdrant with an in-process
  fake model server); `docs/TESTING.md`, `docs/CI.md`.
- **v1.8 — structured tracker memory + proactive reminders.** `/mem list
  [done]` / `done <id>` / `rm <id>`, `due:YYYY-MM-DD` and `tag:<word>`
  metadata parsing (`app/openclaw_runtime/skills/memory.py`). `/mem digest`
  categorizes overdue / due-soon / stale items; wired to a daily cron push
  (`/cron add daily 08:00 Memory digest :: /mem digest`) via a new generic
  `SkillResult.suppress_if_routine` signal so cron skips the Telegram push
  (but still records the run) when there's nothing to report. See
  `docs/TRACKER_MEMORY.md`.
- **v1.9 — chat rolling summary, category self-service, tracker CRUD.**
  Conversation memory folds overflowed turns into a running LLM-generated
  summary instead of dropping them, plus `/keep <fact>` to pin facts that
  always ride along (`OPENCLAW_CONVERSATION_SUMMARY_*`,
  `OPENCLAW_CONVERSATION_KEEP_MAX_ITEMS`); see `docs/CONVERSATION_MEMORY.md`.
  `/cat rename <old> <new>` (instant, registry-only) and `/cat merge <src>
  <dst>` (moves files, lets the memory watcher re-index them) give
  self-service Category RAG cleanup without shell access; see
  `docs/CATEGORY_RAG.md`. The two-step reply flow for a pending category
  now strips a leading `#`/`＃` before validating, fixing a case where a
  habitually-prefixed reply created a stray, fragmented category instead
  of joining the intended one. `/mem` rounds out to full CRUD: `/mem snooze
  <id> <3d|1w|YYYY-MM-DD>` pushes a due date out (also works on undated
  items, reactivates a done one) and `/mem edit <id> <new text>` replaces
  an item's text and re-embeds it without losing its short ID or history;
  see `docs/TRACKER_MEMORY.md`.
- **v1.10 — tag/date scoped retrieval, reply-based ingest, video summary
  relay.** `/mem list`/`/mem digest` and `/rag` gained a shared `tag:<word>`
  scope filter, plus `/rag since:YYYY-MM-DD [before:YYYY-MM-DD]` for
  date-range retrieval (combinable with `tag:`); `/mem archive-stale` sweeps
  stale undated items out of the default list automatically; `/history`
  previews a chat's pinned facts, rolling summary, and recent turns
  read-only. A multi-photo Telegram album's shared caption is now buffered
  and applied to every photo, fixing a case where only one photo in the
  group got filed. Every model call is now grounded with the real current
  date so the model stops mistaking real recent dates for "the future";
  `/search` now opens a pasted link directly instead of searching for its
  literal text. Category RAG gained a third ingest path: reply to **any**
  text message with `#<name>` to file that message's text without an
  upload -- built specifically so an external script that pushes a message
  using the bot's own token (which never arrives back as an update) can
  still be pulled in on demand; a URL in the reply or the replied-to text
  is captured as a distinct field and becomes the `/rag` citation. The
  inbox chunker is now header-aware, keeping a `#`/`##`/`###` section whole
  in one chunk when it fits instead of always slicing by a fixed character
  count. An optional per-bot video summary relay
  (`OPENCLAW_VIDEO_SUMMARY_RELAY_URL`) detects a bare YouTube link, hands it
  off over HTTP to an external Gemini-based summarizer, and lets the
  resulting summary flow into RAG through the same reply-ingest path; see
  `docs/VIDEO_SUMMARY_RELAY.md` and `docs/CATEGORY_RAG.md`. Also fixed a
  stale-git-tag privacy issue: the `v1.9` tag on origin still pointed at
  pre-rebase history containing a leaked identifier even though `main` had
  already been scrubbed -- retagged to the corresponding clean commit.
- **v1.16 — English-learning bot.** An opt-in, per-bot daily English
  coach (`OPENCLAW_ENGLISH_BOT_*`), built as internal milestones
  v1.11-v1.16 with no intermediate release tags. A day-of-week scheduler in
  the gateway pushes one task a day and routes the voice or text reply to
  that day's evaluator: Monday BBC podcast listening + 3 chunk extraction,
  Tuesday shadowing with a word-level (WER) comparison, Wednesday IELTS
  Speaking Part 2 with a STAR check (Part 2 + Part 3 argument check from
  week 18), Thursday British small talk with an Anchor & Bounce check and
  a text banter reply, Friday chunk activation, Saturday a cloze review
  judged by position, and Sunday a Guardian reading pick with an English
  summary plus a Traditional Chinese translation. Wednesday to Saturday
  feedback ends with a model answer to compare against. Each day's task
  can be practised repeatedly until `/Done`; voice replies under 10 seconds
  don't count. Records are owner-scoped in Qdrant, with a 21:00 sweep that
  marks unanswered days skipped. New outbound `sendAudio`, audio clipping
  in the Whisper service, and an RSS parser support it.
- **v1.17 — readable cards, word lookup, daily reports.** Every English-bot
  task and feedback message now uses one shared card layout (short Chinese
  title, English labels, Telegram HTML with collapsible example/answer), with
  model answers on Monday and Tuesday too. The English bot gained `/w` word
  lookup (offline ECDICT dictionary, Traditional Chinese, LLM only for
  in-context sense and examples; a bare 1-4 word message works without
  `/w`), an owner-scoped word list (`/vocab`) and Leitner spaced-repetition
  review (`/vocab review`, plus a Word review block on due days) -- see
  `docs/DICTIONARY.md` and the new `docs/ENGLISH_BOT.md`. The hard-coded
  "OpenClaw Daily Briefing" (which ignored its own `enabled: false` and sent
  an empty report from every bot) is removed; per-bot morning reports are now
  plain `/cron` jobs using the new `/mem upcoming` (what's due in the next few
  days) and `/rag digest` (yesterday's new knowledge, one sentence each).
  `/cron` results are only pushed, no longer saved into the inbox where they
  were being indexed back into tracker memory. Running several bots at once
  is documented in `docs/PROFILES.md`, with a tracked
  `compose.persona.example.yaml`.
- **v1.18 — quieter logs, per-evaluation review flag.** The memory watcher
  logs each skipped file once instead of on every scan (up to ~190,000
  identical lines a day per bot), the cron worker logs its job poll only
  when something changes (it had hidden a week-long Gateway 404), and every
  compose service caps its Docker log at 3 x 10 MB. English bot: a chunk's
  "needs review" flag now follows the latest attempt of each evaluation
  (Monday, Friday, Saturday), so redoing a day can clear that day's miss.
- **v1.19 — weekly recap, chunks into word review, 112-card IELTS bank.**
  Sunday now ends the English bot's week with a recap card per learner
  (days answered, chunks learned, words looked up), and moves every chunk
  not yet learned -- still flagged, or never practised -- into that
  learner's `/vocab review` queue, so missed chunks keep coming back after
  their week. The IELTS Part 2 bank grows from 12 to 112 cue cards (28 per
  category), well past its 60-card target.
- **v1.20 — sharper /rag, per-bot ops, off-peak generation.** `/rag
  source:<text>` narrows a query to one document, link or title, and plain
  `/rag` also searches every category (`OPENCLAW_RAG_INCLUDE_CATEGORIES`).
  `bin/openclawctl --profile <bot|all>` and `openclawctl profiles` run one
  bot's containers at a time. Model-heavy work can run off-peak
  (`OPENCLAW_ENGLISH_BOT_PREPARE_TIME`, `OPENCLAW_CRON_PREPARE_WINDOW`) and
  still be delivered at the usual time. Open English tasks and `/vocab
  review`s survive restarts, each user keeps one daily-task record per day,
  Monday falls back to an unused older episode when the show is on a break,
  `/w` turns away sentences, and the video relay logs every outcome and
  waits up to 600s.
- **v1.21 — Monday as intensive listening.** A review of every daily card
  from the learner's side found Monday asking them to listen without
  sending any audio: Monday now sends the selected stretch as an audio clip
  with a link to the full episode, and adds a listening check -- a gist
  question and a 3-gap dictation from the clip (graded in code, answers
  never sent to the model), with the transcript revealed in the feedback
  and a Listening line in the Sunday recap. Chunk examples are now always
  English sentences containing the phrase (they had come back in Chinese,
  which emptied Saturday's cloze), cloze matching handles irregular forms,
  `-ing`, `someone`/`one's` placeholders and bracketed optional parts, and
  Friday lists each chunk's example.
- **v1.22 — buttons, night ritual, watchdog.** Operator alerts on Telegram for problems
  that only reached a log, plus a host-side container watchdog
  (`scripts/openclaw_watchdog.py`: crash restarts, crash loops, failing
  health checks, host reboots). bot2/bot3 knowledge reports became a Sunday
  `/rag digest week`, Monday's clip is a Telegram voice message, and
  `/vocab export` sends the word list as an Anki import file. Filing an
  upload is a tap: the category prompt lists existing categories as inline
  buttons (+ New category, Cancel); `/cat delete` removes a category after a
  confirm button; new category names are one word. bot2 gained a night
  ritual (`docs/NIGHT_RITUAL.md`): four questions at 22:30, a morning
  first-thing card, weekly and monthly reports, all kept on the host.
  Then: buttons for `/vocab review` self-check, `/vocab` removal, Monday's
  gist and a `/menu`; night-report trends, Add to tomorrow's schedule and
  `/night move`; `/cat merge` keeps PDFs and drops duplicates; uploads
  waiting for a category survive restarts; a weekly watchdog health summary.

- **v1.23 — pronunciation, backups, more buttons.** 🔊 UK / US pronunciation on word lookups and
  self-check answers, from a local Kokoro TTS service (`openclaw-tts`). Then:
  nightly backups, daily cleanup and a weekly e2e run (all in the watchdog
  summary); Anki decks with audio, `/say` practice, a weekly word quiz and
  more 🔊; upload category suggestions and duplicate detection; buttons
  under `/rag` answers; tidied spoken night answers and a yearly report.

Still open, re-baselined against v1.23:

**Platform / runtime**

- Platform-aware multimodal analysis -- **partially unblocked**: the
  `VisionClient` seam (v1.4) and the model catalog (v1.5) are the seams
  this design asked for; the backend workers (`mnn-omni`, `vllm-vlm`,
  `remote-vlm`) and the registered agent are not built. On GB10 the main
  model already covers vision (see "dedicated VLM" section below), so this
  matters most for the Arm CPU-only profile.
- `arm-remote-llm` profile -- **planned only** (`docs/PLATFORMS.md`): a small
  Arm host running the bots while generation goes to a private-LAN
  inference server. Needs its compose file, `.env` example and doc, plus the
  `openclawctl status model` remote-endpoint probe below.
- Runtime lifecycle -- **first cut done** (`bin/openclawctl`,
  `OPENCLAW_BOOT_MODE`, graceful degradation when the model is stopped;
  v1.18 made the watcher/cron logs change-only and capped every Docker log
  at 3 x 10 MB; `openclawctl --profile <bot|all>` now runs one bot's
  containers at a time). Not done: the remote-endpoint probe for
  `arm-remote-llm`, per-platform model-service wiring beyond
  `openclaw-vllm`, and demo reset/seed commands.
- Platform presets -- **partially done**: O6 and DGX multi-model configs
  shipped in v1.4; the remaining profiles and per-platform smoke tests are
  not done.
- Richer multi-agent runtime -- **partially advanced**: v1.5 catalog
  routing + intent classifier cover part of "task routing policies";
  `AgentRegistry` / `TaskDispatcher` are still thin.

**Memory and knowledge**

- OCR extractor for Category RAG -- **not started**
  (`ingest_image_into_category` takes an extractor list, today only
  `vlm_description`).
- Personal memory deepening -- structured `/mem`, reminders, archival, and
  tag/date/category scoping are done; v1.17 added the `/mem upcoming` and
  `/rag digest` daily reports, `/rag source:<text>` now narrows a query
  to one document, link or title, and plain `/rag` also searches every
  category (`OPENCLAW_RAG_INCLUDE_CATEGORIES`). Open: profile show/set flows, and a real
  calendar source for the schedule report
  (today it only sees `/mem ... due:` dates, with no times).

**English-learning bot**

- Done in v1.19: the IELTS Part 2 bank is past its 60-card target (112
  cue cards, 28 per category); chunks not learned by Sunday move into the learner's `/vocab
  review` queue, so missed chunks keep coming back after their week; and
  Sunday adds a weekly recap card (days done, chunks learned, words looked
  up).
- Live verification still planned (`docs/TESTING.md`): a full real-calendar
  week (the first one starts 2026-09-28), and the week-18 Part 2+3 combo.
- Next up: a real calendar source for the schedule report (see Memory and
  knowledge above).

**Testing**

- L3 end-to-end script (`scripts/e2e_run.py`) -- written and run live (10/10 on
  bot2); still to wire into a GB10 runner on tags, plus VLM/router quality checks.

Done since the v1.6 re-baseline, for reference: Telegram conversational
memory (post-v1.6), concurrent multi-bot personas (see the section below),
and everything listed under "Delivered since v1.2" above.

## Delivered milestone: Telegram conversational memory

Shipped post-v1.6. See `docs/CONVERSATION_MEMORY.md`.

- `app/openclaw_runtime/conversation_memory.py` -- one JSON file per `chat_id`
  under `OPENCLAW_CONVERSATION_STORE_PATH`, atomic write, isolated per chat.
- `ChatAgent.run` replays the recent window via `LlmClient.chat(history=...)`;
  `OPENCLAW_CONVERSATION_HISTORY_TURNS` bounds the count,
  `OPENCLAW_CONVERSATION_CONTEXT_CHARS` the size (trimmed front-first in pairs).
- `/new` (alias `/reset`) deletes the chat's file.
- `OPENCLAW_CONVERSATION_RETENTION_HOURS` ages entries out on read + via
  `ConversationMemory.sweep()`; `OPENCLAW_CONVERSATION_MEMORY_ENABLED=false`
  restores strict single-turn.
- Only `ChatAgent` uses it. `/rag`, `/search`, `/mem` stay single-shot; the
  history lives in the request payload so a model fallback sees the same
  context; nothing is written to task history or Qdrant.
- **Rolling summary (post-v1.8):** turns rolling out of the window are folded
  into a running LLM-generated summary instead of being dropped
  (`OPENCLAW_CONVERSATION_SUMMARY_ENABLED`, default true; one `LlmClient.chat()`
  call only when the window actually overflows; fails gracefully back to
  drop-without-summary). `/keep <fact>` pins a fact that always rides along
  regardless of window size (`OPENCLAW_CONVERSATION_KEEP_MAX_ITEMS`). Both
  cleared by `/new`. See `docs/CONVERSATION_MEMORY.md`.
- **`/history` (v1.9):** read-only preview of what a chat currently has
  stored -- pinned facts, the rolling summary, and the raw recent turns.
  Changes nothing (`ConversationMemory.preview()`, a read-only sibling of
  `load()`).

## v2.0 Candidate: Platform-Aware MultimodalAnalysisAgent

Goal: add a reusable multimodal analysis agent that can select the best local
backend for each Arm Continuum deployment profile.

Groundwork already in place (see "Delivered since v1.2"): the `VisionClient`
seam (v1.4) already hides the image backend from callers, and the v1.5 model
catalog already routes each skill to a chosen model. What remains is the
backend workers, the registered agent, and dispatcher integration below.

Recommended architecture:

```text
MultimodalAnalysisAgent
  |
  +-- DGX / GB10 backend -> vLLM + formal VLM
  |
  +-- Arm CPU backend -> MNN Omni
  |
  +-- Arm gateway backend -> remote local VLM
```

MNN Omni should be treated as an Arm CPU-friendly multimodal backend, not as an
O6-only feature and not as the primary DGX / GB10 multimodal path.

MNN Omni should also be treated as a specialist multimodal backend, not as a
replacement for the platform's main text reasoning model. For example, on an
Arm CPU-only Orion O6 profile, ERNIE MoE + llama.cpp can remain the text
reasoning engine while MNN Omni handles image/audio understanding when those
inputs require a multimodal model.

Recommended backend strategy:

- DGX / GB10: use `vllm-vlm` with a formal VLM such as Qwen2.5-VL, Qwen3-VL, or
  a Llama Vision family model.
- Arm CPU-only: use `mnn-omni` as a local CPU-friendly multimodal backend.
- Arm gateway / edge host: use `remote-vlm` to route multimodal tasks to a
  trusted local GB10 / DGX VLM endpoint on the private network.

Recommended model-role split:

```text
Text chat / RAG / cron summaries
  -> platform text model
     - DGX / GB10: vLLM text or VLM text path
     - Arm CPU-only: ERNIE MoE + llama.cpp
     - Arm gateway: trusted local remote LLM

Image / screenshot / mixed media analysis
  -> MultimodalAnalysisAgent
     - DGX / GB10: vLLM VLM backend
     - Arm CPU-only: MNN Omni backend
     - Arm gateway: trusted local remote VLM backend

Voice
  -> default: Whisper / ASR -> text model
  -> future optional path: MNN Omni audio backend after explicit validation
```

Example platform settings:

```env
# Common switch
OPENCLAW_VISION_ENABLED=true
OPENCLAW_MULTIMODAL_BACKEND=mnn-omni

# Arm CPU-only, for example Orion O6
OPENCLAW_MULTIMODAL_BACKEND=mnn-omni
OPENCLAW_MNN_OMNI_BASE_URL=http://127.0.0.1:8790

# DGX / GB10
OPENCLAW_MULTIMODAL_BACKEND=vllm-vlm
OPENCLAW_VLLM_BASE_URL=http://openclaw-vllm:8000/v1
OPENCLAW_VLLM_MODEL=Qwen3-VL...

# Arm gateway routing to local GB10 / DGX VLM
OPENCLAW_MULTIMODAL_BACKEND=remote-vlm
OPENCLAW_REMOTE_VLM_BASE_URL=http://gb10.local:8000/v1
```

### Implementation Path

Implement this capability in three stages:

```text
specialist skill -> registered agent -> TaskDispatcher integration
```

#### Stage 1: Specialist Skill

Goal: prove the capability works reliably before making the orchestration layer
more complex.

In this stage, add explicit backend skills, starting with a direct skill such
as `mnn_omni_analyze` for Arm CPU-only platforms:

```text
Telegram image
  -> save image to workspace
  -> call selected multimodal backend
  -> return image summary
```

Validation:

- The selected backend can run on its target platform.
- Images can be passed into the backend worker.
- Timeouts and errors are handled clearly.
- Telegram remains responsive.
- Existing `/mem`, `/rag`, `/search`, and cron flows are not affected.

#### Stage 2: Registered Agent

Goal: turn the working skill into a first-class OpenClaw agent with explicit
identity, capabilities, and limits.

Proposed agent metadata:

```yaml
agent_id: multimodal_analysis
name: Multimodal Analysis Agent
capabilities:
  - image.describe
  - image.ocr
  - screenshot.explain
  - multimodal.summarize
backends:
  - id: vllm-vlm
    preferred_platforms:
      - dgx-spark
      - gb10
    runtime: vllm
    model_family: vlm
  - id: mnn-omni
    preferred_platforms:
      - arm-cpu-only
      - orion-o6
    runtime: mnn
    model: Qwen2.5-Omni-7B-MNN
    limits:
      max_concurrency: 1
      timeout_seconds: 300
  - id: remote-vlm
    preferred_platforms:
      - arm-gateway
      - rpi5
    runtime: openai-compatible
    target: trusted-local-vlm
```

Validation:

- AgentRegistry can list the agent and its capabilities.
- Health checks expose whether the agent is available.
- Task history can identify which agent handled the image.
- The system can keep deterministic command routes while still understanding
  this agent as a reusable capability.

#### Stage 3: TaskDispatcher Integration

Goal: let OpenClaw automatically choose the multimodal agent when the task
requires it.

Example route:

```text
User uploads a screenshot and asks what is wrong
  -> TaskDispatcher detects image input + troubleshooting intent
  -> dispatch to MultimodalAnalysisAgent
  -> optionally pass summary to RAG Agent
  -> final response through Chat Agent
```

Validation:

- Image, screenshot, and mixed text/image prompts route to the multimodal agent.
- Text-only `/mem`, `/rag`, `/search`, `/doc`, and `/cron` commands remain
  deterministic.
- Dispatcher failures fall back to explicit skill routes instead of blocking
  the Telegram runtime.
- Logs show the selected agent, reason, status, and runtime duration.

### Proposed Components

- Add backend-specific multimodal workers where needed.
- Add `openclaw-mnn-omni` worker for Arm CPU-only platforms.
- Add `vllm-vlm` backend support for DGX / GB10 formal VLM deployments.
- Add `remote-vlm` backend support for Arm gateway devices that route to a
  trusted local VLM endpoint.
- Wrap each backend behind a consistent local HTTP/API contract.
- Add a new OpenClaw skill, for example `multimodal_analyze`, with
  backend-specific adapters.
- Route Telegram image uploads to `MultimodalAnalysisAgent` when enabled.
- Keep text chat, `/mem`, `/rag`, `/search`, and cron on the existing text
  runtime unless the task explicitly requires multimodal analysis.

### Proposed API

```text
GET  /health
POST /analyze-image
POST /analyze-audio
POST /analyze-multimodal
```

### Proposed Environment Variables

```env
OPENCLAW_VISION_ENABLED=false
OPENCLAW_MULTIMODAL_BACKEND=mnn-omni

OPENCLAW_MNN_OMNI_ENABLED=false
OPENCLAW_MNN_OMNI_BASE_URL=http://127.0.0.1:8790
OPENCLAW_MNN_OMNI_MODEL_DIR=/path/to/Qwen2.5-Omni-7B-MNN
OPENCLAW_MNN_OMNI_TIMEOUT=300
OPENCLAW_MNN_OMNI_MAX_IMAGE_SIZE=1280

OPENCLAW_REMOTE_VLM_BASE_URL=
OPENCLAW_REMOTE_VLM_MODEL=
```

`OPENCLAW_VISION_ENABLED=true` should only be enabled after the selected
multimodal backend passes health checks and image smoke tests.

### MVP Scope

- Image input only for the first implementation.
- Start with one backend, likely `mnn-omni` on Arm CPU-only or `vllm-vlm` on
  GB10 depending on available hardware and model readiness.
- Keep Whisper tiny for speech-to-text on CPU-only platforms until direct
  multimodal audio is explicitly validated.
- Keep ERNIE MoE / llama.cpp as the Arm CPU-only text reasoning engine; use MNN
  Omni only when the input requires image, screenshot, or future audio
  understanding.
- Do not replace the platform's main text model.
- Use a single-flight queue or lock to avoid concurrent multimodal inference
  overloading CPU-only hosts.
- Return clear timeout/error messages without blocking the Telegram runtime.

### MNN Omni Integration Notes

The MNN Omni path should be added as a future Arm-friendly multimodal backend
with a narrow first target: Telegram image analysis on CPU-only platforms. The
implementation should avoid coupling the feature to Orion O6 specifically, even
if O6 is the first validation platform.

Recommended first implementation:

```text
Telegram image upload
  -> save image to media inbox
  -> call multimodal_analyze skill
  -> multimodal_analyze selects mnn-omni backend
  -> MNN Omni returns structured image summary
  -> Chat Agent formats a concise user-facing response
  -> optional user command can promote the result into RAG/memory
```

Recommended later implementation:

```text
Telegram voice/audio upload
  -> keep Whisper as the default reliable ASR path
  -> add optional MNN Omni audio experiment behind an explicit flag
  -> compare direct audio understanding against Whisper transcription quality
  -> enable only after accuracy and latency are acceptable on target hardware
```

Validation should confirm that MNN Omni improves image/audio capability without
making text-only OpenClaw flows slower or less reliable.

### MVP Validation

- The selected backend health endpoint returns `200`.
- A Telegram image upload produces a useful local image summary.
- The response includes:
  - main visual content
  - visible text/OCR summary when possible
  - 1-3 suggested follow-up actions or questions
- `/mem` still writes memory successfully.
- `/rag` still retrieves context successfully.
- `/search` still works.
- Cron jobs still run and push results.

## Future: OCR extractor for Category RAG image indexing

Category RAG (see `docs/CATEGORY_RAG.md`) currently indexes an image by
embedding the vision model's description + its verbatim text transcription.
That is enough for photos and light text, but dense scanned documents,
spreadsheets-as-images, and multi-column layouts lose fidelity.

`ingest_image_into_category` builds the embedded text from a list of
extractors (today only `vlm_description`). Add an optional `ocr_text`
extractor behind an env toggle:

- Candidate engines: PaddleOCR, docTR, Tesseract (CJK packs).
- Run as a small local HTTP worker with a `/ocr` endpoint, same pattern as
  the whisper/scraper workers.
- Merge OCR output with the VLM description before chunking; keep both in
  the payload for debugging.
- Only worth doing if "photos of documents" turns out to be a common
  input. Until then, a stronger `OPENCLAW_VLM_MODEL` is the better lever.

## Future: dedicated VLM (only if the main model needs it)

The v1.5-era assumption was "the GB10 model is text-first". As of 2026-09 it
is **not**: the production model (Qwen3.x) reads images, OCRs dense tables and
screenshots, and describes layout well -- verified with `scripts/vision_smoke.py`
against real Telegram photos. So `vision` deliberately shares the main model
endpoint (`OPENCLAW_VLM_BASE_URL` / `OPENCLAW_VLM_MODEL` in `.env`), and a
separate VLM server is **not** planned.

Done:

- Both image paths (Telegram photo analysis and Category RAG image indexing)
  go through the `VisionClient` seam / catalog `vision` role.
- `scripts/vision_smoke.py` -- capability probe: sends a red/green/blue test
  image and checks the description names the colours (`verdict: vision OK`),
  rather than assuming a shared endpoint means poor quality.
- `docs/VISION_SETUP.md` leads with "is a dedicated VLM even needed?" and only
  then covers `models.json` / `OPENCLAW_VLM_*` / `--profile vision`.
- `compose.yaml` `vision` profile + `models.example.json` `vision` entry stay
  available for the dedicated-VLM case.

Revisit a dedicated VLM only when a concrete weakness shows up (tiny text,
dense charts, non-CJK OCR, bounding boxes) or the main model is swapped for a
text-only one. GPU-budget lever at that point: the main model's
`--max-model-len` (currently 262144) dominates the KV-cache reservation;
dropping it frees room for a second 7B VLM on the same GB10.

Still open: image/PDF smoke tests in the GPU e2e layer (L3), not just the
manual script.

## Future: Richer Multi-Agent Runtime

Goal: evolve the current thin AgentRegistry / TaskDispatcher into a richer
multi-agent runtime while keeping the skill architecture stable.

Partially addressed by v1.5: the model catalog routes each skill to an
expert model and the optional intent classifier already picks a skill for
plain-language messages. "Task routing policies" below now means extending
that, not starting from scratch.

Candidate agents:

- Telegram Gateway Agent
- Semantic Memory Agent
- Browser Scraper Agent
- Document Review Agent
- Cron Scheduler Agent
- Multimodal Analysis Agent

Expected work:

- Add explicit agent capability metadata.
- Add task routing policies.
- Add task history and failure reporting.
- Keep deterministic routes for commands such as `/mem`, `/rag`, `/doc`,
  `/search`, and `/cron`.

## Future: Personal Memory Deepening

Goal: make OpenClaw more useful as a long-running personal AI system.

**Done:** `/mem` metadata parsing (`due:YYYY-MM-DD`, `tag:<word>`), explicit
user review (`/mem list [done|archived] [tag:<word>]`), todo completion /
removal (`/mem done <id>`, `/mem rm <id>`), editing (`/mem edit <id> <new
text>`), snoozing (`/mem snooze <id> <3d|1w|YYYY-MM-DD>`, also reactivates
a done item), **proactive reminders** (`/mem digest [tag:<word>]`, wired to
cron via `/cron add daily 08:00 Memory digest :: /mem digest`), and
**memory aging/archival** (`/mem archive-stale`, a fully-automatic sweep
using the same staleness test the digest already computes; no unarchive by
design -- see "Archival" in `docs/TRACKER_MEMORY.md`). Tag *and* date
scoping extend to semantic search too: `/rag tag:<word>` and `/rag since:`/
`before:`. See `docs/TRACKER_MEMORY.md`.

`/mem digest` didn't need a new cron job type -- it's a normal dynamic job
whose prompt happens to be `/mem digest`. What it did need: a generic
"routine, nothing new" signal a skill can set
(`SkillResult.suppress_if_routine`) so the cron worker skips the Telegram
push (but still records the run) instead of pinging "nothing due" every
morning. Overdue / due-soon items repeat every run on purpose; only stale
undated items get a cooldown (`last_reminded_at` +
`OPENCLAW_MEM_DIGEST_REMIND_COOLDOWN_DAYS`) so they aren't nagged about daily.

Also done: the `/mem upcoming [days]` schedule report (v1.17), which unlike
the digest always reports, for a morning push.

Candidate work still open:

- Profile show/set flows.
- A real calendar source (e.g. a read-only calendar feed) for the schedule
  report; `/mem ... due:` only records dates.
- More precise `/rag` scope filters -- **done**: collection (`#<category>`,
  `#all`, v1.4), tag and date (`tag:`, `since:`/`before:`, v1.9), and
  source (`source:<text>`, matched against the `Sources:` names).
## Future: Runtime Lifecycle and Resource Control

Goal: keep OpenClaw useful even when the main model engine is stopped for other
projects, especially on shared GB10 / DGX GPU workstations and Arm CPU-only
hosts.

**First cut shipped post-v1.6** (`bin/openclawctl`, `OPENCLAW_BOOT_MODE`,
graceful Telegram degradation -- see `docs/RUNTIME_LIFECYCLE.md`). The
`openclawctl` design below described a fuller `status`/`status model` and
per-platform model actions; what shipped covers `core`/`model`/`full`
start/stop/restart/status/boot over `docker compose`. The rest of this
section is still the target.

Core design principle:

```text
OpenClaw core != model engine
```

Recommended split:

- Always-on core:
  - Telegram gateway
  - Gateway dashboard
  - Cron worker
  - Qdrant
  - Ollama embedding service
  - Browser scraper
  - Memory watcher
- Optional model engine:
  - GB10 / DGX: vLLM
  - Arm CPU-only: llama.cpp / ERNIE server
  - Arm gateway: trusted remote local vLLM endpoint

Expected behavior:

- OpenClaw core can stay online while vLLM or llama.cpp is stopped.
- Telegram should return a clear message when the model engine is paused.
- Memory, dashboard, cron schedule management, document intake, and scraper
  health checks should remain available when possible.
- Restarting the model engine should not require recreating all OpenClaw
  services.

Proposed boot modes:

```env
OPENCLAW_BOOT_MODE=core
OPENCLAW_BOOT_MODE=full
OPENCLAW_BOOT_MODE=manual
```

Mode semantics:

- `core`: start OpenClaw core services only.
- `full`: start core services and the local model engine.
- `manual`: do not auto-start OpenClaw services.

Recommended platform defaults:

- GB10 / DGX: default to `core` so GPU resources remain easy to reclaim for
  other projects.
- O6 / Arm CPU-only: default to `full` when used as a small always-on assistant,
  or `core` when CPU/RAM must be shared with other workloads.
- Arm gateway + remote local vLLM: default to `core`, because the gateway should
  not own the remote model server lifecycle.

Proposed CLI:

```bash
openclawctl status
openclawctl start core
openclawctl stop core
openclawctl restart core

openclawctl start model
openclawctl stop model
openclawctl restart model
openclawctl status model

openclawctl start full
openclawctl stop full
```

Platform-specific model actions:

- GB10 / DGX:
  - `openclawctl stop model` stops `openclaw-vllm`.
  - `openclawctl start model` starts `openclaw-vllm`.
- O6 / Arm CPU-only:
  - `openclawctl stop model` stops `openclaw-ernie-llama.service`.
  - `openclawctl start model` starts `openclaw-ernie-llama.service`.
- Arm gateway:
  - `openclawctl status model` checks the remote local vLLM endpoint.
  - start/stop may be disabled unless the gateway has explicit permission.

Validation:

- `openclawctl status` clearly distinguishes core status and model status.
- Stopping the model releases GPU/CPU memory without stopping Telegram.
- Telegram responds clearly when the model engine is paused.
- `/help`, `/cron`, document intake, and dashboard access still work with core
  services only.
- Starting the model again restores normal chat, `/rag`, and `/search`
  summarization without rebuilding the whole stack.
- Reboot behavior follows `OPENCLAW_BOOT_MODE`.

## Future: Platform Presets

Goal: make deployment easier across the Arm continuum.

Candidate profiles:

- DGX Spark / GB10 local GPU profile.
- Arm CPU-only profile.
- Arm host + remote local vLLM profile.
- O6 ERNIE + llama.cpp profile.
- Arm CPU-only + MNN Omni multimodal profile.
- Arm gateway + remote VLM profile.

Expected work:

- Add clearer `.env` examples.
- Add platform-specific smoke tests.
- Add resource and performance notes.
- Keep secrets and runtime state out of git.

## Concurrent Multi-Bot Personas — delivered

**Status:** running several bots at once works today -- one
`compose.persona.<name>.yaml` per bot, copied from the tracked
`compose.persona.example.yaml`, as described in `docs/PROFILES.md` "Run
Several Bots At Once". Per-bot container names, a per-bot Gateway with its
own host port, and per-bot collection names/prefixes are all in place.
`openclawctl --profile <bot|all>` runs one bot's containers at a time
(`docs/RUNTIME_LIFECYCLE.md`). The rest of this section is the original
design, kept for its reasoning.

Goal: run several independent OpenClaw "personas" at once on one host, each
with its own Telegram bot identity and its own memory scope, sharing one
model engine and one Qdrant server. Illustrative shape (generic, not any
specific deployment's real personas or user count):

```text
bot-a "topic A"  -- own RAG + cron -- allowlist can hold 1 or more chat IDs
bot-b "topic B"  -- own RAG + cron -- a different allowlist
...
```

**The isolation unit is the bot, not the individual user.** Multi-user
access within one bot is *not* a separate feature: `OPENCLAW_TELEGRAM_
ALLOWED_CHAT_IDS` is already a set, so a bot with 2+ chat IDs already
shares its single memory scope with all of them today, at zero new code.
There is no per-individual auto-partitioning of Qdrant collections or the
Category RAG registry -- access is a manually curated allowlist per bot,
matching how a single-profile deployment already works.

So this reduces to one thing: make `docs/PROFILES.md` support **running
multiple profiles concurrently**, which it explicitly does not yet do
("service names and host ports are still shared"). Each bot persona =
one profile = its own bot token, allowed chat IDs, cron chat IDs, `.env`,
`tracker`/`knowledge` collection names, Category RAG registry path,
conversation-memory store path, task-history path, and cron job store --
same list `docs/PROFILES.md` already defines "What A Profile Separates".

What's already free (needs no new code):
- Multi-user access per bot (`OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS`).
- Per-bot data isolation model (proven by the personal/demo profile split).
- Settings/QdrantClient/MemoryWriteSkill/RagRetrieveSkill already take
  collection names from a single static `Settings` loaded once per
  process -- running N bots is N processes, each with its own ordinary
  Settings, not one process juggling N configs.

What needed building (all done):
- Parameterize `container_name` per bot in compose (`openclaw-telegram`,
  `openclaw-memory-watcher`, `openclaw-cron` -- none of these bind a host
  port, since Telegram bots are outbound long-polling, so this is mostly
  a compose-authoring exercise: N service blocks or a generator, not new
  application code).
- `openclaw-vllm`, Qdrant, Whisper, and `openclaw-browser-scraper` are
  shared across all bots (no GPU/DB duplication needed) -- only
  bot-specific containers are replicated.
- `openclawctl --profile <name>` (or equivalent) so `start`/`stop`/`status`
  act on one persona's containers instead of everything.

Resolved -- **`openclaw-gateway` (the cron dashboard) must be one
container per bot, not shared.** It is the one piece that binds a host
port (`127.0.0.1:18789:18789`) and owns a single sqlite job store
(`OPENCLAW_GATEWAY_STATE_DB`). Traced `gateway_cron.py`:
`list_gateway_jobs()` calls `cron.list` with no bot/chat-id scope
parameter, and `load_dynamic_jobs()` does not filter the result -- an
`openclaw-cron` worker treats every job the RPC returns as its own. Two
bots sharing one dashboard would double-run every job and could deliver
bot B's job through bot A's Telegram token. So: own port + own
`gateway-data` dir per bot, no way around it with the current dashboard.

Found the hard way -- **Category RAG collections need their own
`OPENCLAW_CATEGORY_COLLECTION_PREFIX` per bot; nothing else isolates
them.** Unlike the tracker/knowledge collections (plain, already-isolated
env vars), a category collection's name comes from the category's
*display name*, not the bot. Two bots left on the default `oc_cat_`
prefix that each create a category with the same name (e.g. `trip`)
silently share one Qdrant collection -- no error, just diluted or wrong
`/rag #<category>` answers once enough unrelated content piles up. See
`docs/PROFILES.md` and `docs/CATEGORY_RAG.md`.

Privacy / upstream note: this repo is public. Bot persona names, topics,
and how many exist are private facts about a given deployment, not
project structure -- keep them out of anything committed. Concretely: any
compose file or `profiles/<name>/.env.example` that ends up tracked must
use generic placeholder names (`bot-a`, `personal`, `demo`), never a real
person, relationship, or topic; real per-bot compose files and `.env`s
stay local/gitignored, same as `profiles/*/.env` already is. See
`docs/PROFILES.md` and `docs/PUBLISH_CHECKLIST.md`.

Validation (extends `docs/PROFILES.md`'s existing checklist):
- Two+ bots run at the same time without container-name or port conflicts.
- A message to bot1 never appears in bot2's conversation memory, `/mem`,
  or `/rag`, and vice versa.
- A user on bot1's allowlist but not bot2's gets no response from bot2.
- Cron jobs on one bot never fire against another bot's chat IDs.
- Stopping one bot's containers does not affect another bot's, or the
  shared vLLM/Qdrant/Whisper/browser-scraper services.

Effort: medium. Mostly compose/ops and lifecycle tooling, not deep
application-code changes, since the per-bot isolation model already
exists and is already proven (`docs/PROFILES.md`) -- the actual gap is
running it N times concurrently instead of one at a time.
