"""L2 integration scenarios -- the real ingest -> Qdrant -> retrieve -> format
pipeline, with fakes only at the model boundary.

The Qdrant-backed scenarios need a running Qdrant:

    docker compose -f compose.test.yaml up -d qdrant
    OPENCLAW_TEST_QDRANT_URL=http://127.0.0.1:6333 pytest tests/test_scenarios_integration.py

They skip (not fail) when no Qdrant is reachable. The chat scenarios need only
the in-process fake server and always run.
"""

import json
import os
import tempfile
import unittest
import urllib.request
import uuid
from datetime import date, timedelta
from pathlib import Path

from openclaw_runtime import categories
from openclaw_runtime.agents.base import Task
from openclaw_runtime.agents.skill_agents import ChatAgent
from openclaw_runtime.categories import category_slug
from openclaw_runtime.conversation_memory import ConversationMemory
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.file_ingest import InboxIngestor
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.skills.memory import MemoryWriteSkill, RagRetrieveSkill

from tests.fake_inference import FakeInferenceServer
from tests.support import build_settings

QDRANT_URL = os.environ.get("OPENCLAW_TEST_QDRANT_URL", "http://127.0.0.1:6333")
DIM = 64


def _qdrant_up(url: str) -> bool:
    try:
        urllib.request.urlopen(url.rstrip("/") + "/collections", timeout=2)
        return True
    except Exception:
        return False


@unittest.skipUnless(_qdrant_up(QDRANT_URL), f"no Qdrant at {QDRANT_URL}")
class QdrantScenarioBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeInferenceServer(dim=DIM)
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.inbox = root / "inbox"
        self.inbox.mkdir()

        run = uuid.uuid4().hex[:8]
        self.knowledge = f"t_know_{run}"
        self.tracker = f"t_track_{run}"
        self.prefix = f"t_cat_{run}_"

        self.settings = build_settings(
            ollama_base_url=self.fake.ollama_base_url,
            vllm_base_url=self.fake.openai_base_url,
            qdrant_base_url=QDRANT_URL,
            embedding_model="fake",
            embedding_vector_size=DIM,
            request_timeout=10,
            web_timeout=10,
            retrieval_limit=5,
            inbox_path=self.inbox,
            watcher_state_path=root / "watcher_state.json",
            ingest_chunk_chars=400,
            ingest_chunk_overlap=40,
            knowledge_collection=self.knowledge,
            tracker_collection=self.tracker,
            category_collection_prefix=self.prefix,
            category_registry_path=self.inbox / ".openclaw" / "categories.json",
        )
        self.addCleanup(self._drop_collections)

        self.embeddings = EmbeddingClient(self.settings)
        self.qdrant = QdrantClient(self.settings)
        self.llm = LlmClient(self.settings)
        self.ingestor = InboxIngestor(self.settings, self.embeddings, self.qdrant)
        self.rag = RagRetrieveSkill(self.settings, {}, self.embeddings, self.qdrant, self.llm)

        # The memory-watcher / gateway ensure these at startup; category
        # collections are auto-ensured by the ingestor.
        self.qdrant.ensure_collections()

    def _drop_collections(self) -> None:
        try:
            raw = urllib.request.urlopen(QDRANT_URL.rstrip("/") + "/collections", timeout=3).read()
            names = [c["name"] for c in json.loads(raw)["result"]["collections"]]
        except Exception:
            return
        for name in names:
            if name in (self.knowledge, self.tracker) or name.startswith(self.prefix):
                try:
                    req = urllib.request.Request(
                        f"{QDRANT_URL.rstrip('/')}/collections/{name}", method="DELETE"
                    )
                    urllib.request.urlopen(req, timeout=5)
                except Exception:
                    pass

    # -- helpers ----------------------------------------------------------
    def put_category_doc(self, category: str, filename: str, text: str) -> None:
        slug = category_slug(category)
        directory = self.inbox / "categories" / slug
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(text, encoding="utf-8")
        (directory / (filename + ".meta.json")).write_text(
            json.dumps(
                {"category": category, "category_slug": slug, "original_file_name": filename}
            ),
            encoding="utf-8",
        )
        categories.upsert_registry_entry(self.settings, category)

    def put_knowledge_doc(self, filename: str, text: str) -> None:
        directory = self.inbox / "knowledge"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(text, encoding="utf-8")

    def rag_query(self, query: str) -> str:
        return self.rag.run(f"/rag {query}").answer


class CategoryRagScenario(QdrantScenarioBase):
    def test_isolation_retrieval_and_source_attribution(self) -> None:
        self.put_category_doc(
            "batteries",
            "cells.md",
            "Lithium iron phosphate battery cells have a long cycle life and stable "
            "thermal behaviour under load.",
        )
        self.put_category_doc(
            "taxes",
            "return.md",
            "The self assessment tax return filing deadline in the United Kingdom is "
            "the thirty first of January.",
        )
        results = self.ingestor.scan_once()
        self.assertTrue(any(r.chunks > 0 for r in results), results)

        answer = self.rag_query("#batteries what cell chemistry is described?")
        self.assertIn("phosphate", answer.lower())          # right doc retrieved
        self.assertIn("Sources: cells.md", answer)          # attribution
        self.assertNotIn("tax return", answer.lower())      # isolation holds

        answer2 = self.rag_query("#taxes when is the filing deadline?")
        self.assertIn("january", answer2.lower())
        self.assertIn("Sources: return.md", answer2)

    def test_unknown_category_lists_existing_ones(self) -> None:
        self.put_category_doc("batteries", "cells.md", "battery cell chemistry notes")
        self.ingestor.scan_once()
        answer = self.rag_query("#does-not-exist anything?")
        self.assertIn('No category named "does-not-exist"', answer)
        self.assertIn("batteries", answer)


class KnowledgeAndMemoryScenario(QdrantScenarioBase):
    def test_knowledge_doc_is_indexed_and_retrievable_with_source(self) -> None:
        self.put_knowledge_doc(
            "neoverse.txt",
            "The Arm Neoverse V2 platform targets high performance per watt for "
            "cloud and HPC workloads with wide vector units.",
        )
        self.ingestor.scan_once()
        answer = self.rag.run("/rag what does the Neoverse V2 platform target?").answer
        self.assertIn("performance per watt", answer.lower())
        self.assertIn("Sources: neoverse.txt", answer)

    def test_mem_write_then_default_rag_reads_it_back(self) -> None:
        writer = MemoryWriteSkill(self.settings, {}, self.embeddings, self.qdrant)
        writer.run("/mem OpenClaw deploy note: the gateway container mounts app read only")
        answer = self.rag.run("/rag what does the gateway container mount?").answer
        self.assertIn("read only", answer.lower())


class TrackerMemoryManagementScenario(QdrantScenarioBase):
    """/mem list|done|rm against a real Qdrant -- proves the scroll filter,
    set_payload, and delete endpoints behave the way the fakes assume."""

    def setUp(self) -> None:
        super().setUp()
        self.writer = MemoryWriteSkill(self.settings, {}, self.embeddings, self.qdrant)

    def test_write_list_done_rm_round_trip_against_real_qdrant(self) -> None:
        self.writer.run("/mem renew passport due:2026-12-01 tag:admin")
        self.writer.run("/mem water the plants")

        active = self.writer.run("/mem list").answer
        self.assertIn("Active memory (2):", active)
        self.assertIn("renew passport", active)
        self.assertIn("due 2026-12-01", active)
        self.assertIn("water the plants", active)

        short_id = self._short_id_for("water the plants")
        done = self.writer.run(f"/mem done {short_id}").answer
        self.assertIn(f"Marked #{short_id} as done", done)

        active_after = self.writer.run("/mem list").answer
        self.assertNotIn("water the plants", active_after)
        self.assertIn("Active memory (1):", active_after)

        done_list = self.writer.run("/mem list done").answer
        self.assertIn("water the plants", done_list)

        passport_id = self._short_id_for("renew passport")
        removed = self.writer.run(f"/mem rm {passport_id}").answer
        self.assertIn(f"Deleted #{passport_id}", removed)
        self.assertNotIn("renew passport", self.writer.run("/mem list").answer)

    def _short_id_for(self, needle: str) -> str:
        hits = self.qdrant.scroll_by_filters(self.tracker, {"kind": "tracker_memory"}, limit=50)
        for hit in hits:
            if needle in (hit.get("payload") or {}).get("text", ""):
                return hit["payload"]["short_id"]
        raise AssertionError(f"no tracker memory point contains {needle!r}")

    def test_digest_reports_overdue_and_due_soon_against_real_qdrant(self) -> None:
        today = date.today()
        overdue_due = (today - timedelta(days=1)).isoformat()
        soon_due = (today + timedelta(days=2)).isoformat()
        far_due = (today + timedelta(days=90)).isoformat()

        self.writer.run(f"/mem pay overdue invoice due:{overdue_due}")
        self.writer.run(f"/mem book dentist due:{soon_due}")
        self.writer.run(f"/mem plan next year due:{far_due}")

        digest = self.writer.run("/mem digest")
        self.assertFalse(digest.suppress_if_routine)
        self.assertIn("Overdue (1):", digest.answer)
        self.assertIn("pay overdue invoice", digest.answer)
        self.assertIn("Due in the next 7 days (1):", digest.answer)
        self.assertIn("book dentist", digest.answer)
        self.assertNotIn("plan next year", digest.answer)

        # Overdue/due-soon items repeat every run by design (no cooldown).
        digest_again = self.writer.run("/mem digest")
        self.assertFalse(digest_again.suppress_if_routine)
        self.assertIn("pay overdue invoice", digest_again.answer)

    def test_digest_is_suppressed_when_nothing_is_due_or_stale(self) -> None:
        self.writer.run("/mem just a plain note")
        digest = self.writer.run("/mem digest")
        self.assertTrue(digest.suppress_if_routine)
        self.assertIn("all caught up", digest.answer)


class ChatMemoryScenario(unittest.TestCase):
    """Needs only the in-process fake server -- always runs."""

    def setUp(self) -> None:
        self.seen: list[list[dict]] = []

        def responder(messages):
            self.seen.append(messages)
            return "ack"

        self.fake = FakeInferenceServer(dim=DIM, chat_responder=responder)
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = build_settings(
            vllm_base_url=self.fake.openai_base_url,
            request_timeout=10,
            conversation_memory_enabled=True,
            conversation_store_path=Path(self.tmp.name) / "conv",
            conversation_history_turns=4,
        )
        self.memory = ConversationMemory(self.settings)
        self.agent = ChatAgent(LlmClient(self.settings), self.memory)

    def _say(self, chat_id: int, text: str) -> str:
        return self.agent.run(Task(task_id="t", source="telegram_text", text=text, chat_id=chat_id)).answer

    def test_second_turn_carries_the_first_exchange(self) -> None:
        self._say(7, "the capital of Japan?")
        self._say(7, "and its population?")

        first_call_roles = [m["role"] for m in self.seen[0]]
        second_call_roles = [m["role"] for m in self.seen[1]]
        self.assertEqual(first_call_roles, ["system", "user"])
        self.assertEqual(second_call_roles, ["system", "user", "assistant", "user"])
        self.assertIn("capital of Japan", self.seen[1][1]["content"])

    def test_new_conversation_drops_history(self) -> None:
        self._say(7, "first message")
        self.assertTrue(self.memory.clear(7))
        self._say(7, "fresh start")
        self.assertEqual([m["role"] for m in self.seen[1]], ["system", "user"])

    def test_other_chat_is_isolated(self) -> None:
        self._say(1, "chat one context")
        self._say(2, "chat two question")
        self.assertEqual([m["role"] for m in self.seen[1]], ["system", "user"])


if __name__ == "__main__":
    unittest.main()
