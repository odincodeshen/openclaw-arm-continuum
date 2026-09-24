# Testing

Single entry point for how OpenClaw is verified. Layers run widest-and-cheapest
first.

| Layer | Where | Trigger | Needs | Time |
| --- | --- | --- | --- | --- |
| **L0** static + unit | `tests/`, `scripts/ci_validate.py` | every PR (`ci.yml`) | nothing | ~2 min |
| **L1** contract / golden | `tests/test_golden.py` | every PR (in the L0 `pytest` run) | nothing | seconds |
| **L2** integration scenarios | `tests/test_scenarios_integration.py` | every PR (`integration.yml`) | Qdrant | ~1 min |
| **L3** full functional e2e | (planned) `scripts/e2e_run.py` | tag / manual | GB10 + real models | ~15 min |

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
| English-learning bot v1.12: Monday BBC episode selection + chunk extraction, Tuesday shadowing stretch + annotation + word-level (WER) evaluation | `test_english_bot_monday`, `test_english_bot_tuesday` | `EnglishLearningScenario::test_weekly_content_round_trips_shared_with_no_owner_against_real_qdrant` | bot4 real run against the live BBC RSS feed (planned, see openclaw_eng_spec.md v1.12 milestone) |
| English-learning bot v1.13: Wednesday IELTS Part 2 fixed bank + dedup + STAR eval, Thursday 7-category small talk + safe/avoid prompt guardrail + Anchor&Bounce + text banter reply | `test_english_bot_wednesday`, `test_english_bot_thursday` | `EnglishLearningScenario::test_ielts_asked_question_dedup_persists_against_real_qdrant` | bot4 real run, human check that questions don't repeat and banter tone feels natural (planned, see openclaw_eng_spec.md v1.13 milestone) |
| English-learning bot v1.14: Friday chunk activation (semantic usage check + real-sentence write-back), Saturday cloze quiz (deterministic phrase-blanking, pending-answer, sticky `needs_review`) | `test_english_bot_friday`, `test_english_bot_saturday` | `EnglishLearningScenario::test_monday_friday_saturday_chain_persists_needs_review_against_real_qdrant` | bot4 real run across a full week (planned, see openclaw_eng_spec.md v1.14 milestone) |
| English-learning bot v1.15: Sunday Guardian RSS (3-tier freshness/dedup fallback + stdlib `<article>`/`<p>` extraction, real-page-verified during review after the consultant's series-slug URLs turned out to 404 -- switched to the working author-profile RSS format), full daily-completion-tracking wiring across all 7 days including a new `evaluate_monday_reply` (missing through v1.11-v1.14 -- without it Monday could never be marked completed) | `test_english_bot_sunday`, per-day `mark_task_completed` tests folded into each existing `test_english_bot_<day>` file | `EnglishLearningScenario::test_full_week_push_reply_sweep_against_real_qdrant` | Sunday's `<article>`/`<p>` extractor verified against a real live Guardian page during review; RSS/article fetch otherwise mocked in tests; bot4 real run across a full week still planned |
| English-learning bot v1.16 (final milestone): Wednesday Part 2+3 "same-topic deep dive" from `week_number > 17` onward (~month 5) -- live-generated Part 3 macro question, separate argument-structure (Claim/Concession/Conclusion) evaluation alongside the unchanged Part 2 STAR check, `mark_task_completed` still called exactly once for the combined task | `test_english_bot_wednesday::IsPart23ComboWeekTest`/`GeneratePart3QuestionTest`/`EvaluatePart3ArgumentTest`/`RunWednesdayTaskComboTest`/`EvaluateWednesdayReplyDualEvaluationTest` | — (no new Qdrant interaction pattern beyond v1.13's existing `tag:eng_ielts_topics` dedup test -- Part 3 questions are generated live and threaded through in memory, never persisted) | manually set `week_number` to 18+ and run a real combo week on bot4 to confirm the two-part flow and dual feedback feel natural (planned) |
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

## L3 -- planned

Same scenario definitions, real vLLM + Ollama + Qdrant on the GB10 self-hosted
runner, plus quality assertions a fake can't make (the VLM actually reads an
image; the router classifies correctly). Triggered by `workflow_dispatch` and
on `v*` tags; produces an `e2e-report.md` artifact that gates the GitHub
Release.
