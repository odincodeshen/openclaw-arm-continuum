import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

_path = Path(__file__).resolve().parent.parent / "scripts" / "qdrant_reembed.py"
_spec = importlib.util.spec_from_file_location("qdrant_reembed", _path)
reembed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reembed)


def job(dims=4):
    settings = SimpleNamespace(qdrant_base_url="http://q:6333", ollama_base_url="http://o:11434", request_timeout=60)
    j = reembed.Reembed(settings, "test-model", dims)
    j.seen = []

    def embed(text):
        j.seen.append(text)
        return [0.1] * dims

    j.embed = embed
    return j


class ReembedPointsTest(unittest.TestCase):
    def test_keyword_vectors_and_payloads_are_kept(self):
        j = job()
        points = [{"id": "a", "vector": {"": [0.5, 0.5], "kw": {"indices": [1], "values": [1.0]}},
                   "payload": {"text": "hello", "kind": "note"}}]
        out = j.reembedded(points)
        self.assertEqual(out[0]["vector"][""], [0.1] * 4)
        self.assertEqual(out[0]["vector"]["kw"], {"indices": [1], "values": [1.0]})
        self.assertEqual(out[0]["payload"], {"text": "hello", "kind": "note"})
        self.assertEqual(j.seen, ["hello"])

    def test_plain_and_named_without_keywords(self):
        j = job()
        out = j.reembedded([{"id": 1, "vector": [0.5, 0.5], "payload": {"text": "x"}},
                            {"id": 2, "vector": {"": [0.5, 0.5]}, "payload": {"kind": "daily_task"}}])
        self.assertEqual(out[0]["vector"], [0.1] * 4)
        self.assertEqual(out[1]["vector"], {"": [0.1] * 4})
        self.assertEqual(j.seen, ["x", "daily_task"])  # a point without text falls back to its kind

    def test_dense_size(self):
        j = job()
        self.assertEqual(j.dense_size({"config": {"params": {"vectors": {"size": 768, "distance": "Cosine"}}}}), 768)
        self.assertIsNone(j.dense_size({"config": {"params": {"vectors": {"a": {"size": 3}}}}}))


if __name__ == "__main__":
    unittest.main()
