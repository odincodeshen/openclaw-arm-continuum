import dataclasses
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from openclaw_runtime.daily_reports import (
    NewDocument,
    day_bounds,
    documents_added_on,
    render_knowledge_digest,
    render_upcoming,
    summarize_document,
)
from openclaw_runtime.skills.memory import MemoryWriteSkill, RagRetrieveSkill
from tests.support import build_settings

TODAY = date(2026, 9, 27)  # a Sunday


class RenderUpcomingTest(unittest.TestCase):
    def test_groups_the_next_three_days_and_skips_everything_else(self) -> None:
        items = [
            {"text": "Dentist", "short_id": "a1", "due": "2026-09-27"},
            {"text": "Pay rent", "short_id": "b2", "due": "2026-09-29"},
            {"text": "Too late", "short_id": "c3", "due": "2026-09-30"},
            {"text": "Overdue", "short_id": "d4", "due": "2026-09-20"},
            {"text": "No date", "short_id": "e5"},
            {"text": "Bad date", "short_id": "f6", "due": "soon"},
            {"text": "Call mum", "short_id": "g7", "due": "2026-09-27"},
        ]
        report = render_upcoming(items, TODAY)
        self.assertTrue(report.startswith("行程｜未來3天\n━"))
        self.assertIn("Today · Sun 27 Sep\n• Dentist  #a1\n• Call mum  #g7", report)
        self.assertIn("Tue 29 Sep\n• Pay rent  #b2", report)
        for missing in ("Too late", "Overdue", "No date", "Bad date", "Tomorrow"):
            self.assertNotIn(missing, report)
        self.assertLess(report.index("Today"), report.index("Tue 29 Sep"))

    def test_tomorrow_label(self) -> None:
        report = render_upcoming([{"text": "X", "short_id": "a", "due": "2026-09-28"}], TODAY)
        self.assertIn("Tomorrow · Mon 28 Sep", report)

    def test_empty_still_reports(self) -> None:
        report = render_upcoming([], TODAY)
        self.assertIn("No schedule in the next 3 days.", report)
        self.assertIn("/mem <what> due:YYYY-MM-DD", report)


class DocumentsAddedOnTest(unittest.TestCase):
    def test_day_bounds_are_local_midnight_to_midnight(self) -> None:
        start, end = day_bounds(date(2026, 9, 26), "Europe/London")
        self.assertEqual(end - start, 86400)
        self.assertEqual(start, 1790377200)  # 2026-09-26 00:00 BST = 2026-09-25 23:00 UTC

    def test_groups_chunks_into_documents_in_chunk_order(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [
            {"id": 1, "payload": {"file_sha256": "s1", "chunk_index": 1, "text": "second", "doc_title": "Doc"}},
            {"id": 2, "payload": {"file_sha256": "s1", "chunk_index": 0, "text": "first", "doc_title": "Doc"}},
            {"id": 3, "payload": {"file_sha256": "s2", "chunk_index": 0, "text": "other", "file_name": "b.md"}},
        ]
        docs = documents_added_on(qdrant, [("#ai", "coll")], date(2026, 9, 26), "Europe/London")
        self.assertEqual(
            docs, [NewDocument("#ai", "Doc", "first\nsecond"), NewDocument("#ai", "b.md", "other")]
        )
        _, kwargs = qdrant.scroll_by_filters.call_args
        self.assertEqual((kwargs["since"], kwargs["before"]), day_bounds(date(2026, 9, 26), "Europe/London"))


class RenderKnowledgeDigestTest(unittest.TestCase):
    def test_one_sentence_per_document_grouped_by_source(self) -> None:
        summaries = [
            (NewDocument("Knowledge base", "a", "x"), "Sentence A."),
            (NewDocument("#ai", "b", "y"), "Sentence B."),
            (NewDocument("#ai", "c", "z"), "Sentence C."),
        ]
        report = render_knowledge_digest(date(2026, 9, 26), summaries, more=2)
        self.assertTrue(report.startswith("知識｜昨日新增\n━"))
        self.assertIn("Sat 26 Sep · 5 new", report)
        self.assertIn("Knowledge base\n• Sentence A.\n\n#ai\n• Sentence B.\n• Sentence C.", report)
        self.assertIn("…and 2 more.", report)

    def test_weekly_title_and_period(self) -> None:
        report = render_knowledge_digest(date(2026, 10, 3), [(NewDocument("#ai", "t", "x"), "One.")], days=7)
        self.assertTrue(report.startswith("知識｜本週新增\n━"))
        self.assertIn("27 Sep – 03 Oct · 1 new", report)
        self.assertIn("No new knowledge was added this week.", render_knowledge_digest(date(2026, 10, 3), [], days=7))

    def test_empty_still_reports(self) -> None:
        report = render_knowledge_digest(date(2026, 9, 26), [])
        self.assertIn("Sat 26 Sep · 0 new", report)
        self.assertIn("No new knowledge was added yesterday.", report)

    def test_summary_prompt_asks_for_one_sentence_in_the_reply_language(self) -> None:
        llm = MagicMock()
        llm.chat.return_value = "  一句話\n總結。 "
        summary = summarize_document(llm, NewDocument("#ai", "Doc", "body"), "Traditional Chinese (繁體中文)")
        self.assertEqual(summary, "一句話 總結。")
        prompt = llm.chat.call_args.args[0]
        self.assertIn("exactly one", prompt)
        self.assertIn("Traditional Chinese", prompt)


class MemUpcomingCommandTest(unittest.TestCase):
    def _skill(self, hits):
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = hits
        settings = build_settings(cron_timezone="Europe/London")
        return MemoryWriteSkill(settings, {}, MagicMock(), qdrant), qdrant

    def test_reads_active_tracker_items(self) -> None:
        skill, qdrant = self._skill([{"payload": {"text": "Dentist", "short_id": "a1", "due": "2026-09-27"}}])
        with patch("openclaw_runtime.skills.memory.local_today", return_value=TODAY):
            result = skill.run("/mem upcoming")
        self.assertIn("• Dentist  #a1", result.answer)
        self.assertFalse(result.suppress_if_routine)
        self.assertEqual(qdrant.scroll_by_filters.call_args.args[1], {"kind": "tracker_memory", "status": "active"})

    def test_custom_day_count(self) -> None:
        skill, _ = self._skill([])
        with patch("openclaw_runtime.skills.memory.local_today", return_value=TODAY):
            self.assertIn("未來7天", skill.run("/mem upcoming 7d").answer)
            self.assertIn("未來3天", skill.run("/mem upcoming nonsense").answer)


class RagWeeklyDigestCommandTest(unittest.TestCase):
    def test_covers_the_seven_days_up_to_yesterday(self) -> None:
        settings = dataclasses.replace(build_settings(cron_timezone="Europe/London"), category_rag_enabled=False)
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        skill = RagRetrieveSkill(settings, {}, MagicMock(), qdrant, MagicMock())
        with patch("openclaw_runtime.skills.memory.local_today", return_value=date(2026, 10, 4)):  # a Sunday
            result = skill.run("/rag digest week")
        self.assertIn("27 Sep – 03 Oct · 0 new", result.answer)
        kwargs = qdrant.scroll_by_filters.call_args.kwargs
        self.assertEqual(kwargs["since"], day_bounds(date(2026, 9, 27), "Europe/London")[0])
        self.assertEqual(kwargs["before"], day_bounds(date(2026, 10, 3), "Europe/London")[1])


class RagDigestCommandTest(unittest.TestCase):
    def test_reads_knowledge_and_categories_but_not_tracker_memory(self) -> None:
        settings = dataclasses.replace(
            build_settings(cron_timezone="Europe/London", reply_language="Traditional Chinese"),
            category_rag_enabled=True,
        )
        qdrant = MagicMock()
        qdrant.scroll_by_filters.side_effect = lambda collection, filters, limit=64, since=None, before=None: (
            [{"id": 1, "payload": {"file_sha256": "s", "chunk_index": 0, "text": "note", "doc_title": "t"}}]
            if collection == "cat_ai"
            else []
        )
        llm = MagicMock()
        llm.chat.return_value = "One sentence."
        skill = RagRetrieveSkill(settings, {}, MagicMock(), qdrant, llm)
        with patch("openclaw_runtime.skills.memory.local_today", return_value=TODAY), \
                patch("openclaw_runtime.skills.memory.registry_entries",
                      return_value=[{"display": "AI", "collection": "cat_ai"}]):
            result = skill.run("/rag digest")
        self.assertIn("Sat 26 Sep · 1 new", result.answer)
        self.assertIn("#AI\n• One sentence.", result.answer)
        scanned = [call.args[0] for call in qdrant.scroll_by_filters.call_args_list]
        self.assertEqual(scanned, [settings.knowledge_collection, "cat_ai"])
        self.assertNotIn(settings.tracker_collection, scanned)

    def test_llm_failure_falls_back_to_the_opening_text(self) -> None:
        settings = dataclasses.replace(build_settings(), category_rag_enabled=False)
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [
            {"id": 1, "payload": {"file_sha256": "s", "chunk_index": 0, "text": "Opening   words here"}}
        ]
        llm = MagicMock()
        llm.chat.side_effect = RuntimeError("down")
        skill = RagRetrieveSkill(settings, {}, MagicMock(), qdrant, llm)
        with patch("openclaw_runtime.skills.memory.local_today", return_value=TODAY):
            self.assertIn("• Opening words here", skill.run("/rag digest").answer)


if __name__ == "__main__":
    unittest.main()
