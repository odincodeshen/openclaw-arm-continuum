"""Shared Telegram HTML layout for the English-learning bot's task cards and
feedback cards.

Every day's push and every day's evaluation goes through the two renderers
below, so all seven days keep exactly the same section order: a task card is
title -> rule -> Goal/Time -> Today -> How to, a feedback card is title ->
rule -> Result -> Tips -> Example -> Your answer -> footer. Only the task
card's "Today" block differs per day.

Titles are short Chinese; every label and instruction is English. Icons are
kept to the ✅/❌ marks in results.

Everything is sent with Telegram's HTML parse mode, so any text that did not
come from this module (LLM output, a user's transcript, RSS titles) must be
escaped -- the renderers escape every plain-text field themselves, and the
only pre-built HTML they accept is a task card's ``content_html``, which
builders assemble with the helpers here.
"""

import re
from dataclasses import dataclass
from html import escape, unescape

RULE = "━━━━━━━━━━━━━━"
FEEDBACK_FOOTER = "Try again, or send /Done to finish."
DONE_STEP = "Try again as often as you like, then send /Done"
NOTHING_TO_FLAG = "Nothing to flag."

# Short Chinese title per day (task card), feedback cards reuse the weekday.
DAY_TITLES = {
    "mon": ("週一", "精聽語塊"),
    "tue": ("週二", "影子跟讀"),
    "wed": ("週三", "雅思口說"),
    "thu": ("週四", "英式閒聊"),
    "fri": ("週五", "語塊活化"),
    "sat": ("週六", "填空複習"),
    "sun": ("週日", "輕鬆閱讀"),
}


def esc(text: object) -> str:
    return escape(str(text), quote=False)


def bold(text: object) -> str:
    return f"<b>{esc(text)}</b>"


def italic(text: object) -> str:
    return f"<i>{esc(text)}</i>"


def expandable(inner_html: str) -> str:
    """Telegram's collapsible quote: shows a couple of lines until tapped."""
    return f"<blockquote expandable>{inner_html}</blockquote>"


_MARKDOWN_BOLD = re.compile(r"\*\*([^*\n]+?)\*\*")


def markdown_bold_to_html(text: str) -> str:
    """Escape ``text`` and turn its ``**word**`` stress marks into real
    bold -- Tuesday's shadowing annotation comes back from the LLM in that
    Markdown form. Any unpaired ``**`` left over is dropped rather than shown
    as literal asterisks."""
    html = _MARKDOWN_BOLD.sub(r"<b>\1</b>", esc(text))
    return html.replace("**", "")


def task_title(day_code: str) -> str:
    weekday, name = DAY_TITLES[day_code]
    return f"{weekday}｜{name}"


def feedback_title(day_code: str) -> str:
    weekday, _ = DAY_TITLES[day_code]
    return f"{weekday}｜回饋"


@dataclass(frozen=True)
class TaskCard:
    day_code: str
    week_number: int
    goal: str
    duration: str
    reply_mode: str
    content_html: str  # the only per-day block; built with the helpers above
    steps: list[str]
    # For a card that isn't one of the seven days (e.g. word review): its own
    # title, and no week number when week_number is 0.
    title: str = ""


def render_task_card(card: TaskCard) -> str:
    steps = "\n".join(f"{index}. {esc(step)}" for index, step in enumerate(card.steps, start=1))
    heading = f"<b>{esc(card.title or task_title(card.day_code))}</b>"
    if card.week_number:
        heading += f" · Week {card.week_number}"
    return (
        f"{heading}\n{RULE}\n"
        f"<b>Goal</b>　{esc(card.goal)}\n"
        f"<b>Time</b>　{esc(card.duration)} | {esc(card.reply_mode)}\n\n"
        f"<b>Today</b>\n{card.content_html}\n\n"
        f"<b>How to</b>\n{steps}"
    )


@dataclass(frozen=True)
class FeedbackCard:
    day_code: str
    result_lines: list[str]
    tip_lines: list[str]
    example: str
    answer: str
    title: str = ""
    footer: str = FEEDBACK_FOOTER


def render_feedback_card(card: FeedbackCard) -> str:
    results = "\n".join(esc(line) for line in card.result_lines)
    tips = "\n".join(f"• {esc(tip)}" for tip in card.tip_lines) or f"• {NOTHING_TO_FLAG}"
    example = esc(card.example.strip()) or "(not available this time)"
    answer = esc(card.answer.strip()) or "(empty)"
    return (
        f"<b>{esc(card.title or feedback_title(card.day_code))}</b>\n{RULE}\n"
        f"<b>Result</b>\n{results}\n\n"
        f"<b>Tips</b>\n{tips}\n\n"
        f"<b>Example</b> (tap to expand)\n{expandable(example)}\n"
        f"<b>Your answer</b> (tap to expand)\n{expandable(answer)}\n"
        f"{esc(card.footer)}"
    )


_TAG = re.compile(r"<[^>]+>")


def html_to_plain(html: str) -> str:
    """Fallback for when Telegram rejects the HTML -- strip tags, unescape."""
    return unescape(_TAG.sub("", html))


_TAG_TOKEN = re.compile(r"<(/?)([a-zA-Z]+)[^>]*>")


def _tag_depth_after(html: str) -> int:
    depth = 0
    for match in _TAG_TOKEN.finditer(html):
        depth += -1 if match.group(1) else 1
    return depth


def split_html_message(html: str, limit: int) -> list[str]:
    """Split a long HTML message into Telegram-sized parts at blank-line
    boundaries that sit outside any open tag, so no part ever carries half
    a <b> or <blockquote>. A single block that is still too long on its own
    is returned as-is; the sender falls back to plain text for it."""
    if len(html) <= limit:
        return [html]
    blocks: list[str] = []
    current = ""
    for piece in html.split("\n\n"):
        candidate = f"{current}\n\n{piece}" if current else piece
        if _tag_depth_after(candidate) != 0:
            current = candidate  # inside a tag -- can't cut here
            continue
        if current and len(candidate) > limit and _tag_depth_after(current) == 0:
            blocks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        blocks.append(current)
    return blocks
