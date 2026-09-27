"""Shared assertions for the English-bot task/feedback cards
(openclaw_runtime.message_cards): valid Telegram HTML, and the fixed section
order every day must keep."""

from html.parser import HTMLParser

from openclaw_runtime.message_cards import feedback_title, task_title

# Tags Telegram's HTML parse mode accepts that the cards actually use.
ALLOWED_TAGS = {"b", "i", "blockquote", "a", "code"}

TASK_SECTIONS = ["<b>Goal</b>", "<b>Time</b>", "<b>Today</b>", "<b>How to</b>"]
FEEDBACK_SECTIONS = ["<b>Result</b>", "<b>Tips</b>", "<b>Example</b>", "<b>Your answer</b>"]


class _TelegramHtmlChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported tag <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}>")
        else:
            self.stack.pop()


def assert_valid_telegram_html(testcase, html: str) -> None:
    checker = _TelegramHtmlChecker()
    checker.feed(html)
    checker.close()
    testcase.assertEqual(checker.errors, [], html)
    testcase.assertEqual(checker.stack, [], f"unclosed tags in: {html}")


def _assert_in_order(testcase, html: str, markers: list[str]) -> None:
    positions = [html.find(marker) for marker in markers]
    testcase.assertNotIn(-1, positions, f"missing a section in: {html}")
    testcase.assertEqual(positions, sorted(positions), f"sections out of order in: {html}")


def assert_task_card(testcase, html: str, day_code: str) -> None:
    assert_valid_telegram_html(testcase, html)
    testcase.assertTrue(html.startswith(f"<b>{task_title(day_code)}</b> · Week "), html)
    _assert_in_order(testcase, html, TASK_SECTIONS)


def assert_feedback_card(testcase, html: str, day_code: str) -> None:
    assert_valid_telegram_html(testcase, html)
    testcase.assertTrue(html.startswith(f"<b>{feedback_title(day_code)}</b>"), html)
    _assert_in_order(testcase, html, FEEDBACK_SECTIONS)
    testcase.assertTrue(html.endswith("Try again, or send /Done to finish."), html)
