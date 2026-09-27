import dataclasses
import unittest
from unittest.mock import MagicMock, patch

from openclaw_runtime.skills.memory import (
    RagRetrieveSkill,
    filter_hits_by_source,
    matches_source,
    split_rag_filter_prefix,
)
from tests.support import build_settings


def _hit(text, **payload):
    return {"score": 0.9, "payload": {"text": text, **payload}}


class ParseSourcePrefixTest(unittest.TestCase):
    def test_source_is_a_prefix_filter_combinable_with_others(self) -> None:
        filters, rest = split_rag_filter_prefix("source:q1-plan since:2026-09-01 what is due?")
        self.assertEqual(filters, {"source": "q1-plan", "since": "2026-09-01"})
        self.assertEqual(rest, "what is due?")

    def test_source_before_a_category(self) -> None:
        filters, rest = split_rag_filter_prefix("source:youtu #work open items?")
        self.assertEqual(filters, {"source": "youtu"})
        self.assertEqual(rest, "#work open items?")


class MatchesSourceTest(unittest.TestCase):
    def test_matches_any_source_field_case_insensitively(self) -> None:
        self.assertTrue(matches_source({"original_file_name": "Q1-Plan.pdf"}, "q1-plan"))
        self.assertTrue(matches_source({"source_url": "https://youtu.be/abc"}, "YOUTU"))
        self.assertTrue(matches_source({"doc_title": "Rack diagram"}, "rack"))
        self.assertTrue(matches_source({"file_name": "20260926-notes.md"}, "notes"))
        self.assertFalse(matches_source({"text": "mentions q1-plan in the body only"}, "q1-plan"))

    def test_filter_keeps_order_and_caps(self) -> None:
        hits = [_hit("a", file_name="x.md"), _hit("b", file_name="plan.md"), _hit("c", file_name="plan2.md")]
        self.assertEqual([h["payload"]["text"] for h in filter_hits_by_source(hits, "plan", 1)], ["b"])
        self.assertIs(filter_hits_by_source(hits, None, 1), hits)


class RagSourceFilterTest(unittest.TestCase):
    def _skill(self, search_results):
        settings = dataclasses.replace(build_settings(), category_rag_enabled=True, retrieval_limit=5)
        qdrant = MagicMock()
        qdrant.search.side_effect = lambda collection, vector, limit=None, **kw: search_results.get(collection, [])
        qdrant.scroll_by_file_name.return_value = []
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        llm = MagicMock()
        llm.chat.return_value = "Answer."
        return RagRetrieveSkill(settings, {}, embeddings, qdrant, llm), qdrant, llm, settings

    def test_default_search_keeps_only_matching_sources_and_overfetches(self) -> None:
        settings = build_settings()
        skill, qdrant, llm, settings = self._skill(
            {
                settings.knowledge_collection: [
                    _hit("from the plan", original_file_name="Q1-plan.pdf"),
                    _hit("from elsewhere", original_file_name="notes.md"),
                ],
            }
        )
        result = skill.run("/rag source:q1-plan what is due?")
        self.assertIn("Sources: Q1-plan.pdf", result.answer)
        self.assertNotIn("notes.md", result.answer)
        context = llm.chat.call_args.args[0]
        self.assertIn("from the plan", context)
        self.assertNotIn("from elsewhere", context)
        limits = {call.kwargs["limit"] for call in qdrant.search.call_args_list}
        self.assertEqual(limits, {20})

    def test_no_match_says_so(self) -> None:
        settings = build_settings()
        skill, _, _, _ = self._skill({settings.knowledge_collection: [_hit("x", file_name="notes.md")]})
        result = skill.run("/rag source:nothing-like-this what?")
        self.assertIn('from a source matching "nothing-like-this"', result.answer)

    def test_without_source_the_normal_limit_is_used(self) -> None:
        skill, qdrant, _, _ = self._skill({})
        skill.run("/rag what?")
        self.assertEqual({call.kwargs["limit"] for call in qdrant.search.call_args_list}, {5})

    def test_single_category_is_filtered_too(self) -> None:
        skill, _, llm, _ = self._skill(
            {"cat_work": [_hit("keep", source_url="https://example.com/a"), _hit("drop", file_name="b.md")]}
        )
        with patch(
            "openclaw_runtime.skills.memory.resolve_category",
            return_value={"known": True, "collection": "cat_work", "display": "work"},
        ):
            result = skill.run("/rag source:example.com #work summary?")
        context = llm.chat.call_args.args[0]
        self.assertIn("keep", context)
        self.assertNotIn("drop", context)
        self.assertEqual(result.answer, "Answer.")  # only a source_url, so no Sources: line

    def test_single_category_with_no_match(self) -> None:
        skill, _, _, _ = self._skill({"cat_work": [_hit("drop", file_name="b.md")]})
        with patch(
            "openclaw_runtime.skills.memory.resolve_category",
            return_value={"known": True, "collection": "cat_work", "display": "work"},
        ):
            result = skill.run("/rag source:zzz #work summary?")
        self.assertEqual(result.answer, 'Nothing in category "work" comes from a source matching "zzz".')

    def test_all_categories_is_filtered_too(self) -> None:
        skill, _, llm, _ = self._skill(
            {"cat_a": [_hit("keep", doc_title="Rack diagram")], "cat_b": [_hit("drop", doc_title="Other")]}
        )
        with patch(
            "openclaw_runtime.skills.memory.registry_entries",
            return_value=[{"display": "a", "collection": "cat_a"}, {"display": "b", "collection": "cat_b"}],
        ):
            skill.run("/rag source:rack #all where?")
        context = llm.chat.call_args.args[0]
        self.assertIn("keep", context)
        self.assertNotIn("drop", context)



class PlainRagIncludesCategoriesTest(unittest.TestCase):
    ENTRIES = [{"display": "aitool", "collection": "cat_tool"}, {"display": "mindset", "collection": "cat_mind"}]

    def _skill(self, include: bool, results: dict):
        settings = dataclasses.replace(
            build_settings(), category_rag_enabled=True, rag_include_categories=include, retrieval_limit=5
        )
        qdrant = MagicMock()
        qdrant.search.side_effect = lambda collection, vector, limit=None, **kw: results.get(collection, [])
        qdrant.scroll_by_file_name.return_value = []
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        llm = MagicMock()
        llm.chat.return_value = "Answer."
        return RagRetrieveSkill(settings, {}, embeddings, qdrant, llm), qdrant, llm

    def test_plain_rag_searches_every_category_three_hits_each(self) -> None:
        skill, qdrant, llm = self._skill(True, {"cat_tool": [_hit("a video summary", file_name="reply.md")]})
        with patch("openclaw_runtime.skills.memory.registry_entries", return_value=self.ENTRIES):
            result = skill.run("/rag what did the video say?")
        searched = {call.args[0]: call.kwargs["limit"] for call in qdrant.search.call_args_list}
        self.assertEqual(searched["cat_tool"], 3)
        self.assertEqual(searched["cat_mind"], 3)
        self.assertIn("category:aitool", llm.chat.call_args.args[0])
        self.assertIn("a video summary", llm.chat.call_args.args[0])
        self.assertEqual(result.answer, "Answer.\n\nSources: reply.md")

    def test_date_filter_applies_to_categories_too(self) -> None:
        skill, qdrant, _ = self._skill(True, {})
        with patch("openclaw_runtime.skills.memory.registry_entries", return_value=self.ENTRIES):
            skill.run("/rag since:2026-09-01 anything?")
        category_calls = [c for c in qdrant.search.call_args_list if c.args[0].startswith("cat_")]
        self.assertTrue(all(c.kwargs.get("since") for c in category_calls))

    def test_tag_filter_keeps_to_tracker_memory(self) -> None:
        skill, qdrant, _ = self._skill(True, {})
        with patch("openclaw_runtime.skills.memory.registry_entries", return_value=self.ENTRIES):
            skill.run("/rag tag:work anything?")
        self.assertFalse([c for c in qdrant.search.call_args_list if c.args[0].startswith("cat_")])

    def test_can_be_switched_off(self) -> None:
        skill, qdrant, _ = self._skill(False, {})
        with patch("openclaw_runtime.skills.memory.registry_entries", return_value=self.ENTRIES):
            skill.run("/rag anything?")
        self.assertFalse([c for c in qdrant.search.call_args_list if c.args[0].startswith("cat_")])

    def test_nothing_found_message(self) -> None:
        skill, _, _ = self._skill(True, {})
        with patch("openclaw_runtime.skills.memory.registry_entries", return_value=self.ENTRIES):
            self.assertEqual(skill.run("/rag anything?").answer, "No relevant memory or document was found.")


if __name__ == "__main__":
    unittest.main()
