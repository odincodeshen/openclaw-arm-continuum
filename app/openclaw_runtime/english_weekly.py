"""The English bot's end-of-week step, run with Sunday's push:

- a recap card per learner: which of Monday-Saturday were done, which of
  this week's chunks were learned, and (with the word list enabled) how many
  words were looked up;
- chunks that haven't stuck move into that learner's /vocab review queue, so
  the spaced-repetition schedule keeps bringing them back after the week ends.

A chunk counts as learned when it has a progress record whose needs_review
is False (the latest Monday/Friday/Saturday attempts were all right). A chunk
with no record at all was never practised, so it's treated as not learned.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from openclaw_runtime.daily_task_tracking import day_tag
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.message_cards import RULE, bold, esc
from openclaw_runtime.owned_records import read_owned_points
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.skills.english_bot import (
    read_chunk_progress,
    read_listening_check,
    read_this_week_chunks,
    read_this_week_payload,
)
from openclaw_runtime.vocabulary import add_for_review, list_word_list

PRACTICE_DAYS = [("mon", "Mon"), ("tue", "Tue"), ("wed", "Wed"), ("thu", "Thu"), ("fri", "Fri"), ("sat", "Sat")]
RECAP_TITLE = "週日｜本週回顧"


@dataclass(frozen=True)
class ChunkOutcome:
    phrase: str
    definition: str
    sentence: str  # the learner's own sentence if they made one, else the example
    learned: bool


def chunk_outcomes(qdrant: QdrantClient, collection: str, owner: str, week_number: int) -> list[ChunkOutcome]:
    outcomes = []
    for chunk in read_this_week_chunks(qdrant, collection, week_number):
        phrase = chunk["phrase"]
        progress = read_chunk_progress(qdrant, collection, owner, week_number, phrase)
        outcomes.append(
            ChunkOutcome(
                phrase=phrase,
                definition=chunk.get("definition") or "",
                sentence=(progress or {}).get("user_sentence") or chunk.get("context_sentence") or "",
                learned=bool(progress) and not progress.get("needs_review"),
            )
        )
    return outcomes


def practice_days(qdrant: QdrantClient, collection: str, owner: str, week_number: int) -> dict[str, str]:
    """day code -> "done" (answered), "missed" (pushed, not answered) or
    "none" (never pushed, e.g. the bot started mid-week)."""
    statuses = {}
    for code, _ in PRACTICE_DAYS:
        points = read_owned_points(
            qdrant, collection, owner, {"tag": day_tag(week_number, code), "kind": "daily_task"}, limit=8
        )
        if any((point.get("payload") or {}).get("completed") for point in points):
            statuses[code] = "done"
        else:
            statuses[code] = "missed" if points else "none"
    return statuses


def promote_unlearned_chunks(
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    owner: str,
    outcomes: list[ChunkOutcome],
    *,
    now: datetime | None = None,
) -> list[str]:
    promoted = []
    for outcome in outcomes:
        if outcome.learned:
            continue
        add_for_review(
            qdrant,
            embeddings,
            collection,
            owner,
            outcome.phrase,
            outcome.definition,
            outcome.sentence,
            source="weekly_chunk",
            now=now,
        )
        promoted.append(outcome.phrase)
    return promoted


def render_weekly_recap(
    week_number: int,
    days: dict[str, str],
    outcomes: list[ChunkOutcome],
    *,
    promoted: list[str],
    words_this_week: int | None = None,
    words_total: int | None = None,
    listening: dict | None = None,
    listening_expected: bool = False,
) -> str:
    marks = {"done": "✅", "missed": "❌", "none": "—"}
    day_line = "　".join(f"{label} {marks[days.get(code, 'none')]}" for code, label in PRACTICE_DAYS)
    done = sum(1 for status in days.values() if status == "done")
    lines = [
        f"<b>{esc(RECAP_TITLE)}</b> · Week {week_number}",
        RULE,
        bold("Practice"),
        day_line,
        f"{done} of {len(PRACTICE_DAYS)} days done",
        "",
    ]
    if listening_expected:
        lines.append(bold("Listening"))
        if listening:
            gist = "✅" if listening.get("gist_correct") else "❌"
            total = int(listening.get("dictation_total") or 0)
            dictation = f" · Dictation {int(listening.get('dictation_correct') or 0)}/{total}" if total else ""
            lines.append(f"Gist {gist}{dictation}")
        else:
            lines.append("Not answered this week")
        lines.append("")
    lines.append(bold("Chunks"))
    for outcome in outcomes:
        if outcome.learned:
            lines.append(f"✅ {esc(outcome.phrase)}")
        elif outcome.phrase in promoted:
            lines.append(f"❌ {esc(outcome.phrase)} — added to your word review")
        else:
            lines.append(f"❌ {esc(outcome.phrase)} — worth another look")
    if words_total is not None:
        lines += ["", bold("Words"), f"{words_this_week or 0} looked up this week · {words_total} in your list"]
    if promoted:
        lines += ["", "Chunks you haven't got yet come back in /vocab review from tomorrow."]
    return "\n".join(lines)


def build_weekly_recap(
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    owner: str,
    week_number: int,
    *,
    vocab_enabled: bool,
    now: datetime | None = None,
) -> str:
    """Compute one learner's week, move unlearned chunks into their review
    queue when the word list is enabled, and return the recap card."""
    now = now or datetime.now(timezone.utc)
    outcomes = chunk_outcomes(qdrant, collection, owner, week_number)
    days = practice_days(qdrant, collection, owner, week_number)
    try:
        listening_expected = bool(read_this_week_payload(qdrant, collection, week_number).get("listening_check"))
    except ValueError:
        listening_expected = False
    listening = read_listening_check(qdrant, collection, owner, week_number) if listening_expected else None
    promoted: list[str] = []
    words_this_week = words_total = None
    if vocab_enabled:
        promoted = promote_unlearned_chunks(qdrant, embeddings, collection, owner, outcomes, now=now)
        entries = list_word_list(qdrant, collection, owner)
        since = (now - timedelta(days=7)).isoformat()
        looked_up = [e for e in entries if e.get("source") != "weekly_chunk"]
        words_total = len(entries)
        words_this_week = sum(1 for e in looked_up if (e.get("added_at") or "") >= since)
    return render_weekly_recap(
        week_number,
        days,
        outcomes,
        promoted=promoted,
        words_this_week=words_this_week,
        words_total=words_total,
        listening=listening,
        listening_expected=listening_expected,
    )
