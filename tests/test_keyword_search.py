import dataclasses
import unittest
from unittest.mock import patch

from openclaw_runtime import qdrant_client
from openclaw_runtime.keywords import VECTOR_NAME, document_vector, query_vector, term_counts
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.rag_budget import drop_weak_hits, fit_passages, keywords_first
from openclaw_runtime.skills.memory import RagRetrieveSkill
from tests.support import build_settings


def hit(pid, text, score, via=None, source="a.md"):
    h = {"id": pid, "score": score, "payload": {"text": text, "original_file_name": source}}
    if via:
        h["via"] = via
    return h


class KeywordVectorTest(unittest.TestCase):
    def test_terms_cover_codes_numbers_and_chinese(self):
        counts = term_counts("SYStem.Option.WaitReset default is 3 msec; 電池備援電池")
        for term in ("system", "option", "waitreset", "3", "msec", "電池"):
            self.assertIn(term, counts)
        self.assertEqual(counts["電池"], 2)
        self.assertNotIn("is", counts)  # stop word

    def test_document_vector_is_sorted_and_saturates(self):
        v = document_vector("battery " * 50 + "backup")
        self.assertEqual(v["indices"], sorted(v["indices"]))
        self.assertEqual(len(v["indices"]), 2)
        self.assertLess(max(v["values"]), 2.2)  # BM25 tf part tops out at k1 + 1

    def test_query_vector_has_each_term_once(self):
        v = query_vector("battery battery backup")
        self.assertEqual(v["values"], [1.0, 1.0])

    def test_plurals_match_singulars(self):
        self.assertEqual(query_vector("cell"), query_vector("cells"))
        self.assertEqual(query_vector("battery"), query_vector("batteries"))
        self.assertEqual(query_vector("box"), query_vector("boxes"))
        for word in ("analysis", "status", "class", "bus"):  # not plurals: left alone
            self.assertIn(word, term_counts(word))

    def test_empty_text(self):
        self.assertEqual(document_vector("..."), {"indices": [], "values": []})


class QdrantKeywordTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.has_kw = True

        def fake_get(url, timeout=20):
            sparse = {VECTOR_NAME: {"modifier": "idf"}} if self.has_kw else None
            return {"result": {"config": {"params": {"sparse_vectors": sparse}}}}

        def fake_request(method, url, payload=None, timeout=60):
            self.calls.append((method, url, payload))
            if url.endswith("/points/query"):
                return {"result": {"points": [{"id": "p1", "score": 7.5, "payload": {"text": "x"}}]}}
            return {"result": {}}

        for name, value in (("get_json", fake_get), ("request_json", fake_request)):
            p = patch.object(qdrant_client, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.client = QdrantClient(build_settings())

    def test_upsert_adds_keyword_vector_when_collection_has_it(self):
        self.client.upsert_text("kb", "battery backup", [0.1, 0.2], {})
        vector = self.calls[-1][2]["points"][0]["vector"]
        self.assertEqual(set(vector), {"", VECTOR_NAME})
        self.assertEqual(vector[""], [0.1, 0.2])

    def test_upsert_plain_vector_on_older_collection(self):
        self.has_kw = False
        self.client.upsert_text("old", "battery backup", [0.1, 0.2], {})
        self.assertEqual(self.calls[-1][2]["points"][0]["vector"], [0.1, 0.2])

    def test_keyword_search_marks_hits_and_passes_filters(self):
        hits = self.client.keyword_search("kb", "battery backup", limit=3, filters={"tags": "x"}, since=10)
        self.assertEqual(hits[0]["via"], "keywords")
        method, url, payload = self.calls[-1]
        self.assertTrue(url.endswith("/collections/kb/points/query"))
        self.assertEqual(payload["using"], VECTOR_NAME)
        self.assertEqual(payload["limit"], 3)
        self.assertEqual(len(payload["filter"]["must"]), 2)

    def test_keyword_search_skips_collections_without_keywords(self):
        self.has_kw = False
        self.assertEqual(self.client.keyword_search("old", "battery"), [])
        self.assertEqual(self.calls, [])

    def test_new_collections_get_the_keyword_vector(self):
        with patch.object(qdrant_client, "get_json", lambda url, timeout=20: {"result": {"collections": []}}):
            self.client.ensure_collection("fresh")
        self.assertIn("sparse_vectors", self.calls[-1][2])
        self.assertTrue(self.client.has_keywords("fresh"))


class KeywordsFirstTest(unittest.TestCase):
    def sections(self):
        return [
            ("filename_match", [hit("f", "named file", 0.0)]),
            ("kb", [hit("k1", "kw one", 9.0, "keywords"), hit("v1", "vec one", 0.80), hit("v2", "vec two", 0.78)]),
            ("category:x", [hit("k2", "kw two", 12.0, "keywords"), hit("k1", "kw one again", 3.0, "keywords"),
                            hit("v3", "vec three", 0.60)]),
        ]

    def test_keywords_then_vectors_with_ranks(self):
        out = keywords_first(self.sections(), keyword_hits=2, vector_hits=1)
        kept = {h["id"]: h.get("rank") for _, hits in out for h in hits}
        self.assertEqual(kept, {"f": None, "k2": 0, "k1": 1, "v1": 2})

    def test_keyword_hit_keeps_its_vector_place(self):
        # the best vector match, also a weak keyword hit, still takes a vector place
        sections = [("kb", [hit("k1", "kw one", 9.0, "keywords"), hit("k2", "kw two", 4.0, "keywords"),
                            {**hit("k3", "best vector", 1.0, "keywords"), "vector_score": 0.9},
                            hit("v1", "vec one", 0.8)])]
        out = keywords_first(sections, keyword_hits=2, vector_hits=1)
        self.assertEqual({h["id"]: h["rank"] for _, hits in out for h in hits}, {"k1": 0, "k2": 1, "k3": 2})

    def test_unchanged_without_keyword_hits(self):
        plain = [("kb", [hit("v1", "a", 0.8)])]
        self.assertIs(keywords_first(plain, keyword_hits=4, vector_hits=2), plain)

    def test_margin_leaves_keyword_hits_alone(self):
        out = drop_weak_hits(self.sections(), 0.05)
        ids = [h["id"] for _, hits in out for h in hits]
        self.assertEqual(ids, ["f", "k1", "v1", "v2", "k2", "k1"])  # v3 (0.60) dropped

    def test_fit_passages_keeps_the_chosen_order(self):
        chosen = keywords_first(self.sections(), keyword_hits=2, vector_hits=1)
        block = "word " * 40
        chosen = [(label, [{**h, "payload": {**h["payload"], "text": block + h["id"]}} for h in hits])
                  for label, hits in chosen]
        # each passage costs ~87 tokens: the named file and the best keyword hit fit in 200
        out = fit_passages(chosen, "word", context_tokens=200, passage_tokens=0)
        self.assertEqual([h["id"] for _, hits in out for h in hits], ["f", "k2"])


class RagSearchMergeTest(unittest.TestCase):
    class FakeQdrant:
        def search(self, collection, vector, limit=5, **kwargs):
            return [hit("v1", "vector one", 0.8), hit("k1", "shared", 0.7)]

        def keyword_search(self, collection, text, limit=5, **kwargs):
            return [hit("k1", "shared", 5.0, "keywords")]

    def test_keyword_hits_first_without_duplicates(self):
        settings = dataclasses.replace(build_settings(), rag_keyword_search=True)
        skill = RagRetrieveSkill(settings, {}, None, self.FakeQdrant(), None)
        hits = skill._search("kb", [0.1], None, query="shared")
        self.assertEqual([(h["id"], h.get("via")) for h in hits], [("k1", "keywords"), ("v1", None)])
        self.assertEqual(hits[0]["vector_score"], 0.7)  # k1 was a vector hit too
        self.assertNotIn("vector_score", hits[1])

    def test_off_by_default(self):
        skill = RagRetrieveSkill(build_settings(), {}, None, self.FakeQdrant(), None)
        self.assertEqual([h["id"] for h in skill._search("kb", [0.1], None, query="shared")], ["v1", "k1"])


if __name__ == "__main__":
    unittest.main()
