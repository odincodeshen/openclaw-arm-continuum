import time
import unittest
from datetime import date, timedelta

from openclaw_runtime.skills.memory import MemoryWriteSkill, parse_memory_metadata

from tests.support import build_settings


class FakeEmbeddings:
    def embed(self, text: str) -> list[float]:
        return [float(len(text))]


class FakeQdrant:
    """In-memory stand-in mirroring the QdrantClient methods MemoryWriteSkill
    uses, including AND-filter scroll semantics."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict]] = {}

    def upsert_text(self, collection, text, vector, metadata, *, point_id=None):
        point_id = point_id or f"gen-{sum(len(c) for c in self.collections.values())}"
        payload = {
            "text": text,
            "source": metadata.get("source", "telegram"),
            "kind": metadata.get("kind", "memory"),
            "created_at": metadata.get("created_at", 0),
        }
        for key, value in metadata.items():
            payload.setdefault(key, value)
        self.collections.setdefault(collection, {})[point_id] = {
            "id": point_id,
            "payload": payload,
            "vector": vector,
        }
        return point_id

    def scroll_by_filters(self, collection, filters, limit=64):
        points = list(self.collections.get(collection, {}).values())

        def matches(point: dict) -> bool:
            payload = point["payload"]
            return all(payload.get(key) == value for key, value in filters.items())

        return [p for p in points if matches(p)][:limit]

    def set_payload(self, collection, point_id, payload):
        self.collections[collection][point_id]["payload"].update(payload)

    def delete_points(self, collection, point_ids):
        for pid in point_ids:
            self.collections.get(collection, {}).pop(pid, None)


class ParseMemoryMetadataTest(unittest.TestCase):
    def test_plain_text_has_no_metadata(self) -> None:
        clean, due, tags = parse_memory_metadata("buy milk")
        self.assertEqual(clean, "buy milk")
        self.assertIsNone(due)
        self.assertEqual(tags, [])

    def test_due_and_tag_are_extracted(self) -> None:
        clean, due, tags = parse_memory_metadata("finish report due:2026-09-20 tag:work")
        self.assertEqual(clean, "finish report")
        self.assertEqual(due, "2026-09-20")
        self.assertEqual(tags, ["work"])

    def test_multiple_tags(self) -> None:
        clean, due, tags = parse_memory_metadata("call mom tag:family tag:urgent")
        self.assertEqual(clean, "call mom")
        self.assertEqual(tags, ["family", "urgent"])

    def test_invalid_due_is_left_in_text(self) -> None:
        clean, due, tags = parse_memory_metadata("buy milk due:tomorrow")
        self.assertIsNone(due)
        self.assertIn("due:tomorrow", clean)

    def test_second_due_token_is_left_in_text(self) -> None:
        clean, due, tags = parse_memory_metadata("x due:2026-01-01 due:2026-02-02")
        self.assertEqual(due, "2026-01-01")
        self.assertIn("due:2026-02-02", clean)

    def test_case_insensitive_and_cjk_content(self) -> None:
        clean, due, tags = parse_memory_metadata("明天要交報告 DUE:2026-09-20 TAG:工作")
        self.assertEqual(clean, "明天要交報告")
        self.assertEqual(due, "2026-09-20")
        self.assertEqual(tags, ["工作"])


class MemoryWriteSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = build_settings(tracker_collection="tracker_coll")
        self.qdrant = FakeQdrant()
        self.skill = MemoryWriteSkill(self.settings, {}, FakeEmbeddings(), self.qdrant)

    def _first_short_id(self) -> str:
        payload = next(iter(self.qdrant.collections["tracker_coll"].values()))["payload"]
        return payload["short_id"]

    def test_write_stores_active_item_with_short_id(self) -> None:
        result = self.skill.run("/mem buy milk")
        self.assertIn("Saved to tracker_coll", result.answer)
        self.assertIn("Memory ID:", result.answer)
        points = list(self.qdrant.collections["tracker_coll"].values())
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["payload"]["status"], "active")
        self.assertEqual(points[0]["payload"]["text"], "buy milk")
        self.assertEqual(points[0]["payload"]["kind"], "tracker_memory")

    def test_write_with_due_and_tag(self) -> None:
        result = self.skill.run("/mem pay rent due:2026-09-20 tag:money")
        payload = next(iter(self.qdrant.collections["tracker_coll"].values()))["payload"]
        self.assertEqual(payload["due"], "2026-09-20")
        self.assertEqual(payload["tags"], ["money"])
        self.assertIn("due 2026-09-20", result.answer)
        self.assertIn("tags: money", result.answer)

    def test_metadata_only_is_rejected(self) -> None:
        result = self.skill.run("/mem due:2026-09-20")
        self.assertIn("not just", result.answer)
        self.assertEqual(self.qdrant.collections.get("tracker_coll", {}), {})

    def test_no_content_shows_usage(self) -> None:
        result = self.skill.run("/mem")
        self.assertIn("/mem list", result.answer)
        self.assertIn("/mem done", result.answer)

    def test_list_empty_says_so(self) -> None:
        result = self.skill.run("/mem list")
        self.assertIn("No active memory items", result.answer)

    def test_list_shows_active_items_sorted_by_due_then_undated_last(self) -> None:
        self.skill.run("/mem b task due:2026-09-25")
        self.skill.run("/mem a task due:2026-09-10")
        self.skill.run("/mem no due task")
        result = self.skill.run("/mem list")
        lines = result.answer.splitlines()
        self.assertIn("Active memory (3):", lines[0])
        self.assertIn("a task", lines[1])
        self.assertIn("b task", lines[2])
        self.assertIn("no due task", lines[3])

    def test_done_removes_item_from_active_list_and_into_done_list(self) -> None:
        self.skill.run("/mem finish thing")
        short_id = self._first_short_id()

        done_result = self.skill.run(f"/mem done {short_id}")
        self.assertIn(f"Marked #{short_id} as done", done_result.answer)

        active = self.skill.run("/mem list")
        self.assertIn("No active memory items", active.answer)

        done_list = self.skill.run("/mem list done")
        self.assertIn(short_id, done_list.answer)

    def test_done_unknown_id(self) -> None:
        result = self.skill.run("/mem done deadbeef")
        self.assertIn("No memory item", result.answer)

    def test_done_missing_id_shows_usage(self) -> None:
        result = self.skill.run("/mem done")
        self.assertIn("Usage: /mem done", result.answer)

    def test_rm_deletes_item(self) -> None:
        self.skill.run("/mem throwaway")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem rm {short_id}")
        self.assertIn(f"Deleted #{short_id}", result.answer)
        self.assertEqual(self.qdrant.collections["tracker_coll"], {})

    def test_delete_is_an_alias_for_rm(self) -> None:
        self.skill.run("/mem throwaway")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem delete {short_id}")
        self.assertIn("Deleted", result.answer)

    def test_rm_unknown_id(self) -> None:
        result = self.skill.run("/mem rm deadbeef")
        self.assertIn("No memory item", result.answer)

    def test_content_merely_prefixed_like_a_subcommand_still_writes(self) -> None:
        # "list "/"done "/"rm " are reserved only as an exact leading token
        # (with the trailing space); "listen to..." must not be swallowed.
        result = self.skill.run("/mem listen to the new podcast episode")
        self.assertIn("Saved to tracker_coll", result.answer)
        payload = next(iter(self.qdrant.collections["tracker_coll"].values()))["payload"]
        self.assertEqual(payload["text"], "listen to the new podcast episode")


class MemoryDigestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = build_settings(
            tracker_collection="tracker_coll",
            mem_digest_due_soon_days=7,
            mem_digest_stale_days=14,
            mem_digest_remind_cooldown_days=7,
        )
        self.qdrant = FakeQdrant()
        self.skill = MemoryWriteSkill(self.settings, {}, FakeEmbeddings(), self.qdrant)

    def _seed(self, text: str, **overrides) -> str:
        point_id = f"seed-{len(self.qdrant.collections.get('tracker_coll', {}))}"
        payload = {
            "text": text,
            "source": "telegram",
            "kind": "tracker_memory",
            "status": "active",
            "short_id": point_id,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
        payload.update(overrides)
        self.qdrant.collections.setdefault("tracker_coll", {})[point_id] = {
            "id": point_id,
            "payload": payload,
            "vector": [0.0],
        }
        return point_id

    def test_empty_is_all_caught_up_and_suppressed(self) -> None:
        result = self.skill.run("/mem digest")
        self.assertTrue(result.suppress_if_routine)
        self.assertIn("all caught up", result.answer)

    def test_overdue_item_is_reported_and_not_suppressed(self) -> None:
        self._seed("renew passport", due="2000-01-01")
        result = self.skill.run("/mem digest")
        self.assertFalse(result.suppress_if_routine)
        self.assertIn("Overdue (1):", result.answer)
        self.assertIn("renew passport", result.answer)

    def test_due_soon_item_is_reported(self) -> None:
        due = (date.today() + timedelta(days=3)).isoformat()
        self._seed("book flight", due=due)
        result = self.skill.run("/mem digest")
        self.assertIn("Due in the next 7 days (1):", result.answer)
        self.assertIn("book flight", result.answer)
        self.assertFalse(result.suppress_if_routine)

    def test_far_future_due_item_is_not_reported_yet(self) -> None:
        due = (date.today() + timedelta(days=60)).isoformat()
        self._seed("future thing", due=due)
        result = self.skill.run("/mem digest")
        self.assertTrue(result.suppress_if_routine)

    def test_stale_undated_item_is_reported_and_cooldown_is_set(self) -> None:
        old = int(time.time()) - 20 * 86400
        point_id = self._seed("clean the garage", updated_at=old)
        result = self.skill.run("/mem digest")
        self.assertFalse(result.suppress_if_routine)
        self.assertIn("Stale", result.answer)
        self.assertIn("clean the garage", result.answer)
        payload = self.qdrant.collections["tracker_coll"][point_id]["payload"]
        self.assertGreater(payload.get("last_reminded_at", 0), 0)

    def test_stale_item_within_cooldown_is_not_repeated(self) -> None:
        old = int(time.time()) - 20 * 86400
        recent_reminder = int(time.time()) - 1 * 86400
        self._seed("clean the garage", updated_at=old, last_reminded_at=recent_reminder)
        result = self.skill.run("/mem digest")
        self.assertTrue(result.suppress_if_routine)

    def test_recently_touched_undated_item_is_not_stale(self) -> None:
        self._seed("ongoing project note")
        result = self.skill.run("/mem digest")
        self.assertTrue(result.suppress_if_routine)

    def test_done_items_are_excluded(self) -> None:
        self._seed("old finished task", due="2000-01-01", status="done")
        result = self.skill.run("/mem digest")
        self.assertTrue(result.suppress_if_routine)

    def test_mixed_sections_are_all_present_in_priority_order(self) -> None:
        self._seed("overdue thing", due="2000-01-01")
        due_soon = (date.today() + timedelta(days=2)).isoformat()
        self._seed("soon thing", due=due_soon)
        self._seed("stale thing", updated_at=int(time.time()) - 30 * 86400)

        result = self.skill.run("/mem digest")
        answer = result.answer
        self.assertFalse(result.suppress_if_routine)
        self.assertLess(answer.index("Overdue"), answer.index("Due in the next"))
        self.assertLess(answer.index("Due in the next"), answer.index("Stale"))


if __name__ == "__main__":
    unittest.main()
