"""Night ritual: a short wind-down journal, one question at a time.

Each ritual night (by default Sunday-Friday at 22:30) the bot asks four
questions -- three wins, one 1% improvement, one adjustment, and the first
thing for tomorrow -- then closes the day with a summary card and one warm
line from the local model. Before the first question it asks, with two
buttons, whether the previous night's "first thing" got done. The next
morning that first thing comes back as a reminder; weekly and monthly
reports sum it all up.

Everything is kept as one small JSON file per night under
``<dir>/<owner>/<YYYY-MM-DD>.json`` -- outside the inbox, so it is never
indexed into memory, never searched by /rag, and never part of the
knowledge digest. Reports are also written there as Markdown.

This module is the pure part (storage, rendering, the model prompts); the
Telegram gateway owns the scheduling and the conversation state.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from openclaw_runtime.message_cards import RULE, bold, esc

STEPS = ["wins", "better", "adjust", "first"]

QUESTIONS = {
    "wins": (
        "Wins &amp; Gratitude",
        "What went well today — a win, a small breakthrough, or a nice moment?",
        "One message, 1–3 things · text or voice",
    ),
    "better": (
        "1% Better",
        "Compared with yesterday, where did you get a little better — a skill, your mood, or how you worked?",
        "",
    ),
    "adjust": (
        "One Adjustment",
        "No judgement. If you replayed today, what one thing would you smooth out?",
        "",
    ),
    "first": (
        "Mental Shutdown",
        "What's the one thing to start tomorrow with? Write it down and today is done.",
        "",
    ),
}

LABELS = {"wins": "Wins", "better": "1% Better", "adjust": "Adjustment", "first": "First thing tomorrow"}
SHORT_LABELS = {"wins": "Wins", "better": "1%", "adjust": "Adjust", "first": "First"}
STATUS_MARKS = {"done": "✅", "partial": "◐", "missed": "❌", "open": "…"}
WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FALLBACK_CLOSING = "You showed up for today. Let the rest wait until tomorrow — rest well."


def parse_ritual_days(text: str) -> set[int]:
    """"sun,mon,tue" -> {6, 0, 1} (Python weekday numbers)."""
    days = set()
    for part in (text or "").lower().split(","):
        part = part.strip()[:3]
        if part in WEEKDAY_CODES:
            days.add(WEEKDAY_CODES.index(part))
    return days


def next_step(entry: dict) -> str | None:
    answers = entry.get("answers") or {}
    for step in STEPS:
        if not answers.get(step):
            return step
    return None


def split_wins(text: str) -> list[str]:
    """Wins come as one message; show them as bullets when they were written
    as separate lines or a numbered/semicolon list."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"\n+|[；;]|(?:^|\s)\d+[.)、]\s*", text)
    items = [part.strip(" ,，、-•") for part in parts]
    return [item for item in items if item] or [text]


# --- storage ------------------------------------------------------------------


class NightStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, owner: str, day: date) -> Path:
        return self.root / str(owner) / f"{day.isoformat()}.json"

    def load(self, owner: str, day: date) -> dict | None:
        path = self._path(owner, day)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, owner: str, entry: dict) -> None:
        path = self._path(owner, date.fromisoformat(entry["date"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def between(self, owner: str, start: date, end: date) -> list[dict]:
        """Entries from start to end inclusive, oldest first."""
        entries = []
        day = start
        while day <= end:
            entry = self.load(owner, day)
            if entry:
                entries.append(entry)
            day += timedelta(days=1)
        return entries

    def previous_to_check(self, owner: str, day: date, lookback_days: int = 3) -> dict | None:
        """The most recent earlier night (within lookback_days) whose first
        thing hasn't been marked done / not done yet."""
        for back in range(1, lookback_days + 1):
            entry = self.load(owner, day - timedelta(days=back))
            if entry is None:
                continue
            if (entry.get("answers") or {}).get("first") and entry.get("first_done") is None:
                return entry
            return None  # only the latest earlier night is asked about
        return None

    def move(self, owner: str, source: date, target: date) -> dict:
        """Re-date a night, e.g. one answered about the previous day. Refuses
        to overwrite an existing night."""
        entry = self.load(owner, source)
        if entry is None:
            raise ValueError(f"no night recorded on {source.isoformat()}")
        if self.load(owner, target) is not None:
            raise ValueError(f"{target.isoformat()} already has a night")
        entry["date"] = target.isoformat()
        entry["moved_from"] = source.isoformat()
        self.save(owner, entry)
        self._path(owner, source).unlink(missing_ok=True)
        return entry

    def write_report(self, owner: str, name: str, markdown: str) -> Path:
        path = self.root / str(owner) / "reports" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        return path


def new_entry(day: date, now_iso: str) -> dict:
    return {"date": day.isoformat(), "status": "open", "answers": {}, "started_at": now_iso}


def close_status(entry: dict) -> str:
    """Status for a night that reached the deadline unfinished."""
    return "partial" if any((entry.get("answers") or {}).values()) else "missed"


# --- cards --------------------------------------------------------------------


def _title(zh: str, label: str) -> str:
    return f"<b>【{zh}】</b>· {label}\n{RULE}"


def render_question(step: str) -> str:
    label, prompt, hint = QUESTIONS[step]
    number = STEPS.index(step) + 1
    lines = [_title(f"晚安儀式 {number}/4", label), prompt]
    if hint:
        lines += ["", f"<i>{hint}</i>"]
    return "\n".join(lines)


def render_first_check(previous: dict) -> str:
    first = (previous.get("answers") or {}).get("first", "")
    return "\n".join(
        [
            _title("昨天的第一件事", "Yesterday's first thing"),
            "You planned to start with:",
            bold(first),
            "",
            "Did you get to it?",
        ]
    )


def render_first_check_answered(previous: dict, done: bool) -> str:
    first = (previous.get("answers") or {}).get("first", "")
    result = "✅ Done" if done else "❌ Not yet"
    return "\n".join([_title("昨天的第一件事", "Yesterday's first thing"), bold(first), "", result])


def _day_label(day: date) -> str:
    return day.strftime("%a %d %b").replace(" 0", " ")


def render_entry_body(entry: dict) -> list[str]:
    answers = entry.get("answers") or {}
    lines: list[str] = []
    wins = split_wins(answers.get("wins", ""))
    if wins:
        lines.append(bold(LABELS["wins"]))
        lines += [f"• {esc(item)}" for item in wins]
    for step in ("better", "adjust", "first"):
        if answers.get(step):
            if lines:
                lines.append("")
            lines += [bold(LABELS[step]), esc(answers[step])]
    return lines


def render_closing(entry: dict, closing: str) -> str:
    day = date.fromisoformat(entry["date"])
    lines = [_title("今日結案", _day_label(day))]
    lines += render_entry_body(entry)
    lines += ["", RULE, esc(closing)]
    return "\n".join(lines)


def render_reminder(step: str | None, last: bool) -> str:
    if step == "check":
        where = "tap ✅ or ❌ on the card above to start"
    elif step:
        where = f"just pick up at {STEPS.index(step) + 1}/4"
    else:
        where = "send /night start to begin"
    if last:
        return "\n".join(
            [
                _title("最後提醒", "Last call"),
                f"Tonight's wind-down is still open — {where}.",
                "Replies count until midnight; after that tonight is saved as it is.",
            ]
        )
    return "\n".join([_title("晚安提醒", "Reminder"), f"Tonight's wind-down is still open — {where}."])


def render_morning(first: str) -> str:
    return "\n".join([_title("今天的第一件事", "First thing today"), "You wrote last night:", bold(first), "", "Start here."])


def day_status(entry: dict | None, day: date, ritual_days: set[int], today: date) -> str | None:
    """done / partial / missed / open for a night, or None for a day with no
    ritual and no entry (e.g. Saturday)."""
    if entry:
        status = entry.get("status", "open")
        if status == "open" and day < today:
            return close_status(entry)
        return status
    if day.weekday() in ritual_days and day < today:
        return "missed"
    return None


def status_line(statuses: list[tuple[date, str]]) -> str:
    return "　".join(f"{day.strftime('%a')} {day.day} {STATUS_MARKS[status]}" for day, status in statuses)


def render_history(entries_by_day: dict[date, dict], days: list[date], ritual_days: set[int], today: date,
                   *, detail: list[dict]) -> str:
    statuses = [
        (day, status)
        for day in days
        if (status := day_status(entries_by_day.get(day), day, ritual_days, today)) is not None
    ]
    lines = [_title("晚安紀錄", "/night")]
    lines.append(status_line(statuses) if statuses else "No nights recorded yet.")
    for entry in detail:
        answers = entry.get("answers") or {}
        if not any(answers.values()):
            continue
        lines += ["", bold(_day_label(date.fromisoformat(entry["date"])))]
        for step in STEPS:
            if answers.get(step):
                value = "；".join(split_wins(answers[step])) if step == "wins" else answers[step]
                lines.append(f"{SHORT_LABELS[step]}: {esc(value)}")
        if entry.get("first_done") is not None:
            lines.append(f"First done: {'✅' if entry['first_done'] else '❌'}")
    lines += ["", "<i>/night 7d · /night 2026-09-28 · /night start · /night week · /night month · /night move</i>"]
    return "\n".join(lines)


# --- model prompts --------------------------------------------------------------


def closing_line(llm, entry: dict) -> str:
    """One or two warm English sentences for the end of the night. Never
    advice or analysis. Falls back to a fixed line if the model fails."""
    answers = entry.get("answers") or {}
    prompt = (
        "Someone just finished a short end-of-day journal. Write ONE or TWO short, warm sentences in "
        "English to close their day -- acknowledge something specific they wrote, then let them rest. "
        "No advice, no analysis, no questions, no emoji, no quotation marks. Reply in English only, "
        "even if the notes are in Chinese.\n\n"
        f"Wins: {answers.get('wins', '')}\n1% better: {answers.get('better', '')}\n"
        f"Adjustment: {answers.get('adjust', '')}\nFirst thing tomorrow: {answers.get('first', '')}"
    )
    try:
        text = " ".join((llm.chat(prompt, max_tokens=120) or "").split()).strip().strip('"“”')
    except Exception:  # noqa: BLE001 - the card still closes the day without it
        return FALLBACK_CLOSING
    return text or FALLBACK_CLOSING


REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "highlights": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "growing": {"type": "string"},
        "adjusting": {"type": "string"},
        "closing": {"type": "string"},
    },
    "required": ["highlights", "growing", "adjusting", "closing"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ReportSummary:
    highlights: list[str]
    growing: str
    adjusting: str
    closing: str


def summarize_period(llm, entries: list[dict], period_name: str) -> ReportSummary:
    notes = []
    for entry in entries:
        answers = entry.get("answers") or {}
        if any(answers.values()):
            notes.append(
                f"{entry['date']}: wins={answers.get('wins', '')} | better={answers.get('better', '')} | "
                f"adjust={answers.get('adjust', '')}"
            )
    if not notes:
        return ReportSummary([], "", "", "")
    prompt = (
        f"These are one person's end-of-day journal notes for {period_name}. Write, in English only "
        "(translate from Chinese where needed):\n"
        "- highlights: up to 3 of their best moments or wins, each a short phrase;\n"
        "- growing: one sentence on the improvement that shows up most in 'better';\n"
        "- adjusting: one sentence on the adjustment that comes up most in 'adjust' (say how many times "
        "if it repeats), phrased kindly, with at most one gentle suggestion;\n"
        f"- closing: one short, warm sentence for the end of the {period_name}.\n"
        "Only use what the notes say.\n\n" + "\n".join(notes)
    )
    try:
        data = json.loads(llm.chat_json(prompt, REPORT_SCHEMA, schema_name="night_report", max_tokens=600))
        return ReportSummary(
            [str(item).strip() for item in data.get("highlights", []) if str(item).strip()][:3],
            str(data.get("growing", "")).strip(),
            str(data.get("adjusting", "")).strip(),
            str(data.get("closing", "")).strip(),
        )
    except Exception:  # noqa: BLE001 - the counts are still worth sending
        return ReportSummary([], "", "", "")


@dataclass(frozen=True)
class PeriodStats:
    statuses: list[tuple[date, str]]
    done: int
    partial: int
    nights: int
    first_written: int
    first_done: int
    first_not_done: int


def period_stats(entries_by_day: dict[date, dict], start: date, end: date, ritual_days: set[int], today: date) -> PeriodStats:
    statuses = []
    day = start
    while day <= end:
        status = day_status(entries_by_day.get(day), day, ritual_days, today)
        if status is not None:
            statuses.append((day, status))
        day += timedelta(days=1)
    entries = [entries_by_day[d] for d, _ in statuses if d in entries_by_day]
    firsts = [e for e in entries if (e.get("answers") or {}).get("first")]
    return PeriodStats(
        statuses=statuses,
        done=sum(1 for _, s in statuses if s == "done"),
        partial=sum(1 for _, s in statuses if s == "partial"),
        nights=len(statuses),
        first_written=len(firsts),
        first_done=sum(1 for e in firsts if e.get("first_done") is True),
        first_not_done=sum(1 for e in firsts if e.get("first_done") is False),
    )


@dataclass(frozen=True)
class Trend:
    streak: int  # ritual nights done in a row, ending with the period's last one
    done_rate: float | None
    previous_done_rate: float | None
    first_rate: float | None
    previous_first_rate: float | None


def _rate(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def current_streak(store: "NightStore", owner: str, end: date, ritual_days: set[int], max_days: int = 400) -> int:
    """Done nights in a row, counting back from end over ritual days only
    (a skipped Saturday doesn't break it; a partial or missed night does)."""
    streak = 0
    day = end
    for _ in range(max_days):
        if day.weekday() in ritual_days:
            entry = store.load(owner, day)
            if not entry or entry.get("status") != "done":
                break
            streak += 1
        day -= timedelta(days=1)
    return streak


def compute_trend(stats: PeriodStats, previous: PeriodStats, streak: int) -> Trend:
    return Trend(
        streak=streak,
        done_rate=_rate(stats.done, stats.nights),
        previous_done_rate=_rate(previous.done, previous.nights),
        first_rate=_rate(stats.first_done, stats.first_done + stats.first_not_done),
        previous_first_rate=_rate(previous.first_done, previous.first_done + previous.first_not_done),
    )


def _trend_line(label: str, now: float | None, before: float | None, previous_name: str) -> str | None:
    if now is None:
        return None
    text = f"{label}: {round(now * 100)}%"
    if before is None:
        return text
    arrow = "↑" if now > before + 0.005 else ("↓" if now < before - 0.005 else "→")
    return f"{text} ({previous_name} {round(before * 100)}%) {arrow}"


def trend_lines(trend: Trend, kind: str) -> list[str]:
    previous_name = "last week" if kind == "week" else "last month"
    lines = [f"Streak: {trend.streak} night{'s' if trend.streak != 1 else ''} in a row"]
    for line in (
        _trend_line("Nights done", trend.done_rate, trend.previous_done_rate, previous_name),
        _trend_line("First things done", trend.first_rate, trend.previous_first_rate, previous_name),
    ):
        if line:
            lines.append(line)
    return lines


def _period_label(start: date, end: date) -> str:
    return f"{start.day} {start.strftime('%b')} – {end.day} {end.strftime('%b')}"


def render_report(kind: str, start: date, end: date, stats: PeriodStats, summary: ReportSummary,
                  trend: Trend | None = None) -> str:
    """kind: "week" or "month"."""
    zh = "晚安週報" if kind == "week" else "晚安月報"
    label = _period_label(start, end) if kind == "week" else start.strftime("%B %Y")
    lines = [_title(zh, label), bold("Done")]
    if kind == "week":
        lines.append(" ".join(f"{d.strftime('%a')} {STATUS_MARKS[s]}" for d, s in stats.statuses) or "—")
    done_text = f"{stats.done} of {stats.nights} nights"
    if stats.partial:
        done_text += f" · {stats.partial} partly"
    lines.append(done_text)
    if trend is not None:
        lines += ["", bold("Trend")] + [esc(line) for line in trend_lines(trend, kind)]
    if summary.highlights:
        lines += ["", bold("Highlights")] + [f"• {esc(item)}" for item in summary.highlights]
    if summary.growing:
        lines += ["", bold("Growing"), esc(summary.growing)]
    if summary.adjusting:
        lines += ["", bold("Adjusting"), esc(summary.adjusting)]
    if stats.first_written:
        first = f"{stats.first_written} written · {stats.first_done} done"
        if stats.first_not_done:
            first += f" · {stats.first_not_done} not yet"
        lines += ["", bold("First things"), first]
    if summary.closing:
        lines += ["", RULE, esc(summary.closing)]
    return "\n".join(lines)


def render_report_markdown(kind: str, start: date, end: date, stats: PeriodStats, summary: ReportSummary,
                           entries: list[dict], trend: Trend | None = None) -> str:
    title = "Night ritual — week of " + _period_label(start, end) if kind == "week" else (
        "Night ritual — " + start.strftime("%B %Y")
    )
    lines = [f"# {title}", "", f"- Done: {stats.done} of {stats.nights} nights"
             + (f" ({stats.partial} partly)" if stats.partial else "")]
    if stats.first_written:
        lines.append(f"- First things: {stats.first_written} written, {stats.first_done} done, "
                     f"{stats.first_not_done} not yet")
    if trend is not None:
        lines += [f"- {line}" for line in trend_lines(trend, kind)]
    if summary.highlights:
        lines += ["", "## Highlights", ""] + [f"- {item}" for item in summary.highlights]
    if summary.growing:
        lines += ["", "## Growing", "", summary.growing]
    if summary.adjusting:
        lines += ["", "## Adjusting", "", summary.adjusting]
    if summary.closing:
        lines += ["", f"_{summary.closing}_"]
    lines += ["", "## Nights", ""]
    for entry in entries:
        answers = entry.get("answers") or {}
        lines.append(f"### {entry['date']} ({entry.get('status', '')})")
        lines.append("")
        for step in STEPS:
            if answers.get(step):
                lines.append(f"- **{LABELS[step]}:** {answers[step]}")
        if entry.get("first_done") is not None:
            lines.append(f"- **First thing done:** {'yes' if entry['first_done'] else 'not yet'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def report_period(kind: str, report_day: date) -> tuple[date, date]:
    """The period a report sent on report_day covers: the seven days up to
    yesterday for "week" (sent Monday: Monday to Sunday), the previous
    calendar month for "month"."""
    if kind == "week":
        return report_day - timedelta(days=7), report_day - timedelta(days=1)
    last = report_day.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


def reports_due(report_day: date) -> list[str]:
    kinds = []
    if report_day.weekday() == 0:
        kinds.append("week")
    if report_day.day == 1:
        kinds.append("month")
    return kinds


def build_report(llm, store: NightStore, owner: str, kind: str, report_day: date, ritual_days: set[int]) -> tuple[str, str]:
    """(Telegram HTML, Markdown) for the period ending before report_day.
    The Markdown copy is also written under the owner's reports folder."""
    start, end = report_period(kind, report_day)
    entries = store.between(owner, start, end)
    by_day = {date.fromisoformat(e["date"]): e for e in entries}
    stats = period_stats(by_day, start, end, ritual_days, report_day)
    previous_start, previous_end = report_period(kind, start)
    previous_entries = store.between(owner, previous_start, previous_end)
    previous = period_stats(
        {date.fromisoformat(e["date"]): e for e in previous_entries}, previous_start, previous_end, ritual_days, report_day
    )
    trend = compute_trend(stats, previous, current_streak(store, owner, end, ritual_days))
    summary = summarize_period(llm, entries, "week" if kind == "week" else "month")
    html = render_report(kind, start, end, stats, summary, trend)
    markdown = render_report_markdown(kind, start, end, stats, summary, entries, trend)
    name = f"week-{start.isoformat()}" if kind == "week" else f"month-{start.strftime('%Y-%m')}"
    store.write_report(owner, name, markdown)
    return html, markdown
