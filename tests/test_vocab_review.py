import dataclasses
import json
import time
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import openclaw_telegram_gateway as gateway
from openclaw_runtime.vocabulary import (
    REVIEW_INTERVAL_DAYS,
    REVIEW_SESSION_SIZE,
    count_due_words,
    grade_review,
    next_review_after,
    render_review_quiz,
    render_review_reminder,
    render_word_list,
    start_review,
)
from tests.card_checks import assert_valid_telegram_html

TODAY = date(2026, 9, 27)
NOW = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)


def _point(point_id, word, next_review, **extra):
    payload = {"word": word, "display_word": word, "meaning": f"{word} 的意思", "next_review": next_review, **extra}
    return {"id": point_id, "payload": payload}


class FakeLlm:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.calls.append((prompt, schema_name))
        return self.responses.pop(0)


class NextReviewAfterTest(unittest.TestCase):
    def test_correct_moves_up_a_box_and_out_in_time(self) -> None:
        self.assertEqual(next_review_after(0, True, TODAY), (1, date(2026, 9, 30)))
        self.assertEqual(next_review_after(1, True, TODAY), (2, date(2026, 10, 4)))
        self.assertEqual(next_review_after(3, True, TODAY), (4, date(2026, 10, 27)))

    def test_top_box_stays_at_the_longest_interval(self) -> None:
        top = max(REVIEW_INTERVAL_DAYS)
        box, when = next_review_after(top, True, TODAY)
        self.assertEqual(box, top)
        self.assertEqual((when - TODAY).days, REVIEW_INTERVAL_DAYS[top])

    def test_wrong_goes_back_to_box_zero_tomorrow(self) -> None:
        self.assertEqual(next_review_after(4, False, TODAY), (0, date(2026, 9, 28)))


class StartReviewTest(unittest.TestCase):
    def test_picks_due_words_most_overdue_first_and_skips_future_ones(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [
            _point("a", "later", "2026-10-01"),
            _point("b", "today", "2026-09-27", lookup_count=1),
            _point("c", "overdue", "2026-09-20"),
            _point("d", "today_often", "2026-09-27", lookup_count=4),
        ]
        questions = start_review(qdrant, "coll", "owner-a", TODAY)
        self.assertEqual([q["phrase"] for q in questions], ["overdue", "today_often", "today"])
        self.assertEqual(qdrant.scroll_by_filters.call_args.args[1]["owner"], "owner-a")
        self.assertEqual(count_due_words(qdrant, "coll", "owner-a", TODAY), 3)

    def test_session_is_capped(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [_point(str(i), f"w{i}", "2026-09-01") for i in range(9)]
        self.assertEqual(len(start_review(qdrant, "coll", "owner-a", TODAY)), REVIEW_SESSION_SIZE)

    def test_uses_own_sentence_as_cloze_else_asks_from_meaning(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [
            _point("a", "resilient", "2026-09-27", context_sentence="She is remarkably resilient.", review_box=2),
            _point("b", "chill out", "2026-09-27"),
        ]
        first, second = start_review(qdrant, "coll", "owner-a", TODAY)
        self.assertEqual(first["prompt_text"], "She is remarkably ____. (hint: resilient 的意思)")
        self.assertEqual(first["point_id"], "a")
        self.assertEqual(first["box"], 2)
        self.assertEqual(second["prompt_text"], "Which word or phrase means: chill out 的意思")

    def test_nothing_due(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [_point("a", "later", "2026-12-01")]
        self.assertEqual(start_review(qdrant, "coll", "owner-a", TODAY), [])


def _judge(correct, model_answer="Model."):
    return {"correct": correct, "explanation_zh": "說明", "example_sentence": "Example.", "model_answer": model_answer}


class GradeReviewTest(unittest.TestCase):
    def test_updates_each_word_and_renders_feedback(self) -> None:
        qdrant = MagicMock()
        questions = [
            {"phrase": "resilient", "prompt_text": "She is ____.", "point_id": "a", "box": 1,
             "correct_count": 2, "wrong_count": 0},
            {"phrase": "chill out", "prompt_text": "Which word means: 放鬆", "point_id": "b", "box": 3,
             "correct_count": 0, "wrong_count": 1},
        ]
        llm = FakeLlm([json.dumps({"results": [_judge(True, "She is resilient."), _judge(False, "Chill out.")]})])

        html = grade_review(llm, qdrant, "coll", questions, "1. resilient 2. relax", TODAY, now=NOW)

        qdrant.set_payload.assert_any_call(
            "coll", "a",
            {"review_box": 2, "next_review": "2026-10-04", "last_review_at": NOW.isoformat(),
             "correct_count": 3, "wrong_count": 0},
        )
        qdrant.set_payload.assert_any_call(
            "coll", "b",
            {"review_box": 0, "next_review": "2026-09-28", "last_review_at": NOW.isoformat(),
             "correct_count": 0, "wrong_count": 2},
        )
        assert_valid_telegram_html(self, html)
        self.assertTrue(html.startswith("<b>生字複習｜回饋</b>"))
        self.assertIn("✅ 1. resilient — next review in 7 days", html)
        self.assertIn("❌ 2. chill out — next review tomorrow", html)
        self.assertIn("1. She is resilient.\n2. Chill out.", html)
        self.assertTrue(html.endswith("Next review dates updated. /vocab shows your list."))
        self.assertEqual(llm.calls[0][1], "saturday_cloze_judge")


class RenderReviewTest(unittest.TestCase):
    def test_quiz_card(self) -> None:
        html = render_review_quiz([{"prompt_text": "She is ____ & calm."}, {"prompt_text": "Q2"}])
        assert_valid_telegram_html(self, html)
        self.assertTrue(html.startswith("<b>生字複習</b>\n"))
        self.assertNotIn("Week", html.split("\n")[0])
        self.assertIn("<b>1.</b> She is ____ &amp; calm.", html)
        self.assertIn("Answer all 2 in one typed message", html)

    def test_reminder_only_when_something_is_due(self) -> None:
        self.assertEqual(render_review_reminder(0), "")
        self.assertIn("1 saved word is due today", render_review_reminder(1))
        self.assertIn("<b>Word review</b>\n3 saved words are due today — send /vocab review", render_review_reminder(3))

    def test_word_list_shows_due_count(self) -> None:
        html = render_word_list([{"word": "a"}], due_count=2)
        self.assertIn("2 due for review today — send /vocab review", html)


class GatewayReviewRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        gateway.VOCAB_REVIEW_PENDING.clear()
        self.addCleanup(gateway.VOCAB_REVIEW_PENDING.clear)
        patcher = patch.object(gateway, "settings", dataclasses.replace(gateway.settings, dictionary_enabled=True))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _open_review(self, expires_in=60.0):
        gateway.VOCAB_REVIEW_PENDING[5] = {"questions": [{"phrase": "x"}], "expires_at": time.time() + expires_in}

    def test_typed_reply_answers_the_review(self) -> None:
        self._open_review()
        with patch.object(gateway.threading, "Thread") as thread:
            self.assertTrue(gateway.handle_vocab_review_answer(5, {"text": "1. resilient"}))
        thread.assert_called_once()
        self.assertNotIn(5, gateway.VOCAB_REVIEW_PENDING)

    def test_voice_and_commands_are_left_for_other_handlers(self) -> None:
        self._open_review()
        self.assertFalse(gateway.handle_vocab_review_answer(5, {"voice": {"file_id": "f"}}))
        self.assertFalse(gateway.handle_vocab_review_answer(5, {"text": "/Done"}))
        self.assertIn(5, gateway.VOCAB_REVIEW_PENDING)

    def test_expired_review_is_dropped_not_answered(self) -> None:
        self._open_review(expires_in=-1)
        self.assertFalse(gateway.handle_vocab_review_answer(5, {"text": "an answer to the daily task"}))
        self.assertNotIn(5, gateway.VOCAB_REVIEW_PENDING)

    def test_no_review_open(self) -> None:
        self.assertFalse(gateway.handle_vocab_review_answer(5, {"text": "hello"}))

    def test_vocab_review_command_with_nothing_due(self) -> None:
        sent = []
        with patch.object(gateway, "start_review", lambda *args: []), \
                patch.object(gateway, "send_message", lambda chat_id, text: sent.append(text)):
            self.assertTrue(gateway.handle_vocabulary_command(5, "/vocab review"))
        self.assertIn("No saved words are due", sent[0])
        self.assertNotIn(5, gateway.VOCAB_REVIEW_PENDING)

    def test_vocab_review_command_opens_a_session(self) -> None:
        sent = []
        questions = [{"phrase": "x", "prompt_text": "Q"}]
        with patch.object(gateway, "start_review", lambda *args: questions), \
                patch.object(gateway, "send_html", lambda chat_id, html: sent.append(html)):
            self.assertTrue(gateway.handle_vocabulary_command(5, "/vocab review"))
        self.assertEqual(gateway.VOCAB_REVIEW_PENDING[5]["questions"], questions)
        self.assertIn("生字複習", sent[0])

    def test_daily_card_gets_the_reminder_for_that_owner(self) -> None:
        sent = []
        with patch.object(gateway, "count_due_words", lambda qdrant, collection, owner, today: 2), \
                patch.object(gateway, "send_html", lambda chat_id, html: sent.append((chat_id, html))):
            gateway._english_bot_send_message("5", "<b>card</b>")
        self.assertEqual(sent[0][0], 5)
        self.assertTrue(sent[0][1].startswith("<b>card</b>\n\n<b>Word review</b>\n2 saved words"))

    def test_reminder_failure_never_blocks_the_daily_card(self) -> None:
        sent = []

        def boom(*args):
            raise RuntimeError("qdrant down")

        with patch.object(gateway, "count_due_words", boom), \
                patch.object(gateway, "send_html", lambda chat_id, html: sent.append(html)):
            gateway._english_bot_send_message("5", "<b>card</b>")
        self.assertEqual(sent, ["<b>card</b>"])


if __name__ == "__main__":
    unittest.main()
