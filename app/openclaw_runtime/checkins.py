"""Check-ins: short guided question flows defined in TOML, not in code.

A check-in asks a few questions one at a time on a schedule (e.g. a night
ritual at 22:30, a work log at 18:00), keeps each day's answers as a small
JSON file on the host, can bring one answer back the next morning and ask
the day after whether it got done, and sends weekly / monthly / yearly
reports whose wording comes from the local model. Everything that differs
between check-ins -- the questions, titles, days, times, which answer comes
back, which report sections the model writes -- lives in a TOML file
(examples/checkins/*.toml); this module is the engine: the spec, storage,
cards, model prompts and reports. The Telegram gateway runs the schedule
and the conversation.

Entries live under ``<data_dir>/<owner>/<YYYY-MM-DD>.json`` -- outside the
inbox, so they are never indexed, never searched by /rag, and never part of
the knowledge digest. Reports are written there as Markdown too.
"""

import json
import re
import tomllib
from dataclasses import dataclass, field, replace
from datetime import date, time, timedelta
from pathlib import Path

from openclaw_runtime.message_cards import RULE, bold, esc

WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
STATUS_MARKS = {"done": "✅", "partial": "◐", "missed": "❌", "open": "…"}
PERIODS = {"last_7_days", "week_to_date", "previous_month", "previous_year"}
REPORT_KINDS = ("week", "month", "year")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ID = re.compile(r"^[a-z][a-z0-9_]{1,23}$")


class SpecError(ValueError):
    pass


# --- the spec -----------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    id: str
    label: str
    prompt: str
    hint: str = ""
    list: bool = False  # several items in one answer, shown as bullets
    short: str = ""  # label in /<command> history (default: label)
    card: str = ""  # label on the closing card and in Markdown reports (default: label)
    shape: str = "one short sentence"  # what a long spoken answer is tidied into


@dataclass(frozen=True)
class FollowUp:
    """Before the first question, ask whether an earlier answer got done."""
    source: str  # question id
    title: str
    label: str
    intro: str
    prompt: str
    report_label: str  # e.g. "First things"
    short: str  # e.g. "First"
    md_label: str = "First thing done"  # in Markdown reports


@dataclass(frozen=True)
class Morning:
    source: str
    time: str
    title: str
    label: str
    intro: str
    outro: str


@dataclass(frozen=True)
class Section:
    name: str
    sources: tuple[str, ...]
    ask: str
    items: bool = False  # a list of short items rather than one sentence

    @property
    def key(self) -> str:
        return re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_") or "section"


@dataclass(frozen=True)
class ReportPlan:
    kind: str  # week / month / year
    time: str
    title: str
    period: str
    day: int | None = None  # weekday for "week"


@dataclass(frozen=True)
class CheckinSpec:
    id: str
    title: str
    command: str
    owner_env: str
    days: frozenset[int]
    start: str
    reminders: tuple[str, ...]
    deadline: str
    questions: tuple[Question, ...]
    unit: str = "day"  # "night" -> "5 of 6 nights"
    subject: str = "Today's check-in"  # "Tonight's wind-down is still open"
    reminder_title: str = "提醒"
    last_call_title: str = "最後提醒"
    last_call_note: str = "Replies count until the deadline; after that today is saved as it is."
    history_title: str = ""
    follow_up: FollowUp | None = None
    morning: Morning | None = None
    closing_title: str = "完成"
    closing_prompt: str = ""  # empty: no model line on the closing card
    closing_fallback: str = ""
    schedule_from: str = ""  # question id for "Add to tomorrow's schedule"; empty = no button
    voice_context: str = "a journal question"
    reports: dict[str, ReportPlan] = field(default_factory=dict)
    report_heading: str = "Check-in"  # Markdown report title prefix
    report_subject: str = "check-in notes"  # what the model is told it's reading
    report_closing: str = ""  # instruction for the report's closing line; empty = none
    sections: tuple[Section, ...] = ()
    prepare: str = "04:00"
    timezone: str = ""  # empty: the bot's OPENCLAW_CRON_TIMEZONE
    data_dir: str = ""  # empty: /workspace/.openclaw/checkins/<id>

    @property
    def steps(self) -> list[str]:
        return [q.id for q in self.questions]

    def question(self, step: str) -> Question:
        for q in self.questions:
            if q.id == step:
                return q
        raise KeyError(step)

    @property
    def done_field(self) -> str:
        return f"{self.follow_up.source}_done" if self.follow_up else ""


def _hhmm(value, where: str) -> str:
    value = str(value or "").strip()
    if not _HHMM.match(value):
        raise SpecError(f"{where}: expected HH:MM, got {value!r}")
    return value


def _days(values, where: str) -> frozenset[int]:
    days = set()
    for value in values or []:
        code = str(value).strip().lower()[:3]
        if code not in WEEKDAY_CODES:
            raise SpecError(f"{where}: unknown day {value!r}")
        days.add(WEEKDAY_CODES.index(code))
    if not days:
        raise SpecError(f"{where}: at least one day is needed")
    return frozenset(days)


def parse_spec(text: str, *, source: str = "check-in") -> CheckinSpec:
    """Parse and validate one check-in TOML file."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SpecError(f"{source}: {exc}") from exc
    head = raw.get("checkin") or {}
    checkin_id = str(head.get("id") or "").strip()
    if not _ID.match(checkin_id):
        raise SpecError(f"{source}: [checkin] id must be 2-24 lowercase letters, digits or _")
    title = str(head.get("title") or "").strip()
    if not title:
        raise SpecError(f"{source}: [checkin] title is required")
    sched = raw.get("schedule") or {}
    questions = []
    for index, q in enumerate(raw.get("question") or []):
        qid = str(q.get("id") or "").strip()
        if not _ID.match(qid):
            raise SpecError(f"{source}: question {index + 1} needs an id")
        if not q.get("label") or not q.get("prompt"):
            raise SpecError(f"{source}: question {qid!r} needs label and prompt")
        questions.append(Question(
            id=qid, label=str(q["label"]), prompt=str(q["prompt"]), hint=str(q.get("hint", "")),
            list=bool(q.get("list", False)), short=str(q.get("short", "")) or str(q["label"]),
            card=str(q.get("card", "")) or str(q["label"]),
            shape=str(q.get("shape", "")) or ("1-3 short items, one per line" if q.get("list") else "one short sentence"),
        ))
    if not questions:
        raise SpecError(f"{source}: at least one [[question]] is needed")
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise SpecError(f"{source}: question ids must be unique")

    def known(qid: str, where: str) -> str:
        if qid not in ids:
            raise SpecError(f"{source}: {where} refers to unknown question {qid!r}")
        return qid

    follow = raw.get("follow_up")
    follow_up = None
    if follow:
        follow_up = FollowUp(
            source=known(str(follow.get("from", "")), "[follow_up] from"),
            title=str(follow.get("title", "昨天的第一件事")), label=str(follow.get("label", "Yesterday's first thing")),
            intro=str(follow.get("intro", "You planned to start with:")), prompt=str(follow.get("prompt", "Did you get to it?")),
            report_label=str(follow.get("report_label", "First things")), short=str(follow.get("short", "First")),
            md_label=str(follow.get("md_label", "First thing done")),
        )
    morn = raw.get("morning")
    morning = None
    if morn:
        morning = Morning(
            source=known(str(morn.get("from", "")), "[morning] from"), time=_hhmm(morn.get("time"), "[morning] time"),
            title=str(morn.get("title", "今天的第一件事")), label=str(morn.get("label", "First thing today")),
            intro=str(morn.get("intro", "You wrote last night:")), outro=str(morn.get("outro", "Start here.")),
        )
    closing = raw.get("closing") or {}
    schedule_from = str(closing.get("schedule_button", "") or "")
    if schedule_from:
        known(schedule_from, "[closing] schedule_button")
    report_raw = raw.get("reports") or {}
    reports: dict[str, ReportPlan] = {}
    defaults = {"week": "last_7_days", "month": "previous_month", "year": "previous_year"}
    for kind in REPORT_KINDS:
        plan = report_raw.get(kind)
        if not plan:
            continue
        period = str(plan.get("period", defaults[kind]))
        if period not in PERIODS:
            raise SpecError(f"{source}: reports.{kind} period must be one of {sorted(PERIODS)}")
        day = None
        if kind == "week":
            day = min(_days([plan.get("day", "mon")], f"reports.{kind} day"))
        default_title = title + {"week": "週報", "month": "月報", "year": "年報"}[kind]
        reports[kind] = ReportPlan(kind, _hhmm(plan.get("time", "07:30"), f"reports.{kind} time"),
                                   str(plan.get("title", default_title)), period, day)
    sections = []
    for s in report_raw.get("section") or []:
        sources = tuple(known(str(x), f"section {s.get('name')!r}") for x in (s.get("from") or []))
        if not s.get("name") or not sources or not s.get("ask"):
            raise SpecError(f"{source}: each [[reports.section]] needs name, from and ask")
        sections.append(Section(str(s["name"]), sources, str(s["ask"]), bool(s.get("items", False))))
    deadline = str(sched.get("deadline", "00:00"))
    spec = CheckinSpec(
        id=checkin_id, title=title, command=str(head.get("command", checkin_id)).lstrip("/"),
        owner_env=str(head.get("owner_env", "OPENCLAW_CHECKIN_OWNER")),
        days=_days(sched.get("days"), "[schedule] days"), start=_hhmm(sched.get("start"), "[schedule] start"),
        reminders=tuple(_hhmm(t, "[schedule] reminders") for t in sched.get("reminders") or []),
        deadline=_hhmm(deadline, "[schedule] deadline"), questions=tuple(questions),
        unit=str(head.get("unit", "day")), subject=str(head.get("subject", "Today's check-in")),
        reminder_title=str(sched.get("reminder_title", "提醒")), last_call_title=str(sched.get("last_call_title", "最後提醒")),
        last_call_note=str(sched.get("last_call_note",
                                     "Replies count until the deadline; after that today is saved as it is.")),
        history_title=str(head.get("history_title", "") or f"{title}紀錄"),
        follow_up=follow_up, morning=morning,
        closing_title=str(closing.get("title", "完成")), closing_prompt=str(closing.get("model_line", "")),
        closing_fallback=str(closing.get("fallback", "")), schedule_from=schedule_from,
        voice_context=str(head.get("voice_context", "a journal question")),
        reports=reports, report_heading=str(report_raw.get("heading", title)),
        report_subject=str(report_raw.get("subject", "check-in notes")), report_closing=str(report_raw.get("closing", "")),
        sections=tuple(sections), prepare=_hhmm(report_raw.get("prepare", "04:00"), "reports.prepare"),
        timezone=str(head.get("timezone", "")), data_dir=str(head.get("data_dir", "")),
    )
    for at in spec.reminders:
        if at <= spec.start:
            raise SpecError(f"{source}: reminder {at} is not after the start {spec.start}")
    return spec


def load_specs(folder: Path) -> tuple[list[CheckinSpec], list[str]]:
    """Every *.toml in folder -> (specs, problems). A bad file is reported,
    not fatal, so one typo doesn't stop the bot."""
    specs, problems = [], []
    if not folder.is_dir():
        return specs, problems
    seen: set[str] = set()
    for path in sorted(folder.glob("*.toml")):
        try:
            spec = parse_spec(path.read_text(encoding="utf-8"), source=path.name)
        except (SpecError, OSError) as exc:
            problems.append(str(exc))
            continue
        if spec.id in seen or spec.command in seen:
            problems.append(f"{path.name}: id or command {spec.id!r} is used twice")
            continue
        seen.update({spec.id, spec.command})
        specs.append(spec)
    return specs, problems


def with_overrides(spec: CheckinSpec, **values) -> CheckinSpec:
    return replace(spec, **{k: v for k, v in values.items() if v not in (None, "")})


# --- small helpers ------------------------------------------------------------


def parse_days(text: str) -> set[int]:
    """"sun,mon,tue" -> {6, 0, 1} (Python weekday numbers)."""
    return {WEEKDAY_CODES.index(p.strip()[:3]) for p in (text or "").lower().split(",") if p.strip()[:3] in WEEKDAY_CODES}


def next_step(spec: CheckinSpec, entry: dict) -> str | None:
    answers = entry.get("answers") or {}
    for step in spec.steps:
        if not answers.get(step):
            return step
    return None


def split_items(text: str) -> list[str]:
    """A list answer comes as one message; show it as bullets when it was
    written as separate lines or a numbered/semicolon list."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"\n+|[；;]|(?:^|\s)\d+[.)、]\s*", text)
    items = [part.strip(" ,，、-•") for part in parts]
    return [item for item in items if item] or [text]


def new_entry(day: date, now_iso: str) -> dict:
    return {"date": day.isoformat(), "status": "open", "answers": {}, "started_at": now_iso}


def close_status(entry: dict) -> str:
    """Status for a day that reached the deadline unfinished."""
    return "partial" if any((entry.get("answers") or {}).values()) else "missed"


def deadline_passed(spec: CheckinSpec, day: date, now_date: date, now_time: time) -> bool:
    """Is an open entry for `day` past its deadline? "00:00" = midnight."""
    if now_date > day:
        return True
    if spec.deadline == "00:00" or now_date < day:
        return False
    hour, minute = (int(x) for x in spec.deadline.split(":"))
    return now_time >= time(hour, minute)


# --- storage ------------------------------------------------------------------


class CheckinStore:
    def __init__(self, root: Path, follow_source: str = "first", noun: str = "entry") -> None:
        self.root = root
        self.follow_source = follow_source
        self.noun = noun  # in messages: "no night recorded on ..."

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
        """The most recent earlier entry (within lookback_days) whose
        follow-up answer hasn't been marked done / not done yet."""
        for back in range(1, lookback_days + 1):
            entry = self.load(owner, day - timedelta(days=back))
            if entry is None:
                continue
            if (entry.get("answers") or {}).get(self.follow_source) and entry.get(f"{self.follow_source}_done") is None:
                return entry
            return None  # only the latest earlier entry is asked about
        return None

    def move(self, owner: str, source: date, target: date) -> dict:
        """Re-date an entry. Refuses to overwrite an existing one."""
        entry = self.load(owner, source)
        if entry is None:
            raise ValueError(f"no {self.noun} recorded on {source.isoformat()}")
        if self.load(owner, target) is not None:
            article = "an" if self.noun[:1] in "aeiou" else "a"
            raise ValueError(f"{target.isoformat()} already has {article} {self.noun}")
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


# --- cards --------------------------------------------------------------------


def title_line(zh: str, label: str) -> str:
    return f"<b>【{zh}】</b>· {label}\n{RULE}"


def day_label(day: date) -> str:
    return day.strftime("%a %d %b").replace(" 0", " ")


def render_question(spec: CheckinSpec, step: str) -> str:
    q = spec.question(step)
    lines = [title_line(f"{spec.title} {spec.steps.index(step) + 1}/{len(spec.steps)}", q.label), q.prompt]
    if q.hint:
        lines += ["", f"<i>{q.hint}</i>"]
    return "\n".join(lines)


def render_follow_up(spec: CheckinSpec, previous: dict) -> str:
    f = spec.follow_up
    answer = (previous.get("answers") or {}).get(f.source, "")
    return "\n".join([title_line(f.title, f.label), f.intro, bold(answer), "", f.prompt])


def render_follow_up_answered(spec: CheckinSpec, previous: dict, done: bool) -> str:
    f = spec.follow_up
    answer = (previous.get("answers") or {}).get(f.source, "")
    return "\n".join([title_line(f.title, f.label), bold(answer), "", "✅ Done" if done else "❌ Not yet"])


def render_entry_body(spec: CheckinSpec, entry: dict) -> list[str]:
    answers = entry.get("answers") or {}
    lines: list[str] = []
    for q in spec.questions:
        value = answers.get(q.id)
        if not value:
            continue
        if lines:
            lines.append("")
        lines.append(bold(q.card))
        if q.list:
            lines += [f"• {esc(item)}" for item in split_items(value)]
        else:
            lines.append(esc(value))
    return lines


def render_closing(spec: CheckinSpec, entry: dict, closing: str) -> str:
    lines = [title_line(spec.closing_title, day_label(date.fromisoformat(entry["date"])))]
    lines += render_entry_body(spec, entry)
    if closing:
        lines += ["", RULE, esc(closing)]
    return "\n".join(lines)


def render_reminder(spec: CheckinSpec, step: str | None, last: bool) -> str:
    if step == "check":
        where = "tap ✅ or ❌ on the card above to start"
    elif step:
        where = f"just pick up at {spec.steps.index(step) + 1}/{len(spec.steps)}"
    else:
        where = f"send /{spec.command} start to begin"
    if last:
        return "\n".join([title_line(spec.last_call_title, "Last call"),
                          f"{spec.subject} is still open — {where}.", spec.last_call_note])
    return "\n".join([title_line(spec.reminder_title, "Reminder"), f"{spec.subject} is still open — {where}."])


def render_morning(spec: CheckinSpec, answer: str) -> str:
    m = spec.morning
    return "\n".join([title_line(m.title, m.label), m.intro, bold(answer), "", m.outro])


def day_status(entry: dict | None, day: date, days: set[int] | frozenset[int], today: date) -> str | None:
    """done / partial / missed / open for a day, or None for a day with no
    check-in and no entry (e.g. a weekend for a work log)."""
    if entry:
        status = entry.get("status", "open")
        if status == "open" and day < today:
            return close_status(entry)
        return status
    if day.weekday() in days and day < today:
        return "missed"
    return None


def status_line(statuses: list[tuple[date, str]]) -> str:
    return "　".join(f"{day.strftime('%a')} {day.day} {STATUS_MARKS[status]}" for day, status in statuses)


def render_history(spec: CheckinSpec, entries_by_day: dict[date, dict], days: list[date], today: date,
                   *, detail: list[dict]) -> str:
    statuses = [
        (day, status) for day in days
        if (status := day_status(entries_by_day.get(day), day, spec.days, today)) is not None
    ]
    lines = [title_line(spec.history_title, f"/{spec.command}")]
    lines.append(status_line(statuses) if statuses else f"No {spec.unit}s recorded yet.")
    for entry in detail:
        answers = entry.get("answers") or {}
        if not any(answers.values()):
            continue
        lines += ["", bold(day_label(date.fromisoformat(entry["date"])))]
        for q in spec.questions:
            if answers.get(q.id):
                value = "；".join(split_items(answers[q.id])) if q.list else answers[q.id]
                lines.append(f"{q.short}: {esc(value)}")
        if spec.follow_up and entry.get(spec.done_field) is not None:
            lines.append(f"{spec.follow_up.short} done: {'✅' if entry[spec.done_field] else '❌'}")
    c = spec.command
    lines += ["", f"<i>/{c} 7d · /{c} 2026-09-28 · /{c} start · /{c} week · /{c} month · /{c} move</i>"]
    return "\n".join(lines)


# --- model prompts --------------------------------------------------------------


def closing_line(spec: CheckinSpec, llm, entry: dict) -> str:
    """The closing card's line from the local model ("" when the spec asks
    for none). Falls back to the spec's fixed line if the model fails."""
    if not spec.closing_prompt:
        return ""
    answers = entry.get("answers") or {}
    notes = "\n".join(f"{q.label}: {answers.get(q.id, '')}" for q in spec.questions)
    prompt = f"{spec.closing_prompt}\n\n{notes}"
    try:
        text = " ".join((llm.chat(prompt, max_tokens=120, persona=False) or "").split()).strip().strip('"“”')
    except Exception:  # noqa: BLE001 - the card still closes the day without it
        return spec.closing_fallback
    return text or spec.closing_fallback


CONDENSE_MIN_WORDS = 25


def condense_voice_answer(spec: CheckinSpec, llm, step: str, transcript: str) -> str:
    """A spoken answer tidied into the few words the card needs, in the
    speaker's own language; short or unusable ones come back unchanged."""
    text = " ".join(transcript.split())
    if len(text.split()) < CONDENSE_MIN_WORDS and len(text) < 80:
        return text
    q = spec.question(step)
    prompt = (
        f"This is a spoken answer to {spec.voice_context}, transcribed by speech recognition. Rewrite it as "
        f"{q.shape}, keeping the speaker's own language (do not "
        "translate), their meaning and any concrete details. Drop filler words and repetition. Reply with "
        "the rewritten answer only.\n\n"
        f"Question: {q.prompt}\nSpoken answer: {text}"
    )
    try:
        condensed = (llm.chat(prompt, max_tokens=160, persona=False) or "").strip().strip('"“”')
    except Exception:  # noqa: BLE001 - the transcript itself is still a fine answer
        return text
    return condensed or text


def report_schema(spec: CheckinSpec) -> dict:
    properties = {}
    for s in spec.sections:
        properties[s.key] = ({"type": "array", "items": {"type": "string"}, "maxItems": 5}
                             if s.items else {"type": "string"})
    if spec.report_closing:
        properties["closing"] = {"type": "string"}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


@dataclass(frozen=True)
class ReportSummary:
    sections: dict[str, object]  # section key -> list[str] or str
    closing: str


def summarize_period(spec: CheckinSpec, llm, entries: list[dict], period_name: str) -> ReportSummary:
    if not spec.sections:
        return ReportSummary({}, "")
    used = sorted({src for s in spec.sections for src in s.sources}, key=spec.steps.index)
    notes = []
    cap = 120 if len(entries) > 60 else 600  # a year's worth has to fit the model's context
    for entry in entries:
        answers = entry.get("answers") or {}
        if any(answers.get(q) for q in used):
            notes.append(f"{entry['date']}: " + " | ".join(f"{q}={answers.get(q, '')[:cap]}" for q in used))
    if not notes:
        return ReportSummary({}, "")
    lines = [f"These are one person's {spec.report_subject} for {period_name}. Write, in English only "
             "(translate from Chinese where needed):"]
    for s in spec.sections:
        lines.append(f"- {s.key} (from {', '.join(repr(x) for x in s.sources)}): {s.ask}")
    if spec.report_closing:
        lines.append(f"- closing: {spec.report_closing.replace('{period}', period_name)}")
    prompt = "\n".join(lines) + "\nOnly use what the notes say.\n\n" + "\n".join(notes)
    try:
        data = json.loads(llm.chat_json(prompt, report_schema(spec), schema_name=f"{spec.id}_report", max_tokens=600,
                                         persona=False))
    except Exception:  # noqa: BLE001 - the counts are still worth sending
        return ReportSummary({}, "")
    sections = {}
    for s in spec.sections:
        value = data.get(s.key)
        if s.items:
            sections[s.key] = [str(item).strip() for item in (value or []) if str(item).strip()][:5]
        else:
            sections[s.key] = str(value or "").strip()
    return ReportSummary(sections, str(data.get("closing", "")).strip())


@dataclass(frozen=True)
class PeriodStats:
    statuses: list[tuple[date, str]]
    done: int
    partial: int
    nights: int  # days the check-in was due (named for the night ritual)
    first_written: int  # follow-up answers given
    first_done: int
    first_not_done: int


def period_stats(spec: CheckinSpec, entries_by_day: dict[date, dict], start: date, end: date, today: date) -> PeriodStats:
    statuses = []
    day = start
    while day <= end:
        status = day_status(entries_by_day.get(day), day, spec.days, today)
        if status is not None:
            statuses.append((day, status))
        day += timedelta(days=1)
    entries = [entries_by_day[d] for d, _ in statuses if d in entries_by_day]
    source = spec.follow_up.source if spec.follow_up else ""
    firsts = [e for e in entries if source and (e.get("answers") or {}).get(source)]
    return PeriodStats(
        statuses=statuses,
        done=sum(1 for _, s in statuses if s == "done"),
        partial=sum(1 for _, s in statuses if s == "partial"),
        nights=len(statuses),
        first_written=len(firsts),
        first_done=sum(1 for e in firsts if e.get(spec.done_field) is True),
        first_not_done=sum(1 for e in firsts if e.get(spec.done_field) is False),
    )


@dataclass(frozen=True)
class Trend:
    streak: int
    done_rate: float | None
    previous_done_rate: float | None
    first_rate: float | None
    previous_first_rate: float | None


def _rate(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def follow_up_lookback(spec: CheckinSpec, day: date) -> int:
    """How far back the follow-up looks for the previous entry: to the
    previous check-in day, and at least 3 days (a daily check-in still finds
    Friday's entry on Monday; a weekly one finds last week's)."""
    for back in range(1, 8):
        if (day - timedelta(days=back)).weekday() in spec.days:
            return max(3, back)
    return 3


def current_streak(spec: CheckinSpec, store: CheckinStore, owner: str, end: date, max_days: int = 400) -> int:
    """Done days in a row, counting back from end over check-in days only."""
    streak = 0
    day = end
    for _ in range(max_days):
        if day.weekday() in spec.days:
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


def trend_lines(spec: CheckinSpec, trend: Trend, kind: str) -> list[str]:
    previous_name = {"week": "last week", "month": "last month", "year": "last year"}[kind]
    unit = spec.unit
    lines = [f"Streak: {trend.streak} {unit}{'s' if trend.streak != 1 else ''} in a row"]
    candidates = [_trend_line(f"{unit.capitalize()}s done", trend.done_rate, trend.previous_done_rate, previous_name)]
    if spec.follow_up:
        candidates.append(_trend_line(f"{spec.follow_up.report_label} done", trend.first_rate,
                                      trend.previous_first_rate, previous_name))
    return lines + [line for line in candidates if line]


def _period_label(start: date, end: date) -> str:
    return f"{start.day} {start.strftime('%b')} – {end.day} {end.strftime('%b')}"


def monthly_rates(stats: PeriodStats) -> list[str]:
    months: dict[int, list[str]] = {}
    for day, status in stats.statuses:
        months.setdefault(day.month, []).append(status)
    parts = [
        f"{date(2000, month, 1).strftime('%b')} {round(100 * sum(s == 'done' for s in found) / len(found))}%"
        for month, found in sorted(months.items())
    ]
    return [" · ".join(parts[i : i + 6]) for i in range(0, len(parts), 6)]


def _summary_lines(spec: CheckinSpec, summary: ReportSummary) -> list[str]:
    lines: list[str] = []
    for s in spec.sections:
        value = summary.sections.get(s.key)
        if not value:
            continue
        lines += ["", bold(s.name)]
        lines += [f"• {esc(item)}" for item in value] if isinstance(value, list) else [esc(value)]
    return lines


def render_report(spec: CheckinSpec, kind: str, start: date, end: date, stats: PeriodStats, summary: ReportSummary,
                  trend: Trend | None = None) -> str:
    plan = spec.reports.get(kind)
    zh = plan.title if plan else f"{spec.title}{kind}"
    label = {"week": _period_label(start, end), "month": start.strftime("%B %Y"), "year": str(start.year)}[kind]
    lines = [title_line(zh, label), bold("Done")]
    if kind == "week":
        lines.append(" ".join(f"{d.strftime('%a')} {STATUS_MARKS[s]}" for d, s in stats.statuses) or "—")
    if kind == "year":
        lines += monthly_rates(stats)
    done_text = f"{stats.done} of {stats.nights} {spec.unit}s"
    if stats.partial:
        done_text += f" · {stats.partial} partly"
    lines.append(done_text)
    if trend is not None:
        lines += ["", bold("Trend")] + [esc(line) for line in trend_lines(spec, trend, kind)]
    lines += _summary_lines(spec, summary)
    if spec.follow_up and stats.first_written:
        first = f"{stats.first_written} written · {stats.first_done} done"
        if stats.first_not_done:
            first += f" · {stats.first_not_done} not yet"
        lines += ["", bold(spec.follow_up.report_label), first]
    if summary.closing:
        lines += ["", RULE, esc(summary.closing)]
    return "\n".join(lines)


def render_report_markdown(spec: CheckinSpec, kind: str, start: date, end: date, stats: PeriodStats,
                           summary: ReportSummary, entries: list[dict], trend: Trend | None = None) -> str:
    title = {
        "week": f"{spec.report_heading} — week of " + _period_label(start, end),
        "month": f"{spec.report_heading} — " + start.strftime("%B %Y"),
        "year": f"{spec.report_heading} — {start.year}",
    }[kind]
    lines = [f"# {title}", "", f"- Done: {stats.done} of {stats.nights} {spec.unit}s"
             + (f" ({stats.partial} partly)" if stats.partial else "")]
    if spec.follow_up and stats.first_written:
        lines.append(f"- {spec.follow_up.report_label}: {stats.first_written} written, {stats.first_done} done, "
                     f"{stats.first_not_done} not yet")
    if trend is not None:
        lines += [f"- {line}" for line in trend_lines(spec, trend, kind)]
    if kind == "year":
        lines += [f"- Months: {line}" for line in monthly_rates(stats)]
    for s in spec.sections:
        value = summary.sections.get(s.key)
        if value:
            lines += ["", f"## {s.name}", ""] + ([f"- {item}" for item in value] if isinstance(value, list) else [value])
    if summary.closing:
        lines += ["", f"_{summary.closing}_"]
    lines += ["", f"## {spec.unit.capitalize()}s", ""]
    for entry in entries:
        answers = entry.get("answers") or {}
        lines.append(f"### {entry['date']} ({entry.get('status', '')})")
        lines.append("")
        for q in spec.questions:
            if answers.get(q.id):
                lines.append(f"- **{q.card}:** {answers[q.id]}")
        if spec.follow_up and entry.get(spec.done_field) is not None:
            lines.append(f"- **{spec.follow_up.md_label}:** {'yes' if entry[spec.done_field] else 'not yet'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def report_period(kind: str, report_day: date, period: str = "") -> tuple[date, date]:
    """The days a report sent on report_day covers. week: the seven days up
    to yesterday ("last_7_days"), or Monday to today ("week_to_date");
    month: the previous calendar month; year: the previous calendar year."""
    period = period or {"week": "last_7_days", "month": "previous_month", "year": "previous_year"}[kind]
    if period == "last_7_days":
        return report_day - timedelta(days=7), report_day - timedelta(days=1)
    if period == "week_to_date":
        return report_day - timedelta(days=report_day.weekday()), report_day
    if period == "previous_year":
        return date(report_day.year - 1, 1, 1), date(report_day.year - 1, 12, 31)
    last = report_day.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


def previous_period(kind: str, start: date, end: date, period: str) -> tuple[date, date]:
    if period == "week_to_date":
        return start - timedelta(days=7), end - timedelta(days=7)
    return report_period(kind, start, period)


def reports_due(spec: CheckinSpec, report_day: date) -> list[str]:
    kinds = []
    if "week" in spec.reports and report_day.weekday() == spec.reports["week"].day:
        kinds.append("week")
    if "month" in spec.reports and report_day.day == 1:
        kinds.append("month")
    if "year" in spec.reports and report_day.month == 1 and report_day.day == 1:
        kinds.append("year")
    return kinds


def report_name(kind: str, start: date) -> str:
    return {"week": f"week-{start.isoformat()}", "month": f"month-{start.strftime('%Y-%m')}",
            "year": f"year-{start.year}"}[kind]


def build_report(spec: CheckinSpec, llm, store: CheckinStore, owner: str, kind: str, report_day: date) -> tuple[str, str]:
    """(Telegram HTML, Markdown) for the report sent on report_day. The
    Markdown copy is also written under the owner's reports folder."""
    period = spec.reports[kind].period if kind in spec.reports else ""
    start, end = report_period(kind, report_day, period)
    entries = store.between(owner, start, end)
    by_day = {date.fromisoformat(e["date"]): e for e in entries}
    today = report_day + timedelta(days=1) if period == "week_to_date" else report_day
    stats = period_stats(spec, by_day, start, end, today)
    previous_start, previous_end = previous_period(kind, start, end, period)
    previous_entries = store.between(owner, previous_start, previous_end)
    previous = period_stats(spec, {date.fromisoformat(e["date"]): e for e in previous_entries},
                            previous_start, previous_end, today)
    trend = compute_trend(stats, previous, current_streak(spec, store, owner, end))
    summary = summarize_period(spec, llm, entries, kind)
    html = render_report(spec, kind, start, end, stats, summary, trend)
    markdown = render_report_markdown(spec, kind, start, end, stats, summary, entries, trend)
    store.write_report(owner, report_name(kind, start), markdown)
    return html, markdown
