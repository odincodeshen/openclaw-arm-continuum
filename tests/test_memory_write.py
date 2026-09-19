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

        def field_matches(actual, expected) -> bool:
            # Mirrors real Qdrant: a match against a list-valued payload
            # field means "the list contains this value".
            if isinstance(actual, list):
                return expected in actual
            return actual == expected

        def matches(point: dict) -> bool:
            payload = point["payload"]
            return all(field_matches(payload.get(key), value) for key, value in filters.items())

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

    def test_list_filters_by_tag(self) -> None:
        self.skill.run("/mem work task tag:work")
        self.skill.run("/mem home task tag:home")
        result = self.skill.run("/mem list tag:work")
        self.assertIn('Active memory tagged "work" (1):', result.answer)
        self.assertIn("work task", result.answer)
        self.assertNotIn("home task", result.answer)

    def test_list_tag_filter_with_no_matches(self) -> None:
        self.skill.run("/mem home task tag:home")
        result = self.skill.run("/mem list tag:missing")
        self.assertIn('No active memory items tagged "missing".', result.answer)

    def test_list_done_with_tag_filter(self) -> None:
        self.skill.run("/mem work task tag:work")
        short_id = self._first_short_id()
        self.skill.run(f"/mem done {short_id}")
        result = self.skill.run("/mem list done tag:work")
        self.assertIn('Completed memory tagged "work" (1):', result.answer)
        self.assertIn("work task", result.answer)

    def test_list_tag_filter_ignores_items_without_that_tag(self) -> None:
        self.skill.run("/mem untagged task")
        result = self.skill.run("/mem list tag:work")
        self.assertIn("No active memory items", result.answer)

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


class MemorySnoozeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = build_settings(tracker_collection="tracker_coll")
        self.qdrant = FakeQdrant()
        self.skill = MemoryWriteSkill(self.settings, {}, FakeEmbeddings(), self.qdrant)

    def _first_short_id(self) -> str:
        payload = next(iter(self.qdrant.collections["tracker_coll"].values()))["payload"]
        return payload["short_id"]

    def _payload(self, short_id: str) -> dict:
        for point in self.qdrant.collections["tracker_coll"].values():
            if point["payload"]["short_id"] == short_id:
                return point["payload"]
        raise AssertionError(f"no point with short_id {short_id}")

    def test_snooze_relative_days(self) -> None:
        self.skill.run("/mem renew passport due:2026-01-01")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem snooze {short_id} 3d")
        expected = (date.today() + timedelta(days=3)).isoformat()
        self.assertIn(f"Snoozed #{short_id} to {expected}", result.answer)
        payload = self._payload(short_id)
        self.assertEqual(payload["due"], expected)
        self.assertEqual(payload["status"], "active")

    def test_snooze_relative_weeks(self) -> None:
        self.skill.run("/mem book flight")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem snooze {short_id} 1w")
        expected = (date.today() + timedelta(days=7)).isoformat()
        self.assertIn(expected, result.answer)
        payload = self._payload(short_id)
        self.assertEqual(payload["due"], expected)

    def test_snooze_absolute_date(self) -> None:
        self.skill.run("/mem call dentist")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem snooze {short_id} 2026-12-25")
        self.assertIn("Snoozed", result.answer)
        payload = self._payload(short_id)
        self.assertEqual(payload["due"], "2026-12-25")

    def test_snooze_reactivates_a_done_item(self) -> None:
        self.skill.run("/mem finish report")
        short_id = self._first_short_id()
        self.skill.run(f"/mem done {short_id}")
        result = self.skill.run(f"/mem snooze {short_id} 3d")
        self.assertIn("Snoozed", result.answer)
        payload = self._payload(short_id)
        self.assertEqual(payload["status"], "active")

    def test_snooze_undated_item_gains_a_due_date(self) -> None:
        self.skill.run("/mem no due task")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem snooze {short_id} 2d")
        self.assertIn("Snoozed", result.answer)
        payload = self._payload(short_id)
        self.assertIn("due", payload)

    def test_snooze_unknown_id(self) -> None:
        result = self.skill.run("/mem snooze deadbeef 3d")
        self.assertIn("No memory item", result.answer)

    def test_snooze_invalid_target_is_rejected(self) -> None:
        self.skill.run("/mem call dentist")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem snooze {short_id} tomorrow")
        self.assertIn("Could not parse", result.answer)

    def test_snooze_missing_args_shows_usage(self) -> None:
        result = self.skill.run("/mem snooze")
        self.assertIn("Usage: /mem snooze", result.answer)

    def test_snooze_missing_amount_shows_usage(self) -> None:
        self.skill.run("/mem call dentist")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem snooze {short_id}")
        self.assertIn("Usage: /mem snooze", result.answer)

    def test_snooze_includes_item_text_in_confirmation(self) -> None:
        self.skill.run("/mem renew passport")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem snooze {short_id} 3d")
        self.assertIn("renew passport", result.answer)


class MemoryEditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = build_settings(tracker_collection="tracker_coll")
        self.qdrant = FakeQdrant()
        self.skill = MemoryWriteSkill(self.settings, {}, FakeEmbeddings(), self.qdrant)

    def _first_short_id(self) -> str:
        payload = next(iter(self.qdrant.collections["tracker_coll"].values()))["payload"]
        return payload["short_id"]

    def _payload(self, short_id: str) -> dict:
        for point in self.qdrant.collections["tracker_coll"].values():
            if point["payload"]["short_id"] == short_id:
                return point["payload"]
        raise AssertionError(f"no point with short_id {short_id}")

    def test_edit_replaces_text_and_keeps_same_short_id(self) -> None:
        self.skill.run("/mem buy milk")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem edit {short_id} buy oat milk instead")
        self.assertIn(f"Updated #{short_id}: buy oat milk instead", result.answer)
        payload = self._payload(short_id)
        self.assertEqual(payload["text"], "buy oat milk instead")
        self.assertEqual(len(self.qdrant.collections["tracker_coll"]), 1)

    def test_edit_re_embeds_the_new_text(self) -> None:
        self.skill.run("/mem buy milk")
        short_id = self._first_short_id()
        self.skill.run(f"/mem edit {short_id} a much longer replacement sentence")
        point = next(iter(self.qdrant.collections["tracker_coll"].values()))
        self.assertEqual(point["vector"], FakeEmbeddings().embed("a much longer replacement sentence"))

    def test_edit_preserves_status_and_created_at(self) -> None:
        self.skill.run("/mem finish report")
        short_id = self._first_short_id()
        self.skill.run(f"/mem done {short_id}")
        created_at = self._payload(short_id)["created_at"]

        self.skill.run(f"/mem edit {short_id} finish the quarterly report")
        payload = self._payload(short_id)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["created_at"], created_at)

    def test_edit_without_metadata_keeps_existing_due_and_tags(self) -> None:
        self.skill.run("/mem renew passport due:2026-12-01 tag:admin")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem edit {short_id} renew passport and visa")
        payload = self._payload(short_id)
        self.assertEqual(payload["due"], "2026-12-01")
        self.assertEqual(payload["tags"], ["admin"])
        self.assertIn("due 2026-12-01", result.answer)
        self.assertIn("tags: admin", result.answer)

    def test_edit_with_new_metadata_overrides_existing(self) -> None:
        self.skill.run("/mem renew passport due:2026-12-01 tag:admin")
        short_id = self._first_short_id()
        self.skill.run(f"/mem edit {short_id} renew passport due:2027-01-15 tag:urgent")
        payload = self._payload(short_id)
        self.assertEqual(payload["due"], "2027-01-15")
        self.assertEqual(payload["tags"], ["urgent"])

    def test_edit_unknown_id(self) -> None:
        result = self.skill.run("/mem edit deadbeef new text")
        self.assertIn("No memory item", result.answer)

    def test_edit_missing_args_shows_usage(self) -> None:
        result = self.skill.run("/mem edit")
        self.assertIn("Usage: /mem edit", result.answer)

    def test_edit_missing_new_text_shows_usage(self) -> None:
        self.skill.run("/mem buy milk")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem edit {short_id}")
        self.assertIn("Usage: /mem edit", result.answer)

    def test_edit_metadata_only_is_rejected(self) -> None:
        self.skill.run("/mem buy milk")
        short_id = self._first_short_id()
        result = self.skill.run(f"/mem edit {short_id} due:2026-09-20")
        self.assertIn("not just", result.answer)
        payload = self._payload(short_id)
        self.assertEqual(payload["text"], "buy milk")


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

    def test_digest_scoped_to_tag_only_reports_that_tag(self) -> None:
        self._seed("work overdue thing", due="2000-01-01", tags=["work"])
        self._seed("home overdue thing", due="2000-01-01", tags=["home"])
        result = self.skill.run("/mem digest tag:work")
        self.assertIn("work overdue thing", result.answer)
        self.assertNotIn("home overdue thing", result.answer)

    def test_digest_tag_with_nothing_due_is_suppressed_and_mentions_tag(self) -> None:
        self._seed("home overdue thing", due="2000-01-01", tags=["home"])
        result = self.skill.run("/mem digest tag:work")
        self.assertTrue(result.suppress_if_routine)
        self.assertIn('tagged "work"', result.answer)

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


class MemoryArchiveStaleTest(unittest.TestCase):
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

    def test_nothing_stale_is_a_suppressed_no_op(self) -> None:
        self._seed("recent undated item")
        result = self.skill.run("/mem archive-stale")
        self.assertTrue(result.suppress_if_routine)
        self.assertIn("No stale items", result.answer)

    def test_stale_undated_item_is_archived(self) -> None:
        point_id = self._seed("old idea", updated_at=int(time.time()) - 30 * 86400)
        result = self.skill.run("/mem archive-stale")
        self.assertFalse(result.suppress_if_routine)
        self.assertIn("Archived 1 stale item(s):", result.answer)
        self.assertIn("old idea", result.answer)
        payload = self.qdrant.collections["tracker_coll"][point_id]["payload"]
        self.assertEqual(payload["status"], "archived")
        self.assertGreater(payload.get("archived_at", 0), 0)

    def test_item_with_due_is_never_archived_even_if_old(self) -> None:
        point_id = self._seed(
            "has a due date", due="2000-01-01", updated_at=int(time.time()) - 90 * 86400
        )
        self.skill.run("/mem archive-stale")
        payload = self.qdrant.collections["tracker_coll"][point_id]["payload"]
        self.assertEqual(payload["status"], "active")

    def test_recently_touched_undated_item_is_not_archived(self) -> None:
        point_id = self._seed("fresh note")
        self.skill.run("/mem archive-stale")
        payload = self.qdrant.collections["tracker_coll"][point_id]["payload"]
        self.assertEqual(payload["status"], "active")

    def test_already_done_item_is_not_touched(self) -> None:
        point_id = self._seed(
            "finished task", status="done", updated_at=int(time.time()) - 30 * 86400
        )
        self.skill.run("/mem archive-stale")
        payload = self.qdrant.collections["tracker_coll"][point_id]["payload"]
        self.assertEqual(payload["status"], "done")

    def test_archived_item_is_excluded_from_default_list_and_digest(self) -> None:
        self._seed("old idea", updated_at=int(time.time()) - 30 * 86400)
        self.skill.run("/mem archive-stale")

        active_list = self.skill.run("/mem list")
        self.assertIn("No active memory items", active_list.answer)

        digest = self.skill.run("/mem digest")
        self.assertTrue(digest.suppress_if_routine)

    def test_archived_item_appears_in_list_archived(self) -> None:
        self._seed("old idea", updated_at=int(time.time()) - 30 * 86400)
        self.skill.run("/mem archive-stale")

        archived_list = self.skill.run("/mem list archived")
        self.assertIn("Archived memory (1):", archived_list.answer)
        self.assertIn("old idea", archived_list.answer)


if __name__ == "__main__":
    unittest.main()
