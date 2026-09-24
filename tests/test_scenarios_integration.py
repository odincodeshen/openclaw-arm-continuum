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
import time
import unittest
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from openclaw_runtime import categories
from openclaw_runtime.agents.base import Task
from openclaw_runtime.agents.skill_agents import ChatAgent
from openclaw_runtime.categories import category_slug
from openclaw_runtime.conversation_memory import ConversationMemory
from openclaw_runtime.daily_task_tracking import (
    mark_task_completed,
    mark_task_pushed,
    sweep_incomplete_to_skipped,
)
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.owned_records import read_owned_points, write_owned_point
from openclaw_runtime.skills.english_bot import (
    Chunk,
    WeeklyContent,
    build_cloze_question,
    evaluate_saturday_answers,
    mark_ielts_question_asked,
    next_week_number,
    pick_ielts_question,
    read_chunk_progress,
    read_this_week_chunks,
    read_this_week_payload,
    record_chunk_usage,
    store_weekly_content,
)
from openclaw_runtime.transcription_client import TranscriptSegment
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

    def test_rag_tag_prefix_scopes_to_tracker_items_with_that_tag_against_real_qdrant(self) -> None:
        writer = MemoryWriteSkill(self.settings, {}, self.embeddings, self.qdrant)
        writer.run("/mem deploy note about the gateway mount tag:work")
        writer.run("/mem pick up the dry cleaning tag:home")

        work_answer = self.rag.run("/rag tag:work what does the gateway container mount?").answer
        self.assertIn("gateway", work_answer.lower())
        self.assertNotIn("dry cleaning", work_answer.lower())

        home_answer = self.rag.run("/rag tag:home what needs picking up?").answer
        self.assertIn("dry cleaning", home_answer.lower())
        self.assertNotIn("gateway", home_answer.lower())

    def test_rag_date_range_filters_against_real_qdrant(self) -> None:
        # Proves the created_at range condition -- since: inclusive,
        # before: exclusive -- really works against a real Qdrant, not
        # just the fake: the item is written "now" (today), so since:today
        # includes it, before:today excludes it, and a wide range spanning
        # both sides of today includes it.
        #
        # UTC, not local date.today() -- /mem's created_at is a UTC epoch
        # (int(time.time())) and since:/before: convert their date strings
        # via UTC midnight, so the test's notion of "today" has to match
        # that clock or it can be off by a day near local midnight (e.g.
        # BST vs UTC).
        writer = MemoryWriteSkill(self.settings, {}, self.embeddings, self.qdrant)
        writer.run("/mem deploy note about the gateway mount")
        utc_today = datetime.now(timezone.utc).date()
        today = utc_today.isoformat()
        yesterday = (utc_today - timedelta(days=1)).isoformat()
        tomorrow = (utc_today + timedelta(days=1)).isoformat()

        since_today = self.rag.run(f"/rag since:{today} what does the gateway container mount?").answer
        self.assertIn("gateway", since_today.lower())

        before_today = self.rag.run(f"/rag before:{today} what does the gateway container mount?").answer
        self.assertNotIn("gateway", before_today.lower())

        since_tomorrow = self.rag.run(
            f"/rag since:{tomorrow} what does the gateway container mount?"
        ).answer
        self.assertNotIn("gateway", since_tomorrow.lower())

        spanning_range = self.rag.run(
            f"/rag since:{yesterday} before:{tomorrow} what does the gateway container mount?"
        ).answer
        self.assertIn("gateway", spanning_range.lower())


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

    def test_list_and_digest_tag_filter_against_real_qdrant(self) -> None:
        # Proves Qdrant's match-on-a-list-payload-field semantics ("the tag
        # is one of the item's tags") really work the way FakeQdrant assumes,
        # not just against a real Qdrant's scroll -- for both /mem list and
        # /mem digest.
        today = date.today()
        overdue_due = (today - timedelta(days=1)).isoformat()
        self.writer.run(f"/mem work overdue thing due:{overdue_due} tag:work")
        self.writer.run("/mem home errand tag:home")

        work_list = self.writer.run("/mem list tag:work").answer
        self.assertIn("work overdue thing", work_list)
        self.assertNotIn("home errand", work_list)

        work_digest = self.writer.run("/mem digest tag:work").answer
        self.assertIn("work overdue thing", work_digest)

        home_digest = self.writer.run("/mem digest tag:home")
        self.assertTrue(home_digest.suppress_if_routine)
        self.assertIn('tagged "home"', home_digest.answer)

    def test_archive_stale_sweep_against_real_qdrant(self) -> None:
        self.writer.run("/mem old idea that never got done")
        hits = self.qdrant.scroll_by_filters(self.tracker, {"kind": "tracker_memory"}, limit=50)
        point_id = next(h["id"] for h in hits if "old idea" in h["payload"]["text"])
        backdated = int(time.time()) - 30 * 86400
        self.qdrant.set_payload(self.tracker, point_id, {"updated_at": backdated})

        result = self.writer.run("/mem archive-stale")
        self.assertFalse(result.suppress_if_routine)
        self.assertIn("old idea", result.answer)

        active_after = self.writer.run("/mem list").answer
        self.assertNotIn("old idea", active_after)

        archived_after = self.writer.run("/mem list archived").answer
        self.assertIn("old idea", archived_after)

        # already-archived, so a second sweep finds nothing left to do
        second_sweep = self.writer.run("/mem archive-stale")
        self.assertTrue(second_sweep.suppress_if_routine)

    def test_delete_collection_against_real_qdrant(self) -> None:
        # Backs /cat merge's cleanup step: prove QdrantClient.delete_collection
        # actually removes a real collection, not just that it sends *a* request.
        collection = f"{self.prefix}delete-me"
        self.qdrant.ensure_collection(collection)
        self.qdrant.upsert_text(collection, "hello", [0.0] * DIM, {})
        self.assertEqual(self.qdrant.points_count(collection), 1)

        self.qdrant.delete_collection(collection)

        self.assertIsNone(self.qdrant.points_count(collection))


class EnglishLearningScenario(QdrantScenarioBase):
    """v1.11 foundation for the English-learning bot: proves owner-scoped
    read/write and the daily-task-completion tracker actually behave
    correctly against a real Qdrant, not just against FakeQdrant's
    assumptions about scroll/set_payload filtering semantics."""

    def test_owner_filter_isolates_family_members_on_real_qdrant(self) -> None:
        vector_a = self.embeddings.embed("owner a chunk usage")
        vector_b = self.embeddings.embed("owner b chunk usage")

        write_owned_point(
            self.qdrant,
            self.tracker,
            "owner-a",
            "owner a's sentence",
            vector_a,
            {"tag": "eng_wk1", "chunk": "spread oneself too thin"},
        )
        write_owned_point(
            self.qdrant,
            self.tracker,
            "owner-b",
            "owner b's sentence",
            vector_b,
            {"tag": "eng_wk1", "chunk": "spread oneself too thin"},
        )

        owner_a_records = read_owned_points(self.qdrant, self.tracker, "owner-a", {"tag": "eng_wk1"})
        self.assertEqual(len(owner_a_records), 1)
        self.assertEqual(owner_a_records[0]["payload"]["text"], "owner a's sentence")

        owner_b_records = read_owned_points(self.qdrant, self.tracker, "owner-b", {"tag": "eng_wk1"})
        self.assertEqual(len(owner_b_records), 1)
        self.assertEqual(owner_b_records[0]["payload"]["text"], "owner b's sentence")

    def test_daily_task_completion_round_trip_against_real_qdrant(self) -> None:
        vector = self.embeddings.embed("monday task pushed")
        mark_task_pushed(self.qdrant, self.tracker, 1, "mon", "owner-a", vector)

        pushed = read_owned_points(self.qdrant, self.tracker, "owner-a", {"tag": "eng_wk1_daymon"})
        self.assertEqual(len(pushed), 1)
        self.assertFalse(pushed[0]["payload"]["completed"])

        completed = mark_task_completed(self.qdrant, self.tracker, 1, "mon", "owner-a")
        self.assertTrue(completed)

        after = read_owned_points(self.qdrant, self.tracker, "owner-a", {"tag": "eng_wk1_daymon"})
        self.assertTrue(after[0]["payload"]["completed"])

    def test_sweep_marks_incomplete_owner_skipped_against_real_qdrant(self) -> None:
        vector_a = self.embeddings.embed("tuesday task pushed a")
        vector_b = self.embeddings.embed("tuesday task pushed b")
        mark_task_pushed(self.qdrant, self.tracker, 1, "tue", "owner-a", vector_a)
        mark_task_pushed(self.qdrant, self.tracker, 1, "tue", "owner-b", vector_b)
        mark_task_completed(self.qdrant, self.tracker, 1, "tue", "owner-b")

        swept = sweep_incomplete_to_skipped(
            self.qdrant, self.tracker, 1, "tue", ["owner-a", "owner-b"]
        )

        self.assertEqual(swept, ["owner-a"])
        owner_a_after = read_owned_points(self.qdrant, self.tracker, "owner-a", {"tag": "eng_wk1_daytue"})
        self.assertTrue(owner_a_after[0]["payload"]["skipped"])
        owner_b_after = read_owned_points(self.qdrant, self.tracker, "owner-b", {"tag": "eng_wk1_daytue"})
        self.assertFalse(owner_b_after[0]["payload"]["skipped"])

    def test_weekly_content_round_trips_shared_with_no_owner_against_real_qdrant(self) -> None:
        """v1.12: Monday's chunks + transcript_excerpt + window_segments_json
        must persist correctly against real Qdrant and be readable back by
        week_number with no owner field at all (shared family content, not
        gated behind any one person's chat_id)."""
        week_number = next_week_number(self.qdrant, self.tracker)
        content = WeeklyContent(
            week_number=week_number,
            episode_title="Demis Hassabis",
            episode_guid="urn:bbc:podcast:test-episode",
            segment_start=300.0,
            segment_end=480.0,
            transcript_excerpt="I remember walking into the lab for the first time...",
            chunks=[
                Chunk("take a gamble on", "冒險一試", "We took a gamble on the new design."),
                Chunk("spread oneself too thin", "心力過度分散", "I was spreading myself too thin."),
                Chunk("get to grips with", "掌握複雜事物", "It took weeks to get to grips with it."),
            ],
            window_segments=[
                TranscriptSegment(start=0.0, end=5.0, text="Host: tell me about that."),
                TranscriptSegment(start=5.0, end=45.0, text="I remember walking into the lab..."),
            ],
        )

        store_weekly_content(self.qdrant, self.embeddings, self.tracker, content)

        payload = read_this_week_payload(self.qdrant, self.tracker, week_number)
        self.assertNotIn("owner", payload)
        self.assertEqual(payload["tag"], f"eng_wk{week_number}")
        self.assertEqual(payload["episode_guid"], "urn:bbc:podcast:test-episode")
        self.assertEqual(payload["transcript_excerpt"], "I remember walking into the lab for the first time...")
        segments_back = json.loads(payload["window_segments_json"])
        self.assertEqual(len(segments_back), 2)
        self.assertEqual(segments_back[1]["text"], "I remember walking into the lab...")

        # next_week_number now correctly skips past this week
        self.assertEqual(next_week_number(self.qdrant, self.tracker), week_number + 1)

    def test_ielts_asked_question_dedup_persists_against_real_qdrant(self) -> None:
        """v1.13: pick_ielts_question()'s exclusion of already-asked
        questions has to hold up against a real Qdrant round trip, not just
        a mocked scroll_by_filters -- proves the shared (no-owner)
        tag:eng_ielts_topics write/read actually works end to end."""
        first_pick = pick_ielts_question(self.qdrant, self.tracker)
        mark_ielts_question_asked(self.qdrant, self.embeddings, self.tracker, first_pick)

        second_pick = pick_ielts_question(self.qdrant, self.tracker)
        self.assertNotEqual(second_pick["id"], first_pick["id"])

        mark_ielts_question_asked(self.qdrant, self.embeddings, self.tracker, second_pick)
        # both marks are shared content -- no owner field at all
        points = self.qdrant.scroll_by_filters(self.tracker, {"tag": "eng_ielts_topics"}, limit=64)
        for point in points:
            self.assertNotIn("owner", point.get("payload") or {})

    def test_monday_friday_saturday_chain_persists_needs_review_against_real_qdrant(self) -> None:
        """v1.14 centerpiece: Monday writes a shared chunk -> Friday records
        this owner's real spoken sentence for it (used correctly) -> Saturday
        builds its cloze question from that real sentence (not the shared
        context_sentence) -> a wrong Saturday answer flips needs_review to
        True even though Friday's use was correct -- proves the "any single
        failure this week marks it" rule (spec 2.5 point 4) actually holds
        across three separate days' writes against a real Qdrant, not just
        within one mocked call. Only exercises the deterministic pieces
        (record_chunk_usage, build_cloze_question, evaluate_saturday_answers)
        -- the LLM-judgment steps that produce their inputs are covered by
        the unit tests with a mocked LLM, per the established L0/L2 split."""
        week_number = next_week_number(self.qdrant, self.tracker)
        content = WeeklyContent(
            week_number=week_number,
            episode_title="Test Episode",
            episode_guid=f"urn:test:{week_number}",
            segment_start=300.0,
            segment_end=480.0,
            transcript_excerpt="...",
            chunks=[Chunk("get to grips with", "掌握複雜事物", "It took weeks to get to grips with it.")],
            window_segments=[],
        )
        store_weekly_content(self.qdrant, self.embeddings, self.tracker, content)
        chunks = read_this_week_chunks(self.qdrant, self.tracker, week_number)
        self.assertEqual(len(chunks), 1)
        phrase = chunks[0]["phrase"]

        # Friday: this owner used the chunk correctly, with their own real
        # sentence -- as evaluate_friday_reply (LLM-driven) would record.
        record_chunk_usage(
            self.qdrant,
            self.embeddings,
            self.tracker,
            "owner-a",
            week_number,
            phrase,
            used_correctly=True,
            user_sentence="I am finally getting to grips with the new deployment pipeline.",
            source="fri_voice",
        )
        progress = read_chunk_progress(self.qdrant, self.tracker, "owner-a", week_number, phrase)
        self.assertFalse(progress["needs_review"])

        # Saturday: the cloze question must be built from Friday's real
        # sentence, not the shared context_sentence.
        question = build_cloze_question(chunks[0], progress)
        self.assertEqual(question.source, "user_sentence")
        self.assertIn("deployment pipeline", question.prompt_text)

        # A wrong Saturday answer flips needs_review, even though Friday's
        # use of the same chunk this week was correct.
        evaluate_saturday_answers(
            self.qdrant, self.embeddings, self.tracker, "owner-a", week_number, [phrase], "I have no idea."
        )
        after = read_chunk_progress(self.qdrant, self.tracker, "owner-a", week_number, phrase)
        self.assertTrue(after["needs_review"])
        # the real Friday sentence must survive Saturday's update untouched
        self.assertEqual(
            after["user_sentence"], "I am finally getting to grips with the new deployment pipeline."
        )


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


class ChatMemoryRollingSummaryScenario(unittest.TestCase):
    """Real LlmClient.chat() round trip for the summarization prompt itself,
    not just a fake standing in for it -- proves the prompt/response wiring
    actually works end to end against the OpenAI-shaped fake server."""

    def setUp(self) -> None:
        self.calls: list[list[dict]] = []

        def responder(messages):
            self.calls.append(messages)
            last_user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            if "compressing an ongoing chat's history" in last_user:
                return "User discussed Tokyo and asked about its population."
            return f"ack:{len(self.calls)}"

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
            conversation_history_turns=2,
            conversation_summary_enabled=True,
        )
        self.llm = LlmClient(self.settings)
        self.memory = ConversationMemory(self.settings, self.llm)
        self.agent = ChatAgent(self.llm, self.memory)

    def _say(self, chat_id: int, text: str) -> str:
        return self.agent.run(Task(task_id="t", source="telegram_text", text=text, chat_id=chat_id)).answer

    def test_overflow_triggers_a_real_summarization_call_and_it_is_replayed(self) -> None:
        # window = 2 exchanges (conversation_history_turns=2); the 3rd
        # exchange's record() call is what pushes the 1st exchange out and
        # triggers exactly one real summarization call.
        self._say(7, "capital of Japan?")
        self._say(7, "and its climate?")
        self._say(7, "anything else notable?")

        summarizer_calls = [
            c
            for c in self.calls
            if any("compressing an ongoing chat's history" in m["content"] for m in c if m["role"] == "user")
        ]
        self.assertEqual(len(summarizer_calls), 1)

        data = self.memory._read(7)
        self.assertIn("Tokyo", data["summary"])

        # The *next* turn's outgoing request should carry that summary. Its
        # own record() call may itself trigger another summarization
        # afterwards (appended later to self.calls), so capture the request
        # at the index it lands on, not whatever call happens last.
        before = len(self.calls)
        self._say(7, "one more question")
        fourth_call = self.calls[before]
        context_message = fourth_call[1]["content"]
        self.assertIn("Summary of earlier conversation", context_message)
        self.assertIn("Tokyo", context_message)


if __name__ == "__main__":
    unittest.main()
