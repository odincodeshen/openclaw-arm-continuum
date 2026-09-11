import unittest
from unittest.mock import patch

from openclaw_runtime.qdrant_client import QdrantClient
from tests.support import build_settings


class QdrantClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = QdrantClient(build_settings(qdrant_base_url="http://q", request_timeout=5))

    @patch("openclaw_runtime.qdrant_client.request_json")
    def test_upsert_text_uses_caller_supplied_point_id(self, request_json) -> None:
        request_json.return_value = {}
        returned = self.client.upsert_text(
            "coll", "hello", [0.1], {"source": "telegram", "kind": "tracker_memory"}, point_id="fixed-id"
        )
        self.assertEqual(returned, "fixed-id")
        method, url, payload = request_json.call_args.args
        self.assertEqual(method, "PUT")
        self.assertIn("/collections/coll/points", url)
        self.assertEqual(payload["points"][0]["id"], "fixed-id")

    @patch("openclaw_runtime.qdrant_client.request_json")
    def test_upsert_text_generates_id_when_not_given(self, request_json) -> None:
        request_json.return_value = {}
        returned = self.client.upsert_text("coll", "hello", [0.1], {})
        self.assertTrue(returned)
        payload = request_json.call_args.args[2]
        self.assertEqual(payload["points"][0]["id"], returned)

    @patch("openclaw_runtime.qdrant_client.request_json")
    def test_scroll_by_filters_sends_and_of_all_fields(self, request_json) -> None:
        request_json.return_value = {"result": {"points": [{"id": "p1", "payload": {}}], "next_page_offset": None}}
        hits = self.client.scroll_by_filters("coll", {"kind": "tracker_memory", "status": "active"}, limit=10)
        self.assertEqual(len(hits), 1)
        method, url, payload = request_json.call_args.args
        self.assertEqual(method, "POST")
        self.assertIn("/collections/coll/points/scroll", url)
        must = payload["filter"]["must"]
        self.assertIn({"key": "kind", "match": {"value": "tracker_memory"}}, must)
        self.assertIn({"key": "status", "match": {"value": "active"}}, must)

    @patch("openclaw_runtime.qdrant_client.request_json")
    def test_scroll_by_filters_paginates_until_offset_is_none(self, request_json) -> None:
        request_json.side_effect = [
            {"result": {"points": [{"id": "p1", "payload": {}}], "next_page_offset": "cursor"}},
            {"result": {"points": [{"id": "p2", "payload": {}}], "next_page_offset": None}},
        ]
        hits = self.client.scroll_by_filters("coll", {"kind": "x"}, limit=10)
        self.assertEqual([h["id"] for h in hits], ["p1", "p2"])
        self.assertEqual(request_json.call_count, 2)

    @patch("openclaw_runtime.qdrant_client.request_json")
    def test_scroll_by_filters_respects_limit(self, request_json) -> None:
        request_json.return_value = {
            "result": {
                "points": [{"id": f"p{i}", "payload": {}} for i in range(5)],
                "next_page_offset": None,
            }
        }
        hits = self.client.scroll_by_filters("coll", {"kind": "x"}, limit=2)
        self.assertEqual(len(hits), 2)

    @patch("openclaw_runtime.qdrant_client.request_json")
    def test_set_payload_posts_to_points_payload_endpoint(self, request_json) -> None:
        request_json.return_value = {}
        self.client.set_payload("coll", "pid-1", {"status": "done"})
        method, url, payload = request_json.call_args.args
        self.assertEqual(method, "POST")
        self.assertIn("/collections/coll/points/payload", url)
        self.assertEqual(payload["points"], ["pid-1"])
        self.assertEqual(payload["payload"], {"status": "done"})

    @patch("openclaw_runtime.qdrant_client.request_json")
    def test_delete_points_posts_ids(self, request_json) -> None:
        request_json.return_value = {}
        self.client.delete_points("coll", ["a", "b"])
        method, url, payload = request_json.call_args.args
        self.assertEqual(method, "POST")
        self.assertIn("/collections/coll/points/delete", url)
        self.assertEqual(payload["points"], ["a", "b"])

    @patch("openclaw_runtime.qdrant_client.request_json")
    def test_delete_points_with_empty_list_is_a_noop(self, request_json) -> None:
        self.client.delete_points("coll", [])
        request_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
