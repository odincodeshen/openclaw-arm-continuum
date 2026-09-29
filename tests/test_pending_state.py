import dataclasses
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import openclaw_telegram_gateway as gateway


class PendingStatePersistenceTest(unittest.TestCase):
    """Open English tasks and /vocab reviews survive a gateway restart."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / ".openclaw" / "pending_answers.json"
        patcher = patch.object(gateway, "settings", dataclasses.replace(gateway.settings, pending_state_path=self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        for store in (gateway.PENDING_ANSWER, gateway.VOCAB_REVIEW_PENDING, gateway.PENDING_CATEGORY):
            store.clear()
            self.addCleanup(store.clear)

    def _restart(self) -> None:
        gateway.PENDING_ANSWER.clear()
        gateway.VOCAB_REVIEW_PENDING.clear()
        gateway.PENDING_CATEGORY.clear()
        with patch.object(gateway, "log", lambda message: None):
            gateway.restore_pending_state()

    def test_open_english_task_survives_a_restart(self) -> None:
        task = {"kind": "eng_wed", "week_number": 2, "cue_card": "Describe a park."}
        gateway.set_pending_answer(42, task)
        self.assertTrue(self.path.exists())
        self._restart()
        self.assertEqual(gateway.peek_pending_answer(42), task)

    def test_closed_task_stays_closed_after_a_restart(self) -> None:
        gateway.set_pending_answer(42, {"kind": "eng_thu", "week_number": 2, "opener": "Hi"})
        gateway.pop_pending_answer(42)
        self._restart()
        self.assertIsNone(gateway.peek_pending_answer(42))

    def test_vocab_review_survives_but_an_expired_one_is_dropped(self) -> None:
        gateway.VOCAB_REVIEW_PENDING[1] = {"questions": [{"phrase": "a"}], "expires_at": time.time() + 600}
        gateway.VOCAB_REVIEW_PENDING[2] = {"questions": [{"phrase": "b"}], "expires_at": time.time() - 1}
        gateway.save_pending_state()
        self._restart()
        self.assertIn(1, gateway.VOCAB_REVIEW_PENDING)
        self.assertNotIn(2, gateway.VOCAB_REVIEW_PENDING)

    def test_file_holds_both_kinds_keyed_by_chat_id(self) -> None:
        gateway.set_pending_answer(7, {"kind": "eng_fri", "week_number": 2})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            data,
            {"english_task": {"7": {"kind": "eng_fri", "week_number": 2}}, "vocab_review": {}, "night": {}, "category": {}},
        )

    def test_upload_waiting_for_a_category_survives_a_restart(self) -> None:
        staged = self.path.parent / "staged.pdf"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"%PDF")
        gateway.set_pending_category(5, {"path": str(staged), "kind": "document", "note": "", "original_name": "a.pdf"})
        gateway.set_pending_category(6, {"path": str(self.path.parent / "gone.pdf"), "kind": "document"})
        self._restart()
        self.assertEqual(gateway.PENDING_CATEGORY[5]["items"][0]["original_name"], "a.pdf")
        self.assertNotIn(6, gateway.PENDING_CATEGORY)  # its file is no longer in staging
        gateway.pop_pending_category(5)
        self._restart()
        self.assertEqual(gateway.PENDING_CATEGORY, {})

    def test_unreadable_file_is_ignored(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self._restart()
        self.assertEqual(gateway.PENDING_ANSWER, {})

    def test_saving_is_off_without_a_path(self) -> None:
        with patch.object(gateway, "settings", dataclasses.replace(gateway.settings, pending_state_path=None)):
            gateway.set_pending_answer(9, {"kind": "eng_mon", "week_number": 2})
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
