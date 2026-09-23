import unittest
from unittest.mock import MagicMock

from openclaw_runtime.daily_task_tracking import (
    day_tag,
    mark_task_completed,
    mark_task_pushed,
    sweep_incomplete_to_skipped,
)


class DayTagTest(unittest.TestCase):
    def test_format(self) -> None:
        self.assertEqual(day_tag(42, "mon"), "eng_wk42_daymon")


class MarkTaskPushedTest(unittest.TestCase):
    def test_writes_owned_point_with_not_completed_not_skipped(self) -> None:
        qdrant = MagicMock()
        qdrant.upsert_text.return_value = "p1"

        point_id = mark_task_pushed(qdrant, "coll", 42, "mon", "111", [0.1])

        self.assertEqual(point_id, "p1")
        args, kwargs = qdrant.upsert_text.call_args
        collection, text, vector, payload = args
        self.assertEqual(payload["owner"], "111")
        self.assertEqual(payload["tag"], "eng_wk42_daymon")
        self.assertEqual(payload["kind"], "daily_task")
        self.assertFalse(payload["completed"])
        self.assertFalse(payload["skipped"])

    def test_missing_owner_raises(self) -> None:
        qdrant = MagicMock()
        with self.assertRaises(ValueError):
            mark_task_pushed(qdrant, "coll", 42, "mon", "", [0.1])


class MarkTaskCompletedTest(unittest.TestCase):
    def test_flips_matching_point_to_completed(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"id": "p1", "payload": {"completed": False}}]

        result = mark_task_completed(qdrant, "coll", 42, "mon", "111")

        self.assertTrue(result)
        qdrant.set_payload.assert_called_once_with("coll", "p1", {"completed": True})

    def test_no_matching_record_returns_false_and_does_not_write(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []

        result = mark_task_completed(qdrant, "coll", 42, "mon", "111")

        self.assertFalse(result)
        qdrant.set_payload.assert_not_called()

    def test_missing_owner_raises(self) -> None:
        qdrant = MagicMock()
        with self.assertRaises(ValueError):
            mark_task_completed(qdrant, "coll", 42, "mon", "")


class SweepIncompleteToSkippedTest(unittest.TestCase):
    def test_marks_still_incomplete_owners_skipped(self) -> None:
        qdrant = MagicMock()

        def scroll_side_effect(collection, filters, limit=64):
            owner = filters["owner"]
            if owner == "owner-a":
                return [{"id": "pa", "payload": {"completed": False}}]
            return [{"id": "pb", "payload": {"completed": True}}]

        qdrant.scroll_by_filters.side_effect = scroll_side_effect

        swept = sweep_incomplete_to_skipped(qdrant, "coll", 42, "mon", ["owner-a", "owner-b"])

        self.assertEqual(swept, ["owner-a"])
        qdrant.set_payload.assert_called_once_with("coll", "pa", {"skipped": True})

    def test_owner_with_no_task_record_is_left_alone(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []

        swept = sweep_incomplete_to_skipped(qdrant, "coll", 42, "mon", ["owner-a"])

        self.assertEqual(swept, [])
        qdrant.set_payload.assert_not_called()

    def test_empty_owner_list_does_nothing(self) -> None:
        qdrant = MagicMock()
        swept = sweep_incomplete_to_skipped(qdrant, "coll", 42, "mon", [])
        self.assertEqual(swept, [])
        qdrant.scroll_by_filters.assert_not_called()


if __name__ == "__main__":
    unittest.main()
