import tempfile
import unittest
from pathlib import Path

from openclaw_runtime import categories
from openclaw_runtime.skills.memory import RagRetrieveSkill, split_category_prefix

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


class FakeEmbeddings:
    def embed(self, text: str) -> list[float]:
        return [0.1]


class FakeQdrant:
    def __init__(self, hits_by_collection: dict) -> None:
        self.hits_by_collection = hits_by_collection
        self.searched: list[str] = []

    def search(self, collection, vector, limit=None):
        self.searched.append(collection)
        return self.hits_by_collection.get(collection, [])

    def scroll_by_file_name(self, collection, file_name, limit=12):
        return []


class FakeLlm:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def chat(self, prompt: str, *, max_tokens: int | None = None) -> str:
        self.prompts.append(prompt)
        return "answer-from-context"


def _hit(text: str, score: float = 0.9) -> dict:
    return {"score": score, "payload": {"text": text}}


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
        self.assertIn("找不到類別", result.answer)
        self.assertIn("工作筆記", result.answer)  # the one registered in setUp
        self.assertEqual(qdrant.searched, [])

    def test_known_but_empty_category_reports_no_content(self) -> None:
        qdrant = FakeQdrant({})  # entry collection has no hits
        skill = self._skill(qdrant)
        result = skill.run("/rag #工作筆記 問題")
        self.assertIn("目前還沒有可檢索的內容", result.answer)
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
