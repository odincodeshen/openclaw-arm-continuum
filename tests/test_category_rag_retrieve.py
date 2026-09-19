import tempfile
import unittest
from pathlib import Path

from openclaw_runtime import categories
from openclaw_runtime.skills.memory import RagRetrieveSkill, split_category_prefix, split_rag_filter_prefix

from tests.support import build_settings


class SplitCategoryPrefixTest(unittest.TestCase):
    def test_no_prefix(self) -> None:
        self.assertEqual(split_category_prefix("what did I save"), (None, "what did I save"))

    def test_bare_token(self) -> None:
        self.assertEqual(split_category_prefix("#工作筆記 這份的重點"), ("工作筆記", "這份的重點"))

    def test_bracketed_multi_word(self) -> None:
        self.assertEqual(
            split_category_prefix("#[Work Notes] summarise this"),
            ("Work Notes", "summarise this"),
        )

    def test_all_sentinel(self) -> None:
        token, rest = split_category_prefix("#all where is the rack diagram")
        self.assertNotIn(token, (None, "all"))
        self.assertEqual(rest, "where is the rack diagram")

    def test_fullwidth_hash_from_cjk_ime(self) -> None:
        self.assertEqual(split_category_prefix("＃AI應用 這個類別有什麼"), ("AI應用", "這個類別有什麼"))

    def test_hash_then_space_then_name(self) -> None:
        self.assertEqual(split_category_prefix("# 工作筆記 問題"), ("工作筆記", "問題"))


class SplitRagFilterPrefixTest(unittest.TestCase):
    def test_no_prefix(self) -> None:
        self.assertEqual(split_rag_filter_prefix("what did I save"), ({}, "what did I save"))

    def test_tag_prefix(self) -> None:
        self.assertEqual(
            split_rag_filter_prefix("tag:work what's due this week"),
            ({"tag": "work"}, "what's due this week"),
        )

    def test_case_insensitive_keyword(self) -> None:
        self.assertEqual(split_rag_filter_prefix("TAG:work anything"), ({"tag": "work"}, "anything"))

    def test_tag_with_no_question_leaves_rest_empty(self) -> None:
        self.assertEqual(split_rag_filter_prefix("tag:work "), ({"tag": "work"}, ""))

    def test_tag_alone_with_no_question_is_recognized_with_empty_remainder(self) -> None:
        # RagRetrieveSkill.run() rejects an empty remaining query with its
        # own "Add the question to look up after /rag." message.
        self.assertEqual(split_rag_filter_prefix("tag:work"), ({"tag": "work"}, ""))

    def test_mid_sentence_tag_looking_text_is_not_a_prefix(self) -> None:
        self.assertEqual(
            split_rag_filter_prefix("what does tag:work mean"), ({}, "what does tag:work mean")
        )

    def test_since_prefix(self) -> None:
        self.assertEqual(
            split_rag_filter_prefix("since:2026-09-01 what happened"),
            ({"since": "2026-09-01"}, "what happened"),
        )

    def test_before_prefix(self) -> None:
        self.assertEqual(
            split_rag_filter_prefix("before:2026-09-15 what happened"),
            ({"before": "2026-09-15"}, "what happened"),
        )

    def test_tag_and_date_combine_in_either_order(self) -> None:
        self.assertEqual(
            split_rag_filter_prefix("tag:work since:2026-09-01 what's due"),
            ({"tag": "work", "since": "2026-09-01"}, "what's due"),
        )
        self.assertEqual(
            split_rag_filter_prefix("since:2026-09-01 tag:work what's due"),
            ({"since": "2026-09-01", "tag": "work"}, "what's due"),
        )

    def test_since_and_before_together_form_a_range(self) -> None:
        self.assertEqual(
            split_rag_filter_prefix("since:2026-09-01 before:2026-09-15 what happened"),
            ({"since": "2026-09-01", "before": "2026-09-15"}, "what happened"),
        )

    def test_invalid_date_falls_through_untouched(self) -> None:
        self.assertEqual(
            split_rag_filter_prefix("since:not-a-date what happened"),
            ({}, "since:not-a-date what happened"),
        )

    def test_repeated_key_stops_the_prefix_run(self) -> None:
        # A second since: is treated as part of the question, not an override.
        self.assertEqual(
            split_rag_filter_prefix("since:2026-09-01 since:2026-09-05 what happened"),
            ({"since": "2026-09-01"}, "since:2026-09-05 what happened"),
        )


class FakeEmbeddings:
    def embed(self, text: str) -> list[float]:
        return [0.1]


class FakeQdrant:
    def __init__(self, hits_by_collection: dict) -> None:
        self.hits_by_collection = hits_by_collection
        self.searched: list[str] = []
        self.calls_by_collection: dict = {}

    def search(self, collection, vector, limit=None, filters=None, since=None, before=None):
        self.searched.append(collection)
        self.calls_by_collection[collection] = {"filters": filters, "since": since, "before": before}
        return self.hits_by_collection.get(collection, [])

    def scroll_by_file_name(self, collection, file_name, limit=12):
        return []


class FakeLlm:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def chat(self, prompt: str, *, max_tokens: int | None = None) -> str:
        self.prompts.append(prompt)
        return "answer-from-context"


def _hit(text: str, score: float = 0.9, **payload) -> dict:
    return {"score": score, "payload": {"text": text, **payload}}


class CategoryRagRetrieveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = build_settings(
            category_registry_path=Path(self.tmp.name) / "categories.json",
            tracker_collection="tracker_coll",
            knowledge_collection="knowledge_coll",
            category_collection_prefix="oc_cat_",
        )
        self.entry = categories.upsert_registry_entry(self.settings, "工作筆記")

    def _skill(self, qdrant) -> RagRetrieveSkill:
        return RagRetrieveSkill(self.settings, {}, FakeEmbeddings(), qdrant, FakeLlm())

    def test_category_query_only_searches_that_collection(self) -> None:
        qdrant = FakeQdrant({self.entry["collection"]: [_hit("category-only content")]})
        skill = self._skill(qdrant)

        result = skill.run("/rag #工作筆記 重點是什麼")

        self.assertEqual(result.answer, "answer-from-context")
        self.assertEqual(qdrant.searched, [self.entry["collection"]])
        self.assertNotIn("tracker_coll", qdrant.searched)
        self.assertNotIn("knowledge_coll", qdrant.searched)

    def test_unknown_category_lists_existing_and_does_not_search(self) -> None:
        qdrant = FakeQdrant({})
        skill = self._skill(qdrant)
        result = skill.run("/rag #尚未建立 問題")
        self.assertIn("No category named", result.answer)
        self.assertIn("工作筆記", result.answer)  # the one registered in setUp
        self.assertEqual(qdrant.searched, [])

    def test_known_but_empty_category_reports_no_content(self) -> None:
        qdrant = FakeQdrant({})  # entry collection has no hits
        skill = self._skill(qdrant)
        result = skill.run("/rag #工作筆記 問題")
        self.assertIn("no indexed content yet", result.answer)
        self.assertEqual(qdrant.searched, [self.entry["collection"]])

    def test_fullwidth_hash_query_hits_the_category(self) -> None:
        qdrant = FakeQdrant({self.entry["collection"]: [_hit("content")]})
        skill = self._skill(qdrant)
        result = skill.run("/rag ＃工作筆記 重點")
        self.assertEqual(result.answer, "answer-from-context")
        self.assertEqual(qdrant.searched, [self.entry["collection"]])

    def test_default_query_keeps_legacy_two_collection_behaviour(self) -> None:
        qdrant = FakeQdrant(
            {
                "tracker_coll": [_hit("tracker content")],
                "knowledge_coll": [_hit("knowledge content")],
            }
        )
        skill = self._skill(qdrant)
        skill.run("/rag 一般問題")
        self.assertEqual(set(qdrant.searched), {"tracker_coll", "knowledge_coll"})

    def test_tag_prefix_only_searches_tracker_with_filter_applied(self) -> None:
        qdrant = FakeQdrant({"tracker_coll": [_hit("work item")]})
        skill = self._skill(qdrant)
        result = skill.run("/rag tag:work what's due")
        self.assertEqual(result.answer, "answer-from-context")
        self.assertEqual(qdrant.searched, ["tracker_coll"])
        self.assertEqual(qdrant.calls_by_collection["tracker_coll"]["filters"], {"tags": "work"})

    def test_tag_prefix_skips_knowledge_collection_entirely(self) -> None:
        qdrant = FakeQdrant(
            {"tracker_coll": [_hit("work item")], "knowledge_coll": [_hit("unrelated doc")]}
        )
        skill = self._skill(qdrant)
        skill.run("/rag tag:work what's due")
        self.assertNotIn("knowledge_coll", qdrant.searched)

    def test_query_without_tag_prefix_passes_no_filter(self) -> None:
        qdrant = FakeQdrant({"tracker_coll": [_hit("t")], "knowledge_coll": [_hit("k")]})
        skill = self._skill(qdrant)
        skill.run("/rag 一般問題")
        call = qdrant.calls_by_collection["tracker_coll"]
        self.assertIsNone(call["filters"])
        self.assertIsNone(call["since"])
        self.assertIsNone(call["before"])

    def test_tag_prefix_with_no_hits_mentions_the_tag(self) -> None:
        qdrant = FakeQdrant({})
        skill = self._skill(qdrant)
        result = skill.run("/rag tag:missing anything")
        self.assertIn('tagged "missing"', result.answer)

    def test_since_prefix_applies_to_both_tracker_and_knowledge(self) -> None:
        qdrant = FakeQdrant(
            {"tracker_coll": [_hit("tracker item")], "knowledge_coll": [_hit("knowledge item")]}
        )
        skill = self._skill(qdrant)
        result = skill.run("/rag since:2026-09-01 what happened")
        self.assertEqual(result.answer, "answer-from-context")
        self.assertEqual(set(qdrant.searched), {"tracker_coll", "knowledge_coll"})
        expected_epoch = 1788220800  # 2026-09-01T00:00:00Z
        self.assertEqual(qdrant.calls_by_collection["tracker_coll"]["since"], expected_epoch)
        self.assertEqual(qdrant.calls_by_collection["knowledge_coll"]["since"], expected_epoch)
        self.assertIsNone(qdrant.calls_by_collection["tracker_coll"]["before"])

    def test_before_prefix_sets_upper_bound(self) -> None:
        qdrant = FakeQdrant({"tracker_coll": [_hit("t")], "knowledge_coll": [_hit("k")]})
        skill = self._skill(qdrant)
        skill.run("/rag before:2026-09-15 what happened")
        expected_epoch = 1789430400  # 2026-09-15T00:00:00Z
        self.assertEqual(qdrant.calls_by_collection["tracker_coll"]["before"], expected_epoch)
        self.assertIsNone(qdrant.calls_by_collection["tracker_coll"]["since"])

    def test_tag_and_date_filters_combine(self) -> None:
        qdrant = FakeQdrant({"tracker_coll": [_hit("work item")]})
        skill = self._skill(qdrant)
        skill.run("/rag tag:work since:2026-09-01 what's due")
        self.assertEqual(qdrant.searched, ["tracker_coll"])  # tag still skips knowledge
        call = qdrant.calls_by_collection["tracker_coll"]
        self.assertEqual(call["filters"], {"tags": "work"})
        self.assertEqual(call["since"], 1788220800)

    def test_invalid_date_value_is_treated_as_part_of_the_question(self) -> None:
        qdrant = FakeQdrant({"tracker_coll": [_hit("t")], "knowledge_coll": [_hit("k")]})
        skill = self._skill(qdrant)
        skill.run("/rag since:not-a-date what happened")
        call = qdrant.calls_by_collection["tracker_coll"]
        self.assertIsNone(call["since"])
        self.assertIsNone(call["filters"])

    def test_date_range_with_no_hits_mentions_the_range(self) -> None:
        qdrant = FakeQdrant({})
        skill = self._skill(qdrant)
        result = skill.run("/rag since:2026-09-01 anything")
        self.assertIn("in that date range", result.answer)

    def test_tag_alone_with_no_question_asks_for_one(self) -> None:
        qdrant = FakeQdrant({})
        skill = self._skill(qdrant)
        result = skill.run("/rag tag:work")
        self.assertIn("Add the question to look up after /rag.", result.answer)
        self.assertEqual(qdrant.searched, [])

    def test_all_categories_fans_out_over_registry(self) -> None:
        second = categories.upsert_registry_entry(self.settings, "讀書心得")
        qdrant = FakeQdrant(
            {
                self.entry["collection"]: [_hit("work note hit")],
                second["collection"]: [_hit("reading note hit")],
            }
        )
        skill = self._skill(qdrant)
        result = skill.run("/rag #all 有什麼資料")
        self.assertEqual(result.answer, "answer-from-context")
        self.assertEqual(
            set(qdrant.searched), {self.entry["collection"], second["collection"]}
        )

    def test_answer_appends_source_document_names(self) -> None:
        qdrant = FakeQdrant(
            {
                self.entry["collection"]: [
                    _hit("chunk one", original_file_name="第一季報告.pdf"),
                    _hit("chunk two", original_file_name="第一季報告.pdf"),
                    _hit("chunk three", doc_title="Arm V3 Notes"),
                ]
            }
        )
        skill = self._skill(qdrant)
        result = skill.run("/rag #工作筆記 重點")
        self.assertIn("Sources:", result.answer)
        self.assertIn("第一季報告.pdf", result.answer)
        self.assertIn("Arm V3 Notes", result.answer)
        # de-duplicated
        self.assertEqual(result.answer.count("第一季報告.pdf"), 1)

    def test_no_footer_when_hits_have_no_source(self) -> None:
        qdrant = FakeQdrant({self.entry["collection"]: [_hit("content")]})
        skill = self._skill(qdrant)
        result = skill.run("/rag #工作筆記 重點")
        self.assertEqual(result.answer, "answer-from-context")

    def test_category_disabled_falls_back_to_default(self) -> None:
        self.settings = build_settings(
            category_registry_path=self.settings.category_registry_path,
            category_rag_enabled=False,
            tracker_collection="tracker_coll",
            knowledge_collection="knowledge_coll",
        )
        qdrant = FakeQdrant({"tracker_coll": [_hit("t")], "knowledge_coll": [_hit("k")]})
        skill = self._skill(qdrant)
        skill.run("/rag #工作筆記 問題")
        self.assertEqual(set(qdrant.searched), {"tracker_coll", "knowledge_coll"})


if __name__ == "__main__":
    unittest.main()
