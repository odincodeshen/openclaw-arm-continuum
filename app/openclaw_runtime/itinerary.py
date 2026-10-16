"""Spot a dated plan -- a train or flight, a hotel stay, a ticketed event, an
appointment -- in the text read from a photo, so the bot can offer to put it
on the /mem schedule (docs/TRACKER_MEMORY.md, "Plans from photos").

Nothing is added on its own: the bot shows what it found with an
"Add to schedule" button, and only a tap writes the /mem items.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from openclaw_runtime.message_cards import RULE, esc


MAX_PLANS = 3
MAX_TEXT_CHARS = 6000
MAX_TITLE_CHARS = 120
KEEP_SUGGESTIONS_DAYS = 14
TAG = "plan"

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "is_plan": {"type": "boolean"},
        "plans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"date": {"type": "string"}, "title": {"type": "string"}},
                "required": ["date", "title"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_plan", "plans"],
    "additionalProperties": False,
}

_PROMPT = """Below is the text read from a picture someone saved. Today is {today}.

Is it a booking or ticket for something they will do on a set date -- a train, coach, ferry or flight, a hotel or other stay, a ticketed event, or an appointment?
Receipts for things already done, invoices, articles, menus, timetables and screenshots that merely mention a date are NOT plans.

If it is a plan, list each dated part once, at most {max_plans}:
- date: YYYY-MM-DD (for a stay, the check-in date)
- title: one short line in the text's own language naming what and where, with the departure time or the booking reference if there is one, e.g. "Train Cambridge -> Manchester Piccadilly 09:15" or "Hotel: Premier Inn Manchester, 2 nights (ref 9VFVRE)"
Each part keeps its own date, places and time. A return ticket has two parts: the outbound and the return. A date belongs only to the part it heads (e.g. "Return Journey: ... Travel on Thursday 15 October" dates the return legs listed after it, not the departures above it). If the text gives no date for a part -- say a screenshot cut off the outbound date -- leave that part out: never give it another part's date, and never give a part another part's time or direction.
If it is not a plan, return is_plan false and no plans.

Text:
{text}"""


@dataclass(frozen=True)
class Plan:
    day: date
    title: str

    def to_json(self) -> dict:
        return {"date": self.day.isoformat(), "title": self.title}

    @classmethod
    def from_json(cls, data: dict) -> "Plan":
        return cls(date.fromisoformat(data["date"]), str(data["title"]))


_MONTHS = ["january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december"]


def mentions_date(text: str, day: date) -> bool:
    """Whether ``text`` writes ``day`` out somewhere -- "15 October",
    "Thu 15 Oct", "October 15th", "2026-10-15", "15/10", "10/15", "10月15日".
    The model fills in a date the text doesn't give (a screenshot that cut
    the outbound date off gets "today"); a plan whose date isn't in the
    text is dropped."""
    month = _MONTHS[day.month - 1]
    names = rf"(?:{month}|{month[:3]}{'|sept' if day.month == 9 else ''})"
    d, m = rf"0?{day.day}", rf"0?{day.month}"
    patterns = [
        rf"\b{d}(?:st|nd|rd|th)?\s*(?:of\s+)?{names}\b",
        rf"\b{names}\.?\s*{d}(?:st|nd|rd|th)?\b",
        rf"\b{day.year}[-/.]{m}[-/.]{d}\b",
        rf"(?<![\d/.-]){d}[/.-]{m}(?![\d])",
        rf"(?<![\d/.-]){m}[/.-]{d}(?![\d])",
        rf"(?<!\d){m}\s*月\s*{d}\s*[日号號]",
    ]
    return any(re.search(pattern, text or "", re.IGNORECASE) for pattern in patterns)


def parse_plans(data: dict, today: date, text: str | None = None) -> list[Plan]:
    """The model's answer -> plans worth offering: valid dates from today on
    (and, given the source ``text``, written in it), one per (date, title),
    soonest first, at most MAX_PLANS."""
    if not isinstance(data, dict) or not data.get("is_plan"):
        return []
    plans: list[Plan] = []
    for item in data.get("plans") or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:MAX_TITLE_CHARS]
        try:
            day = date.fromisoformat(str(item.get("date") or "").strip())
        except ValueError:
            continue
        if not title or day < today or (text is not None and not mentions_date(text, day)):
            continue
        plan = Plan(day, title)
        if plan not in plans:
            plans.append(plan)
    return sorted(plans, key=lambda plan: plan.day)[:MAX_PLANS]


def none_reason(data: dict, today: date, text: str | None = None) -> str:
    """Why parse_plans found nothing to offer, for the log: not_a_plan,
    no_dates, past (every date before today), not_in_text (no upcoming date
    is written in ``text``) or invalid."""
    if not isinstance(data, dict) or not data.get("is_plan"):
        return "not_a_plan"
    items = [item for item in data.get("plans") or [] if isinstance(item, dict)]
    if not items:
        return "no_dates"
    days = []
    for item in items:
        try:
            days.append(date.fromisoformat(str(item.get("date") or "").strip()))
        except ValueError:
            pass
    if days and all(day < today for day in days):
        return "past"
    if text is not None and days and not any(mentions_date(text, day) for day in days if day >= today):
        return "not_in_text"
    return "invalid"


def ask_model(llm, text: str, today: date) -> dict:
    """The model's raw answer to "is ``text`` a dated plan?" ({} for no
    text). Raises on a model or JSON error; the caller decides that means
    "no button"."""
    text = (text or "").strip()
    if not text:
        return {}
    prompt = _PROMPT.format(today=today.isoformat(), max_plans=MAX_PLANS, text=text[:MAX_TEXT_CHARS])
    raw = llm.chat_json(prompt, PLAN_SCHEMA, schema_name="plan_from_photo", max_tokens=400, persona=False)
    return json.loads(raw)


def detect_plans(llm, text: str, today: date) -> list[Plan]:
    """Ask the model whether ``text`` is a dated plan; the plans worth offering."""
    return parse_plans(ask_model(llm, text, today), today, text or "")


def mem_command(plan: Plan) -> str:
    return f"/mem {plan.title} due:{plan.day.isoformat()} tag:{TAG}"


def render_card(plans: list[Plan], footer: str = "") -> str:
    lines = [f"<b>【加入行程？】</b>\n{RULE}", "This looks like a plan. Add it to the schedule?", ""]
    lines += [f"• <b>{plan.day.strftime('%a %d %b')}</b> · {esc(plan.title)}" for plan in plans]
    if footer:
        lines += ["", f"<i>{esc(footer)}</i>"]
    return "\n".join(lines)


class PlanSuggestions:
    """Offered plans waiting for a tap, keyed by a short id that fits in a
    button's callback data. Kept KEEP_SUGGESTIONS_DAYS days."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, chat_id: int, plans: list[Plan]) -> str:
        with self.lock:
            data = self._load()
            cutoff = time.time() - KEEP_SUGGESTIONS_DAYS * 86400
            data = {key: value for key, value in data.items() if value.get("created_at", 0) >= cutoff}
            suggestion_id = secrets.token_hex(4)
            data[suggestion_id] = {"chat_id": chat_id, "plans": [plan.to_json() for plan in plans],
                                   "created_at": int(time.time()), "status": "offered"}
            self._save(data)
            return suggestion_id

    def get(self, suggestion_id: str) -> dict | None:
        with self.lock:
            return self._load().get(suggestion_id)

    def set_status(self, suggestion_id: str, status: str) -> None:
        with self.lock:
            data = self._load()
            if suggestion_id in data:
                data[suggestion_id]["status"] = status
                self._save(data)
