import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from openclaw_runtime.english_bot_scheduler import (
    clear_prepared,
    deliver_outbox,
    mark_prepare_attempted,
    prepare_todays_push,
    prepared_for,
    should_prepare_today,
    store_prepared,
)

DAY = datetime(2026, 9, 29, 4, 0)  # a Tuesday


class ShouldPrepareTodayTest(unittest.TestCase):
    def test_inside_the_window_once_a_day(self) -> None:
        self.assertTrue(should_prepare_today(DAY, "04:00", {}))
        self.assertTrue(should_prepare_today(DAY.replace(hour=5, minute=30), "04:00", {}))
        self.assertFalse(should_prepare_today(DAY.replace(hour=5, minute=31), "04:00", {}))
        self.assertFalse(should_prepare_today(DAY.replace(hour=3, minute=59), "04:00", {}))

    def test_attempted_once_even_if_it_failed(self) -> None:
        state: dict = {}
        mark_prepare_attempted(DAY, state)
        self.assertFalse(should_prepare_today(DAY.replace(minute=10), "04:00", state))

    def test_disabled_without_a_prepare_time(self) -> None:
        self.assertFalse(should_prepare_today(DAY, "", {}))


class PreparedStoreTest(unittest.TestCase):
    def test_only_todays_outbox_for_the_same_day_code_is_used(self) -> None:
        state: dict = {}
        store_prepared(DAY, "tue", [{"type": "message", "owner": "a", "text": "hi"}], state)
        later = DAY.replace(hour=7, minute=15)
        self.assertEqual(prepared_for(later, "tue", state), [{"type": "message", "owner": "a", "text": "hi"}])
        self.assertIsNone(prepared_for(later, "wed", state))
        self.assertIsNone(prepared_for(later.replace(day=30), "tue", state))
        clear_prepared(state)
        self.assertIsNone(prepared_for(later, "tue", state))

    def test_an_empty_outbox_still_counts_as_prepared(self) -> None:
        # e.g. Monday with no unused episode: nothing to send, and nothing to regenerate live
        state: dict = {}
        store_prepared(DAY, "mon", [], state)
        self.assertEqual(prepared_for(DAY.replace(hour=7), "mon", state), [])


class PrepareAndDeliverTest(unittest.TestCase):
    def test_everything_is_captured_then_replayed_in_order(self) -> None:
        def fake_push(*, send_message, send_audio, send_report, set_pending_answer, **kwargs):
            send_audio("a", Path("/workspace/clip.mp3"), "Shadow this")
            send_message("a", "<b>task card</b>")
            set_pending_answer("a", {"kind": "eng_tue", "week_number": 2})
            send_report("a", "<b>recap</b>")

        with patch("openclaw_runtime.english_bot_scheduler.run_todays_push", fake_push):
            items = prepare_todays_push(day_code="tue", llm=MagicMock())

        self.assertEqual([item["type"] for item in items], ["audio", "message", "pending", "report"])
        self.assertEqual(items[0]["path"], "/workspace/clip.mp3")  # JSON-safe for the state file

        events = []
        deliver_outbox(
            items,
            send_message=lambda owner, text: events.append(("message", owner, text)),
            send_audio=lambda owner, path, caption: events.append(("audio", owner, path)),
            send_report=lambda owner, text: events.append(("report", owner, text)),
            set_pending_answer=lambda owner, item: events.append(("pending", owner, item["kind"])),
        )
        self.assertEqual(
            events,
            [
                ("audio", "a", Path("/workspace/clip.mp3")),
                ("message", "a", "<b>task card</b>"),
                ("pending", "a", "eng_tue"),
                ("report", "a", "<b>recap</b>"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
