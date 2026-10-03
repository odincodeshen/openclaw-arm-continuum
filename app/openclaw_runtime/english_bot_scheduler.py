"""Gateway-side wiring for the English-learning bot (bot4, lc9_dgx4_en):
day-of-week dispatch, daily push/sweep due-checking, and reply dispatch.

Kept separate from app/openclaw_runtime/skills/english_bot.py (pure,
already-tested per-day logic) -- this module is the "which day is it,
call the right function, remember what's pending" glue, a different
concern. Keeps a small local JSON state file for the daily due-check
(same load_json/write_json shape as app/openclaw_cron_worker.py), since
this is operational scheduler state (did today's push/sweep already run),
not English-learning content -- that stays in Qdrant, per spec Section 0
point 1.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from openclaw_runtime.audio_clip_client import AudioClipClient
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.english_weekly import build_weekly_recap
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.logsafe import redact_ids
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.skills.english_bot import (
    evaluate_friday_reply,
    evaluate_monday_reply,
    evaluate_saturday_answers,
    evaluate_thursday_reply,
    evaluate_tuesday_reply,
    evaluate_wednesday_reply,
    next_week_number,
    read_this_week_chunks,
    run_daily_completion_sweep,
    run_friday_task,
    run_monday_task,
    run_saturday_task,
    run_sunday_task,
    run_thursday_task,
    run_tuesday_task,
    run_wednesday_task,
    workspace_dir_for_week,
)
from openclaw_runtime.transcription_client import TranscriptionClient


DAY_CODES_BY_WEEKDAY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def day_code_for(now: datetime) -> str:
    return DAY_CODES_BY_WEEKDAY[now.weekday()]


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _today_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _is_due(now: datetime, due_time: str, *, window_minutes: int = 30) -> bool:
    """Fires once the clock passes due_time, stays "due" for window_minutes so a delayed
    poll tick still catches it, then stops being due (today's date-key
    dedup, set by the caller, is what actually prevents a double-fire)."""
    hour_text, minute_text = due_time.split(":", 1)
    due_hour, due_minute = int(hour_text), int(minute_text)
    now_minutes = now.hour * 60 + now.minute
    due_minutes = due_hour * 60 + due_minute
    return 0 <= now_minutes - due_minutes <= window_minutes


def should_push_today(now: datetime, push_time: str, state: dict) -> bool:
    return _is_due(now, push_time) and state.get("last_push_date") != _today_key(now)


def mark_pushed_today(now: datetime, state: dict) -> None:
    state["last_push_date"] = _today_key(now)


# Off-peak preparation: the day's push is generated in a cheap-electricity
# window (default 04:00-05:30) and only delivered at the usual push time.
PREPARE_WINDOW_MINUTES = 90


def should_prepare_today(now: datetime, prepare_time: str, state: dict) -> bool:
    """Once a day, anywhere inside the preparation window. Callers mark the
    attempt before running it, so a failure is not retried (retrying could
    repeat the half-done side effects) -- the push just falls back to
    generating live at push time."""
    return bool(prepare_time) and _is_due(now, prepare_time, window_minutes=PREPARE_WINDOW_MINUTES) and (
        state.get("last_prepare_date") != _today_key(now)
    )


def mark_prepare_attempted(now: datetime, state: dict) -> None:
    state["last_prepare_date"] = _today_key(now)


def prepared_for(now: datetime, day_code: str, state: dict) -> list[dict] | None:
    """Today's prepared outbox for this day code, or None to generate live."""
    prepared = state.get("prepared") or {}
    if prepared.get("date") == _today_key(now) and prepared.get("day_code") == day_code:
        return list(prepared.get("items") or [])
    return None


def store_prepared(now: datetime, day_code: str, items: list[dict], state: dict) -> None:
    state["prepared"] = {"date": _today_key(now), "day_code": day_code, "items": items}


def clear_prepared(state: dict) -> None:
    state.pop("prepared", None)


def prepare_todays_push(**push_kwargs) -> list[dict]:
    """Run today's push with everything that would reach a learner captured
    instead of sent: messages, audio clips, recap cards, and the pending
    answer that opens the task. All the generation (LLM, transcription,
    audio clipping) and bookkeeping happens now; deliver_outbox() replays
    the captured items, in order, at push time."""
    items: list[dict] = []
    run_todays_push(
        send_message=lambda owner, text: items.append({"type": "message", "owner": owner, "text": text}),
        send_audio=lambda owner, path, caption: items.append(
            {"type": "audio", "owner": owner, "path": str(path), "caption": caption}
        ),
        send_report=lambda owner, html: items.append({"type": "report", "owner": owner, "text": html}),
        set_pending_answer=lambda owner, item: items.append({"type": "pending", "owner": owner, "item": item}),
        **push_kwargs,
    )
    return items


def deliver_outbox(
    items: list[dict],
    *,
    send_message: Callable[[str, str], None],
    send_audio: Callable[[str, Path, str], None],
    send_report: Callable[[str, str], None],
    set_pending_answer: Callable[[str, dict], None],
) -> None:
    for item in items:
        kind = item["type"]
        if kind == "message":
            send_message(item["owner"], item["text"])
        elif kind == "audio":
            send_audio(item["owner"], Path(item["path"]), item["caption"])
        elif kind == "report":
            send_report(item["owner"], item["text"])
        elif kind == "pending":
            set_pending_answer(item["owner"], item["item"])


def should_sweep_today(now: datetime, sweep_time: str, state: dict) -> bool:
    return _is_due(now, sweep_time) and state.get("last_sweep_date") != _today_key(now)


def mark_swept_today(now: datetime, state: dict) -> None:
    state["last_sweep_date"] = _today_key(now)


def run_todays_push(
    *,
    day_code: str,
    llm: LlmClient,
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    owners: list[str],
    send_message: Callable[[str, str], None],
    send_audio: Callable[[str, Path, str], None],
    set_pending_answer: Callable[[str, dict], None],
    clip_client: AudioClipClient,
    transcription_client: TranscriptionClient,
    workspace_root: Path,
    send_report: Callable[[str, str], None] | None = None,
    vocab_enabled: bool = False,
) -> None:
    """Dispatch to the correct run_<day>_task, then set the pending-answer
    payload for each owner (skipped for Sunday, which auto-completes and
    has no reply to wait for; skipped for Saturday, which already sets its
    own pending answer internally, built in v1.14).

    Only genuinely ephemeral, LLM-generated-at-push-time fields go into the
    pending payload (cue_card, part3_question, opener, reference_text) --
    anything durable (this week's chunks) is re-fetched from Qdrant by
    dispatch_pending_reply() via week_number instead of being duplicated
    here, per the confirmed wiring design.
    """
    if day_code == "mon":
        # workspace_dir needs a week_number before run_monday_task computes
        # its own (it decides internally, via the same next_week_number()
        # call) -- both calls agree since nothing else writes weekly_content
        # concurrently in this single-threaded scheduler loop.
        week_number = next_week_number(qdrant, collection)
        workspace_dir = workspace_dir_for_week(workspace_root, week_number)
        content = run_monday_task(
            clip_client=clip_client,
            transcription_client=transcription_client,
            llm=llm,
            qdrant=qdrant,
            embeddings=embeddings,
            collection=collection,
            owners=owners,
            send_message=send_message,
            workspace_dir=workspace_dir,
            send_audio=send_audio,
        )
        if content is None:
            return  # this week's episode was already processed -- nothing pushed
        for owner in owners:
            set_pending_answer(owner, {"kind": "eng_mon", "week_number": content.week_number})
        return

    # Tue-Sun all act on the week Monday already created this cycle.
    week_number = next_week_number(qdrant, collection) - 1
    if week_number < 1:
        return  # Monday hasn't run yet -- nothing for these days to act on

    if day_code == "tue":
        workspace_dir = workspace_dir_for_week(workspace_root, week_number)
        task = run_tuesday_task(
            week_number=week_number,
            clip_client=clip_client,
            llm=llm,
            qdrant=qdrant,
            embeddings=embeddings,
            collection=collection,
            owners=owners,
            send_message=send_message,
            send_audio=send_audio,
            workspace_dir=workspace_dir,
        )
        for owner in owners:
            set_pending_answer(
                owner,
                {"kind": "eng_tue", "week_number": week_number, "reference_text": task.stretch_text},
            )
    elif day_code == "wed":
        question = run_wednesday_task(
            llm=llm,
            qdrant=qdrant,
            embeddings=embeddings,
            collection=collection,
            week_number=week_number,
            owners=owners,
            send_message=send_message,
        )
        for owner in owners:
            payload = {"kind": "eng_wed", "week_number": week_number, "cue_card": question["cue_card"]}
            if question.get("part3_question"):
                payload["part3_question"] = question["part3_question"]
            set_pending_answer(owner, payload)
    elif day_code == "thu":
        opener = run_thursday_task(
            llm=llm,
            qdrant=qdrant,
            embeddings=embeddings,
            collection=collection,
            week_number=week_number,
            owners=owners,
            send_message=send_message,
        )
        for owner in owners:
            set_pending_answer(owner, {"kind": "eng_thu", "week_number": week_number, "opener": opener})
    elif day_code == "fri":
        run_friday_task(
            qdrant=qdrant,
            embeddings=embeddings,
            collection=collection,
            week_number=week_number,
            owners=owners,
            send_message=send_message,
        )
        for owner in owners:
            set_pending_answer(owner, {"kind": "eng_fri", "week_number": week_number})
    elif day_code == "sat":
        # run_saturday_task already calls set_pending_answer itself
        # (v1.14) -- don't set it again here.
        run_saturday_task(
            qdrant=qdrant,
            embeddings=embeddings,
            collection=collection,
            week_number=week_number,
            owners=owners,
            send_message=send_message,
            set_pending_answer=set_pending_answer,
        )
    elif day_code == "sun":
        # No pending answer for Sunday -- purely passive reading, already
        # marked completed inside run_sunday_task itself.
        run_sunday_task(
            llm=llm,
            qdrant=qdrant,
            embeddings=embeddings,
            collection=collection,
            week_number=week_number,
            owners=owners,
            send_message=send_message,
        )
        # End of the practice week: a recap card per learner, which also
        # moves chunks that haven't stuck into their /vocab review queue.
        # Sent through send_report, not send_message, so it doesn't repeat
        # the Word review reminder already on the reading card. One learner's
        # failure doesn't block the others' recaps -- and never re-raises,
        # since the Sunday push itself has already gone out.
        if send_report is not None:
            for owner in owners:
                try:
                    recap = build_weekly_recap(
                        qdrant, embeddings, collection, owner, week_number, vocab_enabled=vocab_enabled
                    )
                    send_report(owner, recap)
                except Exception as exc:  # noqa: BLE001
                    print(redact_ids(f"[english_bot] weekly recap failed owner={owner}: {exc}"), flush=True)


def run_todays_sweep(qdrant: QdrantClient, collection: str, owners: list[str]) -> dict[str, list[str]]:
    week_number = next_week_number(qdrant, collection) - 1
    if week_number < 1:
        return {}
    return run_daily_completion_sweep(qdrant, collection, week_number, owners)


def dispatch_pending_reply(
    *,
    pending: dict,
    transcribed_reply: str,
    reply_duration_seconds: float,
    llm: LlmClient,
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    owner: str,
) -> str:
    """Given the item popped from PENDING_ANSWER and a transcribed reply
    (from voice or typed directly -- callers don't need to distinguish),
    call the matching evaluate_<day>_reply and return its feedback text.
    Re-fetches shared/durable data (this week's chunks) from Qdrant via
    week_number rather than trusting it to have survived in the ephemeral
    pending payload."""
    kind = pending.get("kind")
    week_number = pending["week_number"]

    if kind == "eng_mon":
        chunks = read_this_week_chunks(qdrant, collection, week_number)
        return evaluate_monday_reply(
            qdrant, embeddings, llm, collection, owner, week_number, chunks, transcribed_reply
        )
    if kind == "eng_tue":
        return evaluate_tuesday_reply(
            llm,
            pending["reference_text"],
            transcribed_reply,
            reply_duration_seconds,
            qdrant=qdrant,
            collection=collection,
            owner=owner,
            week_number=week_number,
        )
    if kind == "eng_wed":
        return evaluate_wednesday_reply(
            llm,
            pending["cue_card"],
            transcribed_reply,
            qdrant=qdrant,
            collection=collection,
            owner=owner,
            week_number=week_number,
            part3_question=pending.get("part3_question"),
        )
    if kind == "eng_thu":
        return evaluate_thursday_reply(
            llm,
            pending["opener"],
            transcribed_reply,
            qdrant=qdrant,
            collection=collection,
            owner=owner,
            week_number=week_number,
        )
    if kind == "eng_fri":
        chunks = read_this_week_chunks(qdrant, collection, week_number)
        return evaluate_friday_reply(
            qdrant, embeddings, llm, collection, owner, week_number, chunks, transcribed_reply
        )
    if kind == "eng_saturday_quiz":
        return evaluate_saturday_answers(
            llm, qdrant, embeddings, collection, owner, week_number, pending["questions"], transcribed_reply
        )
    raise ValueError(f"unknown pending-answer kind: {kind!r}")
