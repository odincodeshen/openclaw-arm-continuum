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
| `ChatMemoryScenario.test_second_turn_carries_the_first_exchange` | `ChatAgent` replays the prior user+assistant turn on the next request |
| `ChatMemoryScenario.test_new_conversation_drops_history` | `/new` clears the replayed window |
| `ChatMemoryScenario.test_other_chat_is_isolated` | conversation memory does not leak across `chat_id` |

## Coverage matrix -- feature -> layer

Add a row when you add a user-facing feature; every feature should have at least
one layer that fails if it breaks.

| Feature | L0/L1 | L2 | L3 (planned) |
| --- | --- | --- | --- |
| Category RAG isolation + `#cat` / `#all` | `test_category_rag_retrieve` | `CategoryRagScenario` | real-index isolation |
| Two-step caption/pending upload flow | `test_category_gateway` | — | Telegram round trip |
| `Sources:` attribution | `test_category_rag_retrieve` | `CategoryRagScenario`, `KnowledgeAndMemoryScenario` | — |
| Document / knowledge ingest | `test_file_ingest` | `KnowledgeAndMemoryScenario` | — |
| `/mem` write + default `/rag` | `test_*` unit | `KnowledgeAndMemoryScenario` | — |
| Conversational memory + `/new` | `test_conversation_memory`, `test_telegram_gateway` | `ChatMemoryScenario` | multi-turn with a real model |
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
