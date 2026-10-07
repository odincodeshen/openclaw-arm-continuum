# Testing

Single entry point for how OpenClaw is verified. Layers run widest-and-cheapest
first.

| Layer | Where | Trigger | Needs | Time |
| --- | --- | --- | --- | --- |
| **L0** static + unit | `tests/`, `scripts/ci_validate.py` | every PR (`ci.yml`) | nothing | ~2 min |
| **L1** contract / golden | `tests/test_golden.py` | every PR (in the L0 `pytest` run) | nothing | seconds |
| **L2** integration scenarios | `tests/test_scenarios_integration.py` | every PR (`integration.yml`) | Qdrant | ~1 min |
| **L3** full functional e2e | `scripts/e2e_run.py` | manual (on the host) | GB10 + real models | ~1 min |
| **V** self-verification | `bin/verify` (`verify/`) | before a release, after a change | Docker + this machine's services; no personal data | ~2-40 min |

## Running locally

```bash
pip install ruff pytest pypdf

# L0 + L1
ruff check .
pytest
python scripts/ci_validate.py

# L2 -- needs Qdrant; the fake model server runs in-process
docker compose -f compose.test.yaml up -d
OPENCLAW_TEST_QDRANT_URL=http://127.0.0.1:6333 pytest tests/test_scenarios_integration.py
docker compose -f compose.test.yaml down -v
```

L2's Qdrant-backed cases **skip** (not fail) when no Qdrant is reachable, so a
plain `pytest` is always green. Each scenario creates and deletes its own
uniquely-named collections (`t_know_*`, `t_track_*`, `t_cat_*`), so pointing
`OPENCLAW_TEST_QDRANT_URL` at a shared Qdrant is safe.

## L1 -- what the golden tests pin

`tests/test_golden.py` fails when a user-visible contract drifts unintentionally:

- the Telegram command menu (`setMyCommands`) list and its consistency with `/help`
- `MODEL_PAUSED_MESSAGE` still names the fix and the commands that keep working
- fixed `category_slug()` output vectors (changing the derivation orphans every
  existing Qdrant collection and registry entry)
- user-facing strings stay English (the v1.6 i18n guarantee)

Intentional changes update the expected values in that file on purpose.

## L2 -- what the scenarios cover

`tests/fake_inference.py` is an in-process server implementing `GET /v1/models`,
`POST /v1/chat/completions` (echoes the last user message), and Ollama
`/api/embed` (deterministic word-based vectors, so retrieval is assertable).
Scenarios drive the real runtime components against real Qdrant.

| Scenario | Asserts |
| --- | --- |
| `CategoryRagScenario.test_isolation_retrieval_and_source_attribution` | doc indexed into the right per-category collection; `/rag #cat` retrieves it; `Sources:` names the original filename; other categories are not matched |
| `CategoryRagScenario.test_unknown_category_lists_existing_ones` | `/rag #<unknown>` explains and lists real categories |
| `KnowledgeAndMemoryScenario.test_knowledge_doc_is_indexed_and_retrievable_with_source` | inbox `knowledge/` file is chunked, embedded, upserted, retrieved with a `Sources:` line |
| `KnowledgeAndMemoryScenario.test_mem_write_then_default_rag_reads_it_back` | `/mem` write lands in the tracker collection and a later `/rag` retrieves it |
| `TrackerMemoryManagementScenario.test_write_list_done_rm_round_trip_against_real_qdrant` | `/mem list`/`done`/`rm` against real Qdrant: `scroll_by_filters`, `set_payload`, `delete_points` behave as the unit fakes assume |
| `TrackerMemoryManagementScenario.test_digest_reports_overdue_and_due_soon_against_real_qdrant` | `/mem digest` overdue/due-soon categorization against real Qdrant; repeats every call |
| `TrackerMemoryManagementScenario.test_digest_is_suppressed_when_nothing_is_due_or_stale` | an all-clear digest sets `suppress_if_routine` |
| `TrackerMemoryManagementScenario.test_delete_collection_against_real_qdrant` | `QdrantClient.delete_collection` actually removes a real collection (backs `/cat merge`'s cleanup step) |
| `ChatMemoryScenario.test_second_turn_carries_the_first_exchange` | `ChatAgent` replays the prior user+assistant turn on the next request |
| `ChatMemoryScenario.test_new_conversation_drops_history` | `/new` clears the replayed window |
| `ChatMemoryScenario.test_other_chat_is_isolated` | conversation memory does not leak across `chat_id` |
| `ChatMemoryRollingSummaryScenario.test_overflow_triggers_a_real_summarization_call_and_it_is_replayed` | a real `LlmClient.chat()` summarization call round-trips through the fake server and the result is replayed in the next turn |

## Coverage matrix -- feature -> layer

Add a row when you add a user-facing feature; every feature should have at least
one layer that fails if it breaks.

| Feature | L0/L1 | L2 | L3 (planned) |
| --- | --- | --- | --- |
| Category RAG isolation + `#cat` / `#all` | `test_category_rag_retrieve` | `CategoryRagScenario` | real-index isolation |
| Two-step caption/pending upload flow | `test_category_gateway` | — | Telegram round trip |
| Multi-photo album caption sharing (`media_group_id`) | `test_category_gateway::MediaGroupBufferTest` | — | Telegram round trip with a real album |
| Reply-to-message `#<category>` (no upload needed) | `test_category_gateway::ReplyCategoryMessageTest`, `CategoryIngestTest::test_ingest_text_*` | — | Telegram round trip against a real reply |
| Reply-ingest URL extraction (`URL:` line + `Sources:` citation) | `test_category_gateway::ReplyCategoryMessageTest::test_url_in_*`, `CategoryIngestTest::test_ingest_text_with_url_*`/`test_ingest_text_without_url_*` | — | — |
| Video summary relay (bare-link detection, Apps Script HTTP relay, failure reporting) | `test_video_relay::HandleVideoLinkMessageTest`, `RelayVideoSummaryTest` | — | live smoke test against a real Apps Script deployment |
| English-learning bot (bot4) v1.11: RSS parsing, owner-scoped Qdrant records, daily-task tracking, outbound `sendAudio`, audio clipping | `test_rss_parser`, `test_owned_records`, `test_daily_task_tracking`, `test_send_audio`, `test_pending_answer`, `test_http_client_multipart`, `test_audio_clip` | `EnglishLearningScenario::test_owner_filter_*`/`test_daily_task_completion_*`/`test_sweep_*` | real Docker build + real audio content verified during review (see commit a85b271) |
| English-learning bot v1.12: Monday BBC episode selection + chunk extraction, Tuesday shadowing stretch + annotation + word-level (WER) evaluation | `test_english_bot_monday`, `test_english_bot_tuesday` | `EnglishLearningScenario::test_weekly_content_round_trips_shared_with_no_owner_against_real_qdrant` | bot4 real run against the live BBC RSS feed -- done (forced day runs); live issues fixed: cross-persona whisper paths, highlight-clip filtering, voice replies too short to count |
| English-learning bot v1.13: Wednesday IELTS Part 2 fixed bank + dedup + STAR eval, Thursday 7-category small talk + safe/avoid prompt guardrail + Anchor&Bounce + text banter reply | `test_english_bot_wednesday`, `test_english_bot_thursday` | `EnglishLearningScenario::test_ielts_asked_question_dedup_persists_against_real_qdrant` | bot4 real run -- done (forced day runs): questions don't repeat, banter tone checked by hand, model answer / model reply added from live feedback |
| English-learning bot v1.14: Friday chunk activation (semantic usage check + real-sentence write-back), Saturday cloze quiz (deterministic phrase-blanking, pending-answer, sticky `needs_review`) | `test_english_bot_friday`, `test_english_bot_saturday` | `EnglishLearningScenario::test_monday_friday_saturday_chain_persists_needs_review_against_real_qdrant` | bot4 real run -- done (forced day runs): Friday sentence write-back feeds Saturday's cloze; position-based cloze judging fixed live; model ramble / filled-in answer key added |
| English-learning bot v1.15: Sunday Guardian RSS (3-tier freshness/dedup fallback + stdlib `<article>`/`<p>` extraction, real-page-verified during review after the consultant's series-slug URLs turned out to 404 -- switched to the working author-profile RSS format), full daily-completion-tracking wiring across all 7 days including a new `evaluate_monday_reply` (missing through v1.11-v1.14 -- without it Monday could never be marked completed) | `test_english_bot_sunday`, per-day `mark_task_completed` tests folded into each existing `test_english_bot_<day>` file | `EnglishLearningScenario::test_full_week_push_reply_sweep_against_real_qdrant` | Sunday's `<article>`/`<p>` extractor verified against a real live Guardian page during review; RSS/article fetch otherwise mocked in tests; bot4 forced Sunday run done; a real calendar week on bot4 still planned |
| English-learning bot v1.16 (final milestone): Wednesday Part 2+3 "same-topic deep dive" from `week_number > 17` onward (~month 5) -- live-generated Part 3 macro question, separate argument-structure (Claim/Concession/Conclusion) evaluation alongside the unchanged Part 2 STAR check, `mark_task_completed` still called exactly once for the combined task | `test_english_bot_wednesday::IsPart23ComboWeekTest`/`GeneratePart3QuestionTest`/`EvaluatePart3ArgumentTest`/`RunWednesdayTaskComboTest`/`EvaluateWednesdayReplyDualEvaluationTest` | — (no new Qdrant interaction pattern beyond v1.13's existing `tag:eng_ielts_topics` dedup test -- Part 3 questions are generated live and threaded through in memory, never persisted) | manually set `week_number` to 18+ and run a real combo week on bot4 to confirm the two-part flow and dual feedback feel natural (still planned) |
| English-learning bot wiring stage: day-of-week push/sweep scheduler running inside the gateway process (`OPENCLAW_ENGLISH_BOT_ENABLED`, bot4-only), `PENDING_ANSWER` used as the universal reply-routing mechanism for all 7 days (not just Saturday), voice/text replies both accepted | `test_english_bot_scheduler` (day-of-week dispatch, push/sweep due-window + same-day dedup, `run_todays_push`/`dispatch_pending_reply` per-day dispatch, all mocked -- no real Qdrant/LLM/Telegram) | — (pure scheduling/dispatch glue, no new Qdrant interaction beyond what each day's already-covered `run_<day>_task`/`evaluate_<day>_reply` already does) | **this is gateway-integration code with no meaningful unit-test coverage for the real thing** -- background-thread timing against a real clock, the actual `handle_message()` routing precedence, and a real voice-message round-trip (download → transcribe → evaluate → reply) can only be verified by an actual live run on bot4; that live smoke test is the real verification for this stage, not optional follow-up. Done for all seven days on bot4 using `OPENCLAW_ENGLISH_BOT_FORCE_DAY_CODE` + a near-term `OPENCLAW_ENGLISH_BOT_PUSH_TIME` (repeatable practice + `/Done`, under-10s voice rejection); a real calendar week still planned |
| English-bot message cards: one shared task-card / feedback-card layout for all 7 days (short Chinese title, English labels, Telegram HTML with expandable Example / Your answer), HTML escaping, blank-line-aware splitting, plain-text fallback when Telegram rejects the HTML, `/`-commands bypass the open daily task | `test_message_cards` (incl. `SameStructureEveryDayTest` across all 7 builders), `tests/card_checks.py` assertions in every `test_english_bot_<day>` | — | bot4 live push of each day to eyeball the layout on phone and desktop |
| Word lookup `/w` + word list `/vocab`: offline ECDICT SQLite (built once, Simplified→Traditional via OpenCC), LLM for in-context sense + example, labelled AI fallback for words not in the dictionary, owner-scoped Qdrant word list with lookup-count dedup | `test_dictionary` (build + lookup against a tiny CSV fixture), `test_vocabulary` (lookup, save/bump, list/remove, rendering, gateway routing), `test_vocab_review` (Leitner schedule, due-word selection/order/cap, cloze-from-own-sentence vs meaning recall, grading write-back, typed-only review routing + 60-min expiry, per-owner Word review block on daily cards) | — | built from the real ecdict.csv (770,611 entries) and spot-checked; live `/w` round trip on bot4 |
| English bot weekly recap + unlearned chunks into `/vocab review` (Sunday), 112-card IELTS bank | `test_english_weekly` (outcomes, day statuses incl. duplicate records, promotion, rendering, per-owner isolation of failures), `test_english_bot_wednesday::QuestionBankTest` | — | first real recap on bot4, Sunday 2026-10-04 |
| `/rag source:<text>` filter (default, `#<category>`, `#all`; over-fetch then filter) | `test_rag_source_filter` | — | — |
| `openclawctl --profile <bot\|all>` / `profiles` (per-bot services, `--no-deps`, Gateway state dir, running-only `all`) | `test_openclawctl::OpenclawctlProfileTest` (dry-run against a temp repo root) | — | used live on the four running bots |
| Plain `/rag` also searches every category (3 hits each; skipped under `tag:`; `OPENCLAW_RAG_INCLUDE_CATEGORIES`) | `test_rag_source_filter::PlainRagIncludesCategoriesTest` | — | live plain `/rag` on bot2 answered from a category |
| Video relay outcome logging (`relay ok` / `failed` / `unreachable`, with duration) | `test_video_relay` | — | — |
| Off-peak preparation: English bot outbox (prepare 04:00, deliver at push time, one attempt, live fallback) and `/cron` prepared jobs (`OPENCLAW_CRON_PREPARE_WINDOW`/`_PROMPTS`) | `test_offpeak_prepare`, `test_cron_worker::OffPeakPrepareTest` | — | first overnight run on bot2/3/4 |
| Open English tasks and `/vocab review`s survive a restart (`OPENCLAW_PENDING_STATE_PATH`); one daily-task record per user per day; Monday picks the newest unused full episode | `test_pending_state`, `test_daily_task_tracking`, `test_english_bot_monday::PickUnusedEpisodeTest` | — | — |
| English bot chunk examples: English sentences containing the phrase (with one repair pass), cloze matching of irregular forms / `-ing` / `someone`/`one's` placeholders / bracketed optional parts, Monday audio clip + episode link, Friday examples | `test_english_bot_monday::EnsureEnglishExamplesTest`, `test_english_bot_saturday::GenerateClozeTest`, `test_english_bot_friday::BuildFridayMessageTest` | — | week-2 examples regenerated live; Saturday preview checked |
| Monday listening check: deterministic 3-gap dictation (spread content words, never chunk words), one-call gist + dictation extraction + chunk judging, typo-tolerant grading, transcript reveal, weekly-recap Listening line, older weeks keep the chunk-only path | `test_monday_listening` | — | dictation chosen from the real week-2 transcript; first live Monday 2026-10-05 |
| Operator alerts (cron job failure, lasting Gateway outage, preparation failure, English scheduler error, watcher scan/index failure; cooldown + one "recovered") | `test_alerts` | — | test alert delivered on bot4 |
| Weekly `/rag digest week` (7 days up to yesterday, weekly jobs prepared off-peak on their day), Monday clip as a voice message (Opus), `/vocab export` (Anki TSV), host container watchdog (crash restart / restarting / unhealthy / reboot; manual restart and stopped containers stay quiet) | `test_daily_reports`, `test_cron_worker`, `test_send_audio::SendVoiceAndDocumentTest`, `test_vocabulary::AnkiExportTest`, `test_watchdog` | — | Opus clip encoded in `openclaw-whisper`; bot2/bot3 jobs switched to Sunday; watchdog run live against the host's containers |
| Check-ins from TOML: spec validation (ids, references, times, days, bad files skipped), presets parse, deadline and report periods, a full work-log week through the gateway (questions, closing card without a model line, schedule button, next-day follow-up and morning card, week-to-date report made at send time, sent once), weekends skipped, `/worklog` history and help, command menu, owner checks, the night ritual unchanged through the generic runtime; `/mem list done since:7d`; `persona=False` model calls | `test_checkins`, `test_night_ritual`, `test_memory_write::MemListSinceTest`, `test_llm_client` | — | night ritual live on DGX bot2 via the runtime; worklog live on the O6 |
| O6 / CPU-only readiness: prompts trimmed to the model's context window (middle of the longest part, then max_tokens), `context_tokens` per catalog endpoint, photos not queued without a vision model, `openclawctl` with no model services, `compose.o6.yaml` + O6 persona example, e2e `--container` | `test_context_fit`, `test_image_text::NoVisionModelTest`, `test_openclawctl` | — | `scripts/o6_validate.py` 7/7 on the GB10 and on the O6 (ERNIE + Qwen2-VL); e2e 10/10 on the O6 |
| O6 performance and check-in templates: `/rag` budget (passages focused on the question, best-first within `OPENCLAW_RAG_CONTEXT_TOKENS`, named files first, at least one kept, duplicates once, Chinese matching, Sources only for what was read); every template parses with Chinese titles and English prompts; follow-up look-back reaches last week for a weekly check-in; `/checkins` lists and reports skipped files, `/checkins reload` adds and removes without a restart, built-in commands can't be taken | `test_rag_budget`, `test_checkins::PresetLibraryTest`, `::FollowUpLookbackTest`, `::CheckinsCommandTest` | — | `scripts/perf_probe.py` on the O6 (`/rag` ~100 s -> ~26 s); `scripts/rag_budget_eval.py` on DGX bot2's documents (1500/400 kept as many answers as no limit) |
| `/rag` retrieval: relevance margin (vector hits only, named files kept); keyword vectors (terms incl. numbers and Chinese pairs, BM25 tf, stable hashes); Qdrant collections created with `kw`, passages written with it only where the collection has it, keyword query with filters; keywords-first selection and its order through the budget; `_search` merges keyword and vector hits without duplicates; chat IDs masked in logs except `rejected`; `/checkins add` / `remove` | `test_keyword_search`, `test_rag_budget::DropWeakHitsTest`, `test_logsafe`, `test_checkins::CheckinsCommandTest` | — | `scripts/qdrant_add_keywords.py` on bot2 (10 collections, 553 points); `scripts/rag_retrieval_eval.py`: source passage sent 27% -> 87% (Chinese questions 13% -> 70%); e2e 10/10 |
| Embedding model switch: re-embedding keeps ids, payloads and keyword vectors, plain and named vectors, the vector size check | `test_qdrant_reembed` | — | `scripts/qdrant_reembed.py` on a throwaway 30-point collection on the GB10 (768 -> 1024, keyword vectors and payload index kept, an English question found a Chinese passage); candidates compared with `bin/verify gold/standard --env` on both machines (`docs/EMBEDDINGS.md`) |
| Image text: separate verbatim transcription (no persona/reply-language instruction, continuation when cut off, NO_TEXT), description in the image's language, uncategorised and cancelled photos into the knowledge base, scanned PDF pages rendered and read with a per-page cache and page limit | `test_image_text`, `test_category_gateway::CategoryIngestTest` | — | UK ticket screenshot complete (was cut off), Traditional receipt and Simplified rail ticket exact with script kept, scanned lease PDF read exactly via the watcher (~10 s/page) |
| Nightly backup (Qdrant snapshots + workspace archive, rebuildable files skipped, own folders pruned), daily cleanup inside each bot, weekly e2e via `openclaw_maintenance.py`, summary lines; Anki `.apkg` with UK/US MP3; `/say`; `/vocab quiz` + Sunday offer; 🔊 on `/vocab` and Monday/Friday chunks; upload category suggestion + duplicate skip; `/rag` Show sources / Follow-up / Save answer; spoken night answers tidied; yearly night report | `test_maintenance`, `test_housekeeping`, `test_watchdog`, `test_tts`, `test_vocab_review::WordQuizTest`, `test_upload_hints`, `test_rag_buttons`, `test_night_ritual` | — | backup run live (31 collections, 9 s); e2e 10/10 via the script; .apkg imported into the real Anki library (notes, media, re-import updates) |
| Pronunciation: `openclaw-tts` request checks and per-voice cache, 🔊 UK/US word + sentence buttons on lookups and self-check answers, voice message sent and temp file removed, expired button / service down | `test_tts` | — | Kokoro samples compared with Piper on bot4; UK `bf_emma`, US `af_heart` |
| Buttons: `/vocab review` self-check (Show answer → Remembered / Forgot, one box move per tap), `/vocab` Remove words…, Monday gist A/B/C (first tap counts, model placeholder never overwrites it), `/menu` (only listed commands run); night ritual trend (streak, vs previous period), Add to tomorrow's schedule (into another bot's tracker memory), `/night move`; `/cat merge` moves PDFs and drops duplicates; an upload waiting for its category survives a restart; watchdog weekly summary | `test_vocab_review`, `test_monday_listening::GistButtonsTest`, `::GatewayGistTest`, `test_night_ritual`, `test_category_gateway`, `test_pending_state`, `test_watchdog` | — | `scripts/e2e_run.py` 10/10 on bot2 |
| Night ritual: four single questions (text/voice), yesterday's first-thing check by button, closing card with a local-model line, 23:00/23:30 reminders only while open, midnight partial/missed, Saturday skipped, `/night start` early, morning first-thing card, weekly/monthly reports prepared off-peak with a Markdown copy, open night survives a restart | `test_night_ritual` | — | enabled on bot2 2026-09-29; first live night that evening |
| Inline-button category picker (existing categories, + New category with force-reply, Cancel, buttons closed on file/cancel/expiry, bare `cancel`), `/cat delete` with a confirm card, one-word category names (legacy multi-word names still resolve) | `test_category_gateway::CategoryPickerTest`, `::CategoryDeleteTest`, `::CategoryRenameCommandTest` | — | picker, New category and filing tried on bot2 |
| `/cat rename` / `/cat merge` | `test_categories`, `test_category_gateway`, `test_qdrant_client` | `TrackerMemoryManagementScenario::test_delete_collection_against_real_qdrant` | full merge round trip with real ingest |
| `Sources:` attribution | `test_category_rag_retrieve` | `CategoryRagScenario`, `KnowledgeAndMemoryScenario` | — |
| Document / knowledge ingest | `test_file_ingest` | `KnowledgeAndMemoryScenario` | — |
| Header-aware chunker (keeps `##`/`###` sections whole; falls back to char-slicing only for oversized sections) | `test_file_ingest::ChunkTextTest` | — | — |
| `/mem` write + default `/rag` | `test_*` unit | `KnowledgeAndMemoryScenario` | — |
| `/mem list` / `done` / `rm` + `due:`/`tag:` metadata | `test_memory_write`, `test_qdrant_client` | `TrackerMemoryManagementScenario` | — |
| `/mem digest` + cron `suppress_if_routine` skip | `test_memory_write::MemoryDigestTest`, `test_cron_worker::RunDynamicJobTest`/`WriteGatewayRunbackTest` | `TrackerMemoryManagementScenario` | live daily push |
| `/mem snooze` (relative/absolute, reactivates done) | `test_memory_write::MemorySnoozeTest` | `TrackerMemoryManagementScenario` (shares `set_payload` coverage) | — |
| `/mem edit` (re-embed, preserve status/due/tags) | `test_memory_write::MemoryEditTest` | `KnowledgeAndMemoryScenario` (shares `upsert_text(point_id=...)` coverage) | — |
| `/mem list`/`digest` tag scope filter | `test_memory_write` (list + digest tag tests) | `TrackerMemoryManagementScenario::test_list_and_digest_tag_filter_against_real_qdrant` | — |
| `/rag tag:<word>` scope filter (tracker only, skips knowledge) | `test_category_rag_retrieve` (`SplitRagFilterPrefixTest`, tag-filter tests), `test_qdrant_client` (`search(filters=...)`) | `KnowledgeAndMemoryScenario::test_rag_tag_prefix_scopes_to_tracker_items_with_that_tag_against_real_qdrant` | — |
| `/rag since:`/`before:` date range (tracker + knowledge, combines with `tag:`) | `test_category_rag_retrieve` (`SplitRagFilterPrefixTest`, date-range tests), `test_qdrant_client` (`search(since=..., before=...)`) | `KnowledgeAndMemoryScenario::test_rag_date_range_filters_against_real_qdrant` | — |
| `/mem archive-stale` (fully-automatic sweep) + `/mem list archived` | `test_memory_write::MemoryArchiveStaleTest` | `TrackerMemoryManagementScenario::test_archive_stale_sweep_against_real_qdrant` | live weekly cron sweep |
| Conversational memory + `/new` | `test_conversation_memory`, `test_telegram_gateway` | `ChatMemoryScenario` | multi-turn with a real model |
| Rolling summary + `/keep` | `test_conversation_memory::RollingSummaryTest`/`PinnedFactsTest` | `ChatMemoryRollingSummaryScenario` | summary quality with a real model |
| `/history` read-only preview | `test_conversation_memory::HistoryPreviewTest`, `test_telegram_gateway::FormatHistoryPreviewTest`/`HistoryCommandTest` | — | — |
| Vision / image analysis | `test_vision_client`, `test_category_gateway` | — | `scripts/vision_smoke.py` on a real VLM |
| Intent router | `test_intent_router` | — | classification accuracy |
| Expert-model routing / `/review` | `test_engineering_review*`, `test_skill_router` | — | `docs/DGX_V13_VALIDATION.md` |
| Web search | `test_routing_integration` (mocked) | — | live scrape |
| Cron schedules + push | `test_cron_*`, `test_gateway_cron` | — | real due-window push |
| Model-engine-down degradation | `test_telegram_gateway::ModelPausedMessageTest` | — | `openclawctl stop model` live |
| `bin/openclawctl` | `test_openclawctl` | — | real `docker compose` on a host |
| Command menu / `/help` consistency | `test_golden` | — | — |

## L3 -- `scripts/e2e_run.py`

Runs inside a bot's Telegram container against that bot's real vLLM,
embedding model, Qdrant, Whisper, Gateway and Telegram token, using only
throwaway collections and a temporary inbox (deleted afterwards). It sends
no Telegram messages -- only a read-only `getMe`.

```bash
docker exec -i openclaw-telegram-<bot> python3 - < scripts/e2e_run.py
docker exec -i openclaw-telegram-<bot> python3 - --out /workspace/e2e-report.md < scripts/e2e_run.py
```

Checks: model reply, embeddings, category RAG (ingest, isolation,
`Sources:`), knowledge RAG, `/mem` write + `upcoming` + `list`, Whisper and
Gateway reachability, Telegram `getMe`, the night ritual's closing line and
weekly summary (English, not the fallback) and Monday's gist options (three,
with an answer). Prints a Markdown table; exit code 1 if anything failed.
First run 2026-09-29 on bot2: 10/10 passed.

Still planned: running it from a GB10 self-hosted runner on `v*` tags, and
the VLM image and intent-router quality checks.

## Performance -- `scripts/perf_probe.py` and `scripts/rag_budget_eval.py`

Both run inside a bot's Telegram container, like the e2e.

- **`perf_probe.py`** times a bot's typical requests on throwaway
  collections: short chat, a second chat turn, `/rag`, `/rag #category` and
  a check-in closing line. For each it reports:
  - prompt size;
  - tokens reused from llama.cpp's prompt cache;
  - time reading the prompt and writing the answer;
  - whether the `/rag` answers still contain the fact asked about.

  `--rag-context-tokens` / `--rag-passage-tokens` try a `/rag` budget for one
  run only.
- **`rag_budget_eval.py`** only reads. It checks a `/rag` budget on the bot's
  own documents:
  1. it samples passages and has the model write a question and fact for each;
  2. it compares answers with and without the budget.

  Only counts are printed (questions with `--show`).
- **`rag_retrieval_eval.py`** checks retrieval only, so it is quick. For
  each model-written question it reports:
  - whether the source passage is in what `/rag` sends the model, with
    keyword search off and on;
  - its rank in its collection;
  - what relevance margins would keep.

  `--hybrid` adds offline keyword/fusion rankings,
  `--question-language "Traditional Chinese"` tests cross-language
  questions, and `--reembed-model` / `--reembed-prefix` try another
  embedding model on throwaway copies.

```bash
docker exec -i openclaw-telegram-<bot> python3 - --rounds 2 < scripts/perf_probe.py
docker exec -i openclaw-telegram-<bot> python3 - --budgets 1200/300,1500/400 < scripts/rag_budget_eval.py
```

## Self-verification -- `bin/verify`

`bin/verify` runs the unit tests and a platform check, then one scenario
per feature, each in its own throwaway sandbox. `bin/verify full` adds the
gold set: 48 fixed questions and 7 images, scored by code against minimums
and this machine's last accepted run. The platform check covers
model, context, JSON output, a long prompt, image text and services, and
compares speeds with `verify/platforms/<machine>.toml`. Each scenario gets:

- the real gateway code with a fake Telegram;
- this machine's real model and services;
- made-up fixtures, and no profile, chat ID or token.

It works the same on a GB10, an Orion O6 or any other host, and it is how a
change is checked before it ships. A new feature adds a scenario. See
`verify/README.md` for the scenario format.

Its first run (October 2026) passed all 12 scenarios on both the GB10 and
the O6; with four more for coverage, all 16 pass on both. With the fill-in bug of 2026-10-05 put back,
`checkin_skip_fillin` failed at the step where the answer went to chat
instead.

The gold set's first runs:

| | Retrieval | Answers | Image text |
| --- | --- | --- | --- |
| GB10 | 92% | 92% | 100% |
| O6 | 89% | 86% | 99% |

A second GB10 run matched its baseline exactly. Cross-language questions
are the weak spot: 70% on the GB10 and 60% on the O6.

### Every week, coverage, other machines

- **Weekly:** `scripts/openclaw_maintenance.py verify` runs `bin/verify full`
  every Monday at 01:00. That is after the night ritual, before the 03:15
  backup and the 04:00 off-peak preparation, so it never competes with the
  bots for the model.
  - It keeps `.cache/openclaw-verify-status.json` for the watchdog's weekly
    summary and alerts on Telegram when it fails.
  - On the O6 a systemd user timer runs it, on the copy in
    `~/openclaw-verify` (`OPENCLAW_VERIFY_DIR`).
- **Coverage:** `bin/verify coverage` lists bot commands and check-in
  templates that no scenario uses; `standard` and `full` print the same
  line. Commands a sandbox can't run are listed with the reason:
  - `/doc` and `/search` need the internet;
  - `/review` is a long multi-model run;
  - `/w`, `/vocab` and `/say` need the dictionary database.
- **Other machines:** `bin/verify <mode> --remote HOST:DIR` copies the
  checkout's files to another machine and runs `bin/verify` there.

## Releasing -- `bin/release`

A release is cut only after the checks pass, in this order. It stops at the
first failure:

1. **git:** on `main`, nothing uncommitted, in step with `origin`, and a new
   tag later than the last one.
2. **Unit tests and `scripts/ci_validate.py`.**
3. **`bin/verify standard`** on this machine must pass: the unit tests,
   the platform check and every feature scenario, in sandboxes with no
   personal data. Each machine in `RELEASE_VERIFY_REMOTE` (`HOST:DIR`) gets
   a copy of the files and runs the same check there through
   `bin/verify --remote`; a failure there is only a warning, since a
   CPU-only model's answers vary more. The older live e2e
   (`scripts/e2e_run.py`) can run too, in the bots named in `RELEASE_E2E`
   (must pass) or `RELEASE_E2E_OPTIONAL` (warn), retried once.
4. **Privacy scan** of everything added since the previous tag. It looks
   for:
   - chat IDs and tokens taken from `profiles/*/.env`;
   - home paths, e-mail addresses and private IPs;
   - tracked `.env` or bot compose files.

   Findings name the file and the kind of leak, never the value. A line
   marked `privacy-scan: fake data` may hold made-up e-mail addresses, IPs
   or home paths, such as the scan's own tests. Real chat IDs and tokens are
   never let through.
5. **Publish:**
   - the version in both READMEs and `docs/FUTURE_TODO.md`;
   - a `release: <tag> - <title>` commit, then push;
   - the GitHub release, whose notes end with a "Release checks" list of the
     results.

```bash
bin/release v1.28 --title "Check-in skip days" --notes notes.md --dry-run   # checks only
bin/release v1.28 --title "Check-in skip days" --notes notes.md
```

`.release.local` is gitignored, so bot names and hosts stay out of the
repo. It holds `KEY=VALUE` lines:

```
RELEASE_VERIFY=standard                       # bin/verify mode here ("none" to skip)
RELEASE_VERIFY_REMOTE=<host>:openclaw-verify  # optional, space-separated
RELEASE_E2E=openclaw-telegram-<bot>           # optional live e2e
RELEASE_E2E_OPTIONAL=ssh:<host>:openclaw-telegram-<bot>
RELEASE_PYTHON=.cache/test-venv/bin/python   # a Python with pytest (python3 -m venv .cache/test-venv; pip install pytest pypdf ruff)
```

