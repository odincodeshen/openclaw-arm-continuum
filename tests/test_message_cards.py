import dataclasses
import io
import re
import unittest
import urllib.error
from unittest.mock import patch

import openclaw_telegram_gateway as gateway
from openclaw_runtime.message_cards import (
    DAY_TITLES,
    FeedbackCard,
    TaskCard,
    html_to_plain,
    markdown_bold_to_html,
    render_feedback_card,
    render_task_card,
    split_html_message,
)
from openclaw_runtime.skills.english_bot import (
    IELTS_QUESTION_BANK,
    Chunk,
    ClozeQuestion,
    WeeklyContent,
    build_friday_message,
    build_monday_message,
    build_saturday_message,
    build_sunday_message,
    build_thursday_message,
    build_tuesday_message,
    build_wednesday_message,
)
from tests.card_checks import (
    FEEDBACK_SECTIONS,
    TASK_SECTIONS,
    assert_feedback_card,
    assert_task_card,
    assert_valid_telegram_html,
)


def _all_seven_task_cards() -> dict[str, str]:
    chunks = [{"phrase": "move on", "definition": "繼續前進"}]
    return {
        "mon": build_monday_message(
            WeeklyContent(
                week_number=2,
                episode_title="Episode",
                episode_guid="g",
                segment_start=180.0,
                segment_end=345.0,
                transcript_excerpt="x",
                chunks=[Chunk("move on", "繼續前進", "We moved on.")],
                window_segments=[],
            )
        ),
        "tue": build_tuesday_message("I **never** left", 2),
        "wed": build_wednesday_message(IELTS_QUESTION_BANK[0], 2),
        "thu": build_thursday_message("weather_banter", "Grim out, isn't it?", 2),
        "fri": build_friday_message(chunks, 2),
        "sat": build_saturday_message([ClozeQuestion("move on", "We had to ____.", "context_sentence")], 2),
        "sun": build_sunday_message("Title", "https://example.com/a", "Summary.", 2, "摘要。"),
    }


class SameStructureEveryDayTest(unittest.TestCase):
    """The whole point of the shared renderer: all seven days' task cards
    carry the same sections in the same order, so a change to one day can't
    quietly drift from the rest."""

    def test_every_day_has_a_task_card_in_the_shared_layout(self) -> None:
        cards = _all_seven_task_cards()
        self.assertEqual(set(cards), set(DAY_TITLES))
        for day_code, html in cards.items():
            with self.subTest(day=day_code):
                assert_task_card(self, html, day_code)

    def test_section_skeleton_is_identical_across_days(self) -> None:
        def skeleton(html: str) -> list[str]:
            return [line for line in html.split("\n") if line in TASK_SECTIONS or line.startswith("<b>Goal</b>")]

        skeletons = {day: [s.split("　")[0] for s in skeleton(html)] for day, html in _all_seven_task_cards().items()}
        self.assertEqual(len({tuple(s) for s in skeletons.values()}), 1, skeletons)

    def test_only_the_title_is_chinese(self) -> None:
        """Short Chinese title; Goal/Time and the How-to steps are English.
        (The Today block may carry Chinese learning content, e.g. a chunk's
        meaning, so it isn't checked here.)"""
        cjk = re.compile("[぀-ヿ㐀-鿿＀-￯]")
        for day_code, html in _all_seven_task_cards().items():
            with self.subTest(day=day_code):
                lines = html.split("\n")
                self.assertIsNotNone(cjk.search(lines[0]))
                labels = [line for line in lines if line.startswith(("<b>Goal</b>", "<b>Time</b>"))]
                steps = html.split("<b>How to</b>\n", 1)[1]
                self.assertIsNone(cjk.search("\n".join(labels) + steps), html)


class RenderCardTest(unittest.TestCase):
    def test_task_card_escapes_plain_fields(self) -> None:
        html = render_task_card(
            TaskCard(
                day_code="thu",
                week_number=1,
                goal="Anchor & Bounce",
                duration="<3 min",
                reply_mode="Voice",
                content_html="ok",
                steps=["Say <hi>"],
            )
        )
        assert_task_card(self, html, "thu")
        self.assertIn("Anchor &amp; Bounce", html)
        self.assertIn("&lt;3 min", html)
        self.assertIn("1. Say &lt;hi&gt;", html)

    def test_feedback_card_escapes_everything_and_keeps_order(self) -> None:
        html = render_feedback_card(
            FeedbackCard(
                day_code="wed",
                result_lines=["✅ Situation <ok>"],
                tip_lines=["Use & more"],
                example="Model </blockquote> answer",
                answer="My <b>answer</b>",
            )
        )
        assert_feedback_card(self, html, "wed")
        self.assertIn("✅ Situation &lt;ok&gt;", html)
        self.assertIn("• Use &amp; more", html)
        self.assertIn("Model &lt;/blockquote&gt; answer", html)
        self.assertIn("My &lt;b&gt;answer&lt;/b&gt;", html)

    def test_feedback_card_fills_empty_tips_and_example(self) -> None:
        html = render_feedback_card(
            FeedbackCard(day_code="fri", result_lines=["✅ x"], tip_lines=[], example="  ", answer="a")
        )
        self.assertIn("• Nothing to flag.", html)
        self.assertIn("(not available this time)", html)

    def test_feedback_sections_order(self) -> None:
        html = render_feedback_card(
            FeedbackCard(day_code="sat", result_lines=["r"], tip_lines=["t"], example="e", answer="a")
        )
        positions = [html.index(section) for section in FEEDBACK_SECTIONS]
        self.assertEqual(positions, sorted(positions))


class MarkdownBoldTest(unittest.TestCase):
    def test_converts_stress_marks_and_escapes(self) -> None:
        self.assertEqual(markdown_bold_to_html("I **never** left & <came>"), "I <b>never</b> left &amp; &lt;came&gt;")

    def test_unpaired_markers_are_dropped(self) -> None:
        html = markdown_bold_to_html("a **b** c **d")
        self.assertEqual(html, "a <b>b</b> c d")
        assert_valid_telegram_html(self, html)


class SplitHtmlMessageTest(unittest.TestCase):
    def test_short_message_is_one_part(self) -> None:
        self.assertEqual(split_html_message("<b>hi</b>", 100), ["<b>hi</b>"])

    def test_splits_at_blank_lines_outside_tags(self) -> None:
        blocks = [f"<b>Block {i}</b>\n" + "x" * 40 for i in range(6)]
        parts = split_html_message("\n\n".join(blocks), 120)
        self.assertGreater(len(parts), 1)
        self.assertEqual("\n\n".join(parts), "\n\n".join(blocks))
        for part in parts:
            assert_valid_telegram_html(self, part)

    def test_never_cuts_inside_a_blockquote(self) -> None:
        quote = "<blockquote expandable>" + "\n\n".join("y" * 50 for _ in range(4)) + "</blockquote>"
        html = "intro\n\n" + quote + "\n\noutro"
        for part in split_html_message(html, 80):
            assert_valid_telegram_html(self, part)

    def test_html_to_plain(self) -> None:
        self.assertEqual(html_to_plain("<b>A &amp; B</b> <i>c</i>"), "A & B c")


class SendHtmlTest(unittest.TestCase):
    def test_sends_with_html_parse_mode(self) -> None:
        calls = []
        with patch.object(gateway, "telegram", lambda method, payload=None, timeout=60: calls.append(payload)):
            gateway.send_html(7, "<b>hi</b>")
        self.assertEqual(calls, [{"chat_id": 7, "text": "<b>hi</b>", "parse_mode": "HTML"}])

    def test_falls_back_to_plain_text_when_telegram_rejects_the_html(self) -> None:
        calls = []

        def fake_telegram(method, payload=None, timeout=60):
            calls.append(payload)
            if payload.get("parse_mode") == "HTML":
                raise urllib.error.HTTPError("url", 400, "Bad Request", {}, io.BytesIO(b"{}"))
            return {}

        with patch.object(gateway, "telegram", fake_telegram):
            gateway.send_html(7, "<b>A &amp; B</b>")
        self.assertEqual(calls[-1], {"chat_id": 7, "text": "A & B"})


class CommandsBypassPendingAnswerTest(unittest.TestCase):
    """While a daily task is open, a /command (like /w to look a word up)
    must go to normal command handling, not be evaluated as the answer."""

    def setUp(self) -> None:
        with gateway.PENDING_ANSWER_LOCK:
            gateway.PENDING_ANSWER.clear()
        self.addCleanup(gateway.PENDING_ANSWER.clear)
        patcher = patch.object(gateway, "settings", dataclasses.replace(gateway.settings, english_bot_enabled=True))
        patcher.start()
        self.addCleanup(patcher.stop)
        gateway.set_pending_answer(5, {"kind": "eng_thu", "week_number": 1, "opener": "Hi"})

    def test_slash_command_is_not_treated_as_an_answer(self) -> None:
        handled = gateway.handle_english_bot_pending_reply(5, {"text": "/w resilient"})
        self.assertFalse(handled)
        self.assertTrue(gateway.has_pending_answer(5))

    def test_done_still_closes_the_task(self) -> None:
        with patch.object(gateway, "send_message", lambda chat_id, text: None):
            handled = gateway.handle_english_bot_pending_reply(5, {"text": "/Done"})
        self.assertTrue(handled)
        self.assertFalse(gateway.has_pending_answer(5))

    def test_plain_text_is_still_an_answer(self) -> None:
        with patch.object(gateway.threading, "Thread") as thread:
            handled = gateway.handle_english_bot_pending_reply(5, {"text": "Yeah, grim. You?"})
        self.assertTrue(handled)
        thread.assert_called_once()


if __name__ == "__main__":
    unittest.main()
