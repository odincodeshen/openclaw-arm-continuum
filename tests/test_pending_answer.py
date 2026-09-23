import unittest

import openclaw_telegram_gateway as gateway


class PendingAnswerTest(unittest.TestCase):
    def setUp(self) -> None:
        with gateway.PENDING_ANSWER_LOCK:
            gateway.PENDING_ANSWER.clear()
        self.addCleanup(gateway.PENDING_ANSWER.clear)

    def test_no_pending_answer_by_default(self) -> None:
        self.assertFalse(gateway.has_pending_answer(1))
        self.assertIsNone(gateway.pop_pending_answer(1))

    def test_set_then_has_then_pop(self) -> None:
        gateway.set_pending_answer(1, {"kind": "cloze_quiz", "chunk": "spread oneself too thin"})
        self.assertTrue(gateway.has_pending_answer(1))

        item = gateway.pop_pending_answer(1)
        self.assertEqual(item, {"kind": "cloze_quiz", "chunk": "spread oneself too thin"})

    def test_pop_clears_the_pending_state(self) -> None:
        gateway.set_pending_answer(1, {"kind": "cloze_quiz"})
        gateway.pop_pending_answer(1)
        self.assertFalse(gateway.has_pending_answer(1))
        self.assertIsNone(gateway.pop_pending_answer(1))

    def test_different_chat_ids_are_independent(self) -> None:
        gateway.set_pending_answer(1, {"kind": "cloze_quiz"})
        self.assertFalse(gateway.has_pending_answer(2))

    def test_setting_again_overwrites_previous_pending_item(self) -> None:
        gateway.set_pending_answer(1, {"kind": "cloze_quiz", "chunk": "a"})
        gateway.set_pending_answer(1, {"kind": "cloze_quiz", "chunk": "b"})
        item = gateway.pop_pending_answer(1)
        self.assertEqual(item["chunk"], "b")

    def test_does_not_interfere_with_pending_category(self) -> None:
        with gateway.PENDING_CATEGORY_LOCK:
            gateway.PENDING_CATEGORY.clear()
        self.addCleanup(gateway.PENDING_CATEGORY.clear)

        gateway.set_pending_category(1, {"path": "/x/a.pdf", "kind": "document", "note": ""})
        gateway.set_pending_answer(1, {"kind": "cloze_quiz"})

        self.assertTrue(gateway.has_pending_category(1))
        self.assertTrue(gateway.has_pending_answer(1))


if __name__ == "__main__":
    unittest.main()
