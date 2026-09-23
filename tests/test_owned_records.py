import unittest
from unittest.mock import MagicMock

from openclaw_runtime.owned_records import read_owned_points, write_owned_point


class WriteOwnedPointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.qdrant = MagicMock()
        self.qdrant.upsert_text.return_value = "point-id-1"

    def test_writes_with_owner_in_payload(self) -> None:
        point_id = write_owned_point(
            self.qdrant, "coll", "111", "some text", [0.1, 0.2], {"tag": "eng_wk1"}
        )
        self.assertEqual(point_id, "point-id-1")
        args, kwargs = self.qdrant.upsert_text.call_args
        collection, text, vector, payload = args
        self.assertEqual(collection, "coll")
        self.assertEqual(payload["owner"], "111")
        self.assertEqual(payload["tag"], "eng_wk1")

    def test_missing_owner_raises_and_never_calls_qdrant(self) -> None:
        with self.assertRaises(ValueError):
            write_owned_point(self.qdrant, "coll", "", "text", [0.1], {})
        self.qdrant.upsert_text.assert_not_called()

    def test_none_owner_raises(self) -> None:
        with self.assertRaises(ValueError):
            write_owned_point(self.qdrant, "coll", None, "text", [0.1], {})
        self.qdrant.upsert_text.assert_not_called()


class ReadOwnedPointsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.qdrant = MagicMock()
        self.qdrant.scroll_by_filters.return_value = [{"id": "p1"}]

    def test_reads_with_owner_filter(self) -> None:
        result = read_owned_points(self.qdrant, "coll", "222", {"tag": "eng_wk1"})
        self.assertEqual(result, [{"id": "p1"}])
        args, kwargs = self.qdrant.scroll_by_filters.call_args
        collection, filters = args
        self.assertEqual(collection, "coll")
        self.assertEqual(filters, {"owner": "222", "tag": "eng_wk1"})

    def test_missing_owner_raises_and_never_calls_qdrant(self) -> None:
        with self.assertRaises(ValueError):
            read_owned_points(self.qdrant, "coll", "")
        self.qdrant.scroll_by_filters.assert_not_called()

    def test_missing_owner_raises_even_with_extra_filters_supplied(self) -> None:
        """The most dangerous case: a caller supplies real filters (so the
        call looks intentional) but forgets owner -- must still refuse,
        not silently return every family member's matching records."""
        with self.assertRaises(ValueError):
            read_owned_points(self.qdrant, "coll", "", {"tag": "eng_wk1", "kind": "daily_task"})
        self.qdrant.scroll_by_filters.assert_not_called()


if __name__ == "__main__":
    unittest.main()
