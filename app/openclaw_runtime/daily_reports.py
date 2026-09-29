"""Daily report bodies for the per-bot morning pushes, run through /cron:

- ``/mem upcoming [days]`` -- tracker items (``/mem ... due:YYYY-MM-DD``) due
  today through the next few days, grouped by day.
- ``/rag digest`` -- documents added to the knowledge base and every Category
  RAG collection yesterday, one sentence each.

Both always return a report, even an empty one, so the morning push arrives
every day. Plain text, laid out like the English bot's cards (short Chinese
title, rule, English labels) -- skill answers reach Telegram as plain text.
"Today"/"yesterday" are dates in OPENCLAW_CRON_TIMEZONE, the same clock the
/cron schedule runs on.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.message_cards import RULE
from openclaw_runtime.qdrant_client import QdrantClient

UPCOMING_DEFAULT_DAYS = 3
KNOWLEDGE_DIGEST_MAX_DOCS = 10
WEEKLY_DIGEST_MAX_DOCS = 20
SUMMARY_SOURCE_CHARS = 4000


def local_today(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(timezone_name or "UTC")).date()


def _day_label(day: date, today: date) -> str:
    name = day.strftime("%a %d %b")
    if day == today:
        return f"Today · {name}"
    if day == today + timedelta(days=1):
        return f"Tomorrow · {name}"
    return name


def render_upcoming(items: list[dict], today: date, days: int = UPCOMING_DEFAULT_DAYS) -> str:
    """``items`` are tracker payloads; only those due from today up to
    ``days`` calendar days (today included) are listed, grouped by day."""
    last_day = today + timedelta(days=days - 1)
    by_day: dict[date, list[dict]] = {}
    for item in items:
        try:
            due = date.fromisoformat(str(item.get("due") or ""))
        except ValueError:
            continue
        if today <= due <= last_day:
            by_day.setdefault(due, []).append(item)

    lines = [f"行程｜未來{days}天", RULE]
    if not by_day:
        lines += [
            f"No schedule in the next {days} days.",
            "",
            "Add one with: /mem <what> due:YYYY-MM-DD",
        ]
        return "\n".join(lines)
    for day in sorted(by_day):
        lines.append(_day_label(day, today))
        for item in by_day[day]:
            lines.append(f"• {str(item.get('text') or '').strip()}  #{item.get('short_id', '?')}")
        lines.append("")
    lines.append("Mark one done with: /mem done <id>")
    return "\n".join(lines)


@dataclass(frozen=True)
class NewDocument:
    source: str  # "Knowledge base" or the category's display name
    title: str
    text: str


def day_bounds(day: date, timezone_name: str) -> tuple[int, int]:
    """[start, end) of a local calendar day, as epoch seconds."""
    tz = ZoneInfo(timezone_name or "UTC")
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def documents_added_on(
    qdrant: QdrantClient, sources: list[tuple[str, str]], day: date, timezone_name: str, days: int = 1
) -> list[NewDocument]:
    """Documents (not chunks) first indexed on ``day`` (or in the ``days``
    days starting then). ``sources`` is (label, collection) pairs. A
    document's chunks share a file_sha256 (or file_name), so they're grouped
    back into one document, in chunk order."""
    since, _ = day_bounds(day, timezone_name)
    _, before = day_bounds(day + timedelta(days=days - 1), timezone_name)
    documents: list[NewDocument] = []
    for label, collection in sources:
        grouped: dict[str, list[dict]] = {}
        for point in qdrant.scroll_by_filters(collection, {}, limit=512, since=since, before=before):
            payload = point.get("payload") or {}
            key = str(payload.get("file_sha256") or payload.get("file_name") or point.get("id"))
            grouped.setdefault(key, []).append(payload)
        for chunks in grouped.values():
            chunks.sort(key=lambda payload: int(payload.get("chunk_index") or 0))
            first = chunks[0]
            title = str(first.get("doc_title") or first.get("file_name") or "Untitled").strip()
            text = "\n".join(str(chunk.get("text") or "") for chunk in chunks)
            documents.append(NewDocument(source=label, title=title, text=text[:SUMMARY_SOURCE_CHARS]))
    return documents


def summarize_document(llm: LlmClient, document: NewDocument, language: str) -> str:
    language_hint = f" Write it in {language}." if language else ""
    prompt = (
        "Summarize the key point of this saved note or document in exactly one "
        f"short sentence, as a reminder of what was learned.{language_hint} Reply "
        "with the sentence only.\n\n"
        f"Title: {document.title}\n\n{document.text}"
    )
    return " ".join(llm.chat(prompt, max_tokens=120).split())


def render_knowledge_digest(
    day: date, summaries: list[tuple[NewDocument, str]], more: int = 0, days: int = 1
) -> str:
    """days=1: yesterday's report. days=7: the week ending on ``day``."""
    if days == 1:
        title, period, empty = "知識｜昨日新增", day.strftime("%a %d %b"), "No new knowledge was added yesterday."
    else:
        first = day - timedelta(days=days - 1)
        title, empty = "知識｜本週新增", "No new knowledge was added this week."
        period = f"{first.strftime('%d %b')} – {day.strftime('%d %b')}"
    lines = [title, RULE, f"{period} · {len(summaries) + more} new"]
    if not summaries:
        lines += ["", empty]
        return "\n".join(lines)
    current_source = None
    for document, summary in summaries:
        if document.source != current_source:
            lines += ["", document.source]
            current_source = document.source
        lines.append(f"• {summary}")
    if more:
        lines += ["", f"…and {more} more."]
    return "\n".join(lines)
