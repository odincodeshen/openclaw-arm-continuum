"""Night ritual: the built-in check-in preset (checkin_presets/night.toml).

The engine is openclaw_runtime.checkins; this module keeps the night
ritual's long-standing names (render_question("wins"), NightStore,
build_report(..., ritual_days) ...) as thin wrappers bound to the preset, so
existing callers and saved data stay as they were. See docs/NIGHT_RITUAL.md
and docs/CHECKINS.md.
"""

from dataclasses import replace
from datetime import date
from pathlib import Path

from openclaw_runtime import checkins as ck
from openclaw_runtime.checkins import (  # noqa: F401 - re-exported
    STATUS_MARKS,
    WEEKDAY_CODES,
    PeriodStats,
    ReportSummary,
    Trend,
    close_status,
    compute_trend,
    day_status,
    new_entry,
    status_line,
)

PRESET_PATH = Path(__file__).parent / "checkin_presets" / "night.toml"
NIGHT = ck.parse_spec(PRESET_PATH.read_text(encoding="utf-8"), source="night.toml")

STEPS = NIGHT.steps
QUESTIONS = {q.id: (q.label, q.prompt, q.hint) for q in NIGHT.questions}
FALLBACK_CLOSING = NIGHT.closing_fallback
REPORT_SCHEMA = ck.report_schema(NIGHT)

parse_ritual_days = ck.parse_days
split_wins = ck.split_items


def _with_days(ritual_days) -> ck.CheckinSpec:
    return replace(NIGHT, days=frozenset(ritual_days))


class NightStore(ck.CheckinStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root, follow_source="first", noun="night")


def next_step(entry: dict) -> str | None:
    return ck.next_step(NIGHT, entry)


def render_question(step: str) -> str:
    return ck.render_question(NIGHT, step)


def render_first_check(previous: dict) -> str:
    return ck.render_follow_up(NIGHT, previous)


def render_first_check_answered(previous: dict, done: bool) -> str:
    return ck.render_follow_up_answered(NIGHT, previous, done)


def render_entry_body(entry: dict) -> list[str]:
    return ck.render_entry_body(NIGHT, entry)


def render_closing(entry: dict, closing: str) -> str:
    return ck.render_closing(NIGHT, entry, closing)


def render_reminder(step: str | None, last: bool) -> str:
    return ck.render_reminder(NIGHT, step, last)


def render_morning(first: str) -> str:
    return ck.render_morning(NIGHT, first)


def render_history(entries_by_day: dict[date, dict], days: list[date], ritual_days, today: date,
                   *, detail: list[dict]) -> str:
    return ck.render_history(_with_days(ritual_days), entries_by_day, days, today, detail=detail)


def closing_line(llm, entry: dict) -> str:
    return ck.closing_line(NIGHT, llm, entry)


def condense_voice_answer(llm, step: str, transcript: str) -> str:
    return ck.condense_voice_answer(NIGHT, llm, step, transcript)


def summarize_period(llm, entries: list[dict], period_name: str) -> ck.ReportSummary:
    return ck.summarize_period(NIGHT, llm, entries, period_name)


def period_stats(entries_by_day: dict[date, dict], start: date, end: date, ritual_days, today: date) -> ck.PeriodStats:
    return ck.period_stats(_with_days(ritual_days), entries_by_day, start, end, today)


def current_streak(store: NightStore, owner: str, end: date, ritual_days, max_days: int = 400) -> int:
    return ck.current_streak(_with_days(ritual_days), store, owner, end, max_days)


def trend_lines(trend: ck.Trend, kind: str) -> list[str]:
    return ck.trend_lines(NIGHT, trend, kind)


def report_period(kind: str, report_day: date) -> tuple[date, date]:
    return ck.report_period(kind, report_day, NIGHT.reports[kind].period)


def reports_due(report_day: date) -> list[str]:
    return ck.reports_due(NIGHT, report_day)


def build_report(llm, store: NightStore, owner: str, kind: str, report_day: date, ritual_days) -> tuple[str, str]:
    return ck.build_report(_with_days(ritual_days), llm, store, owner, kind, report_day)
