"""Monday - Thursday task logic for the English-learning bot (bot4,
lc9_dgx4_en), per openclaw_eng_spec.md Section 0/2.1/2.2/2.3/2.4.

Not wired into the SkillRouter/skills.json -- these days are triggered by
time (a future /cron entry), not by a user's message text, so they don't
fit the existing can_handle(text)/run(text) Skill protocol. This module
exposes small, independently testable functions plus one orchestrator per
day; a later wiring step (cron + gateway) calls the orchestrators and
supplies real send_message/send_audio callables, owner chat_ids, and the
current week_number.

Weekly content (episode, chunks, transcript) is SHARED across the family --
written with plain qdrant.upsert_text(), not owned_records.write_owned_point(),
since that module deliberately requires an owner and this content has none
(see spec Section 0 point 2). Per-user completion tracking still goes
through daily_task_tracking, which is owner-scoped.
"""

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from openclaw_runtime.audio_clip_client import AudioClipClient
from openclaw_runtime.daily_task_tracking import mark_task_completed, mark_task_pushed, sweep_incomplete_to_skipped
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.http_client import get_bytes, get_text
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.owned_records import read_owned_points, write_owned_point
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.rss_client import RssItem, parse_rss_items
from openclaw_runtime.transcription_client import TranscriptionClient, TranscriptSegment


BBC_DESERT_ISLAND_DISCS_RSS = "https://podcasts.files.bbci.co.uk/b006qnmr.rss"

# The feed interleaves short daily "highlight clip" items (~180-245s,
# confirmed live 2026-09-24) with full weekly episodes (~3050-3130s). A
# clip this short is almost entirely consumed by INTRO_SKIP_SECONDS alone,
# leaving nothing to transcribe -- so fetch_latest_episode() filters for a
# minimum duration rather than blindly taking the newest item by pubDate.
# 1200s (20 min) sits safely between the two, with wide margin either side.
MIN_EPISODE_DURATION_SECONDS = 1200.0

# Fixed intro + host's guest-bio segment, consultant-verified (spec Section 0
# point 3): opening theme ~20-30s + host's intro monologue ~60-90s, usually
# into the real interview by 2:00-2:30, rarely later than 2:40. 3 minutes is
# the safe skip value.
INTRO_SKIP_SECONDS = 180.0
MONDAY_WINDOW_SECONDS = 20 * 60.0


@dataclass(frozen=True)
class Chunk:
    phrase: str
    definition: str
    context_sentence: str


@dataclass(frozen=True)
class WeeklyContent:
    week_number: int
    episode_title: str
    episode_guid: str
    segment_start: float
    segment_end: float
    transcript_excerpt: str
    chunks: list[Chunk]
    window_segments: list[TranscriptSegment]


GUEST_WINDOW_SCHEMA = {
    "type": "object",
    "properties": {
        "start_seconds": {"type": "number", "minimum": 0},
        "end_seconds": {"type": "number", "minimum": 0},
        "excerpt_text": {"type": "string", "minLength": 1},
    },
    "required": ["start_seconds", "end_seconds", "excerpt_text"],
    "additionalProperties": False,
}

CHUNK_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "chunks": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string", "minLength": 1},
                    "definition": {"type": "string", "minLength": 1},
                    "context_sentence": {"type": "string", "minLength": 1},
                },
                "required": ["phrase", "definition", "context_sentence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["chunks"],
    "additionalProperties": False,
}

STRETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "start_seconds": {"type": "number", "minimum": 0},
        "end_seconds": {"type": "number", "minimum": 0},
        "stretch_text": {"type": "string", "minLength": 1},
    },
    "required": ["start_seconds", "end_seconds", "stretch_text"],
    "additionalProperties": False,
}

ANNOTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "annotated_text": {"type": "string", "minLength": 1},
    },
    "required": ["annotated_text"],
    "additionalProperties": False,
}


def _format_transcript_for_prompt(segments: list[TranscriptSegment]) -> str:
    return "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in segments)


def workspace_dir_for_week(inbox_path: Path, week_number: int) -> Path:
    return inbox_path / "english_bot" / f"wk{week_number}"


def next_week_number(qdrant: QdrantClient, collection: str) -> int:
    """Weekly content is shared (no owner), so the next week number is just
    "highest week_number seen so far, plus one" -- no separate counter file,
    consistent with the "no independent state store" decision (Section 0
    point 1)."""
    points = qdrant.scroll_by_filters(collection, {"kind": "weekly_content"}, limit=512)
    max_week = 0
    for point in points:
        payload = point.get("payload") or {}
        week = payload.get("week_number")
        if isinstance(week, int) and week > max_week:
            max_week = week
    return max_week + 1


def is_episode_processed(qdrant: QdrantClient, collection: str, guid: str) -> bool:
    if not guid:
        return False
    points = qdrant.scroll_by_filters(
        collection, {"kind": "weekly_content", "episode_guid": guid}, limit=1
    )
    return bool(points)


def fetch_latest_episode(rss_xml: str) -> RssItem | None:
    """Newest item (by pubDate, since parse_rss_items already sorts that way)
    that is long enough to be a real episode, not a short highlight clip --
    see MIN_EPISODE_DURATION_SECONDS. An item with no <itunes:duration> at
    all is treated as unknown-but-acceptable rather than excluded, since the
    tag isn't guaranteed by every feed."""
    items = parse_rss_items(rss_xml)
    for item in items:
        if item.duration_seconds is None or item.duration_seconds >= MIN_EPISODE_DURATION_SECONDS:
            return item
    return None


def select_guest_dominant_window(llm: LlmClient, segments: list[TranscriptSegment]) -> dict:
    """LLM picks a ~2.5-3 min / 300-450 word window where the guest speaks
    >=75%, allowing brief host backchannels/short follow-ups -- not literal
    single-speaker-zero-interruption, which was rejected as unrealistic for
    an interview show (spec Section 0 point 3). This function only verifies
    the skill calls the model and parses its response correctly; judgment
    quality is an L3 concern."""
    transcript_block = _format_transcript_for_prompt(segments)
    prompt = (
        "This is a timestamped transcript excerpt from a BBC Radio 4 interview "
        "podcast. Find one continuous window, roughly 2.5-3 minutes (300-450 "
        "words), where the GUEST is the dominant speaker (at least 75% of the "
        "words). Brief host backchannels (\"mm\", \"right\", \"really?\") and "
        "short follow-up questions (10 words or fewer) inside the window are "
        "fine and do not disqualify it -- only exclude windows where the host "
        "is asking substantial questions throughout. Prefer a window with "
        "personal narrative, a challenge overcome, or a life perspective. "
        "Reply with the exact start_seconds and end_seconds taken from the "
        "transcript's own timestamps, and excerpt_text containing the "
        "verbatim transcript text for that window.\n\n"
        f"Transcript:\n{transcript_block}"
    )
    raw = llm.chat_json(prompt, GUEST_WINDOW_SCHEMA, schema_name="guest_dominant_window", max_tokens=1200)
    return json.loads(raw)


def extract_chunks(llm: LlmClient, excerpt_text: str) -> list[Chunk]:
    prompt = (
        "From this excerpt of a British radio interview, extract exactly 3 "
        "high-frequency English collocations or idioms that would be useful "
        "for a Traditional-Chinese-speaking professional to learn. For each, "
        "give a Traditional Chinese definition and one original example "
        "sentence set in a tech-workplace or daily-life context (not copied "
        "verbatim from the excerpt).\n\n"
        f"Excerpt:\n{excerpt_text}"
    )
    raw = llm.chat_json(prompt, CHUNK_EXTRACTION_SCHEMA, schema_name="chunk_extraction", max_tokens=800)
    data = json.loads(raw)
    return [
        Chunk(phrase=c["phrase"], definition=c["definition"], context_sentence=c["context_sentence"])
        for c in data["chunks"]
    ]


def store_weekly_content(
    qdrant: QdrantClient, embeddings: EmbeddingClient, collection: str, content: WeeklyContent
) -> None:
    """Shared content -- no owner, plain upsert_text (not
    owned_records.write_owned_point, which requires one). Each chunk point
    also carries the full window transcript as window_segments_json so
    Tuesday can pick its shadowing stretch without re-transcribing."""
    segments_json = json.dumps(
        [{"start": s.start, "end": s.end, "text": s.text} for s in content.window_segments]
    )
    for chunk in content.chunks:
        text = f"{chunk.phrase}: {chunk.definition}. {chunk.context_sentence}"
        vector = embeddings.embed(text)
        qdrant.upsert_text(
            collection,
            text,
            vector,
            {
                "kind": "weekly_content",
                "tag": f"eng_wk{content.week_number}",
                "week_number": content.week_number,
                "episode_guid": content.episode_guid,
                "episode_title": content.episode_title,
                "segment_start": content.segment_start,
                "segment_end": content.segment_end,
                "phrase": chunk.phrase,
                "definition": chunk.definition,
                "context_sentence": chunk.context_sentence,
                "transcript_excerpt": content.transcript_excerpt,
                "window_segments_json": segments_json,
                "mastered": False,
                "needs_review": False,
            },
        )


def read_this_week_payload(qdrant: QdrantClient, collection: str, week_number: int) -> dict:
    points = qdrant.scroll_by_filters(
        collection, {"kind": "weekly_content", "week_number": week_number}, limit=4
    )
    if not points:
        raise ValueError(f"no weekly content stored for week {week_number} -- has Monday's task run yet?")
    return points[0]["payload"]


def read_this_week_chunks(qdrant: QdrantClient, collection: str, week_number: int) -> list[dict]:
    """Unlike read_this_week_payload (one point), Friday/Saturday need all 3
    chunk payloads for the week."""
    points = qdrant.scroll_by_filters(
        collection, {"kind": "weekly_content", "week_number": week_number}, limit=4
    )
    if not points:
        raise ValueError(f"no weekly content stored for week {week_number} -- has Monday's task run yet?")
    return [p["payload"] for p in points]


def build_monday_message(content: WeeklyContent) -> str:
    lines = [
        f"This week's listening: {content.episode_title}",
        f"Listen to {content.segment_start:.0f}s-{content.segment_end:.0f}s in this week's episode.",
        "",
        "This week's chunks:",
    ]
    for chunk in content.chunks:
        lines.append(f"- {chunk.phrase} ({chunk.definition}): {chunk.context_sentence}")
    lines.append("")
    lines.append("Pick one chunk and reply with your own real-life example sentence after listening.")
    return "\n".join(lines)


def run_monday_task(
    *,
    clip_client: AudioClipClient,
    transcription_client: TranscriptionClient,
    llm: LlmClient,
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    owners: list[str],
    send_message: Callable[[str, str], None],
    workspace_dir: Path,
    rss_url: str = BBC_DESERT_ISLAND_DISCS_RSS,
) -> WeeklyContent | None:
    """Full Monday pipeline. Returns None (and sends nothing) if the latest
    episode has already been processed."""
    rss_xml = get_text(rss_url, timeout=30)
    episode = fetch_latest_episode(rss_xml)
    if episode is None or not episode.enclosure_url:
        raise ValueError("BBC RSS feed returned no usable episode with an audio enclosure")
    if is_episode_processed(qdrant, collection, episode.guid):
        return None

    workspace_dir.mkdir(parents=True, exist_ok=True)
    episode_path = workspace_dir / "episode.mp3"
    episode_path.write_bytes(get_bytes(episode.enclosure_url, timeout=120))

    window_path = workspace_dir / "window.mp3"
    clip_client.clip(episode_path, INTRO_SKIP_SECONDS, INTRO_SKIP_SECONDS + MONDAY_WINDOW_SECONDS, window_path)

    transcript = transcription_client.transcribe_with_segments(window_path)
    if not transcript.segments:
        raise ValueError("transcription returned no segments -- nothing to select a window from")

    window = select_guest_dominant_window(llm, transcript.segments)
    chunks = extract_chunks(llm, window["excerpt_text"])

    week_number = next_week_number(qdrant, collection)
    content = WeeklyContent(
        week_number=week_number,
        episode_title=episode.title,
        episode_guid=episode.guid,
        segment_start=INTRO_SKIP_SECONDS + float(window["start_seconds"]),
        segment_end=INTRO_SKIP_SECONDS + float(window["end_seconds"]),
        transcript_excerpt=window["excerpt_text"],
        chunks=chunks,
        window_segments=transcript.segments,
    )
    store_weekly_content(qdrant, embeddings, collection, content)

    message = build_monday_message(content)
    vector = embeddings.embed(message)
    for owner in owners:
        send_message(owner, message)
        mark_task_pushed(qdrant, collection, week_number, "mon", owner, vector)

    return content


def evaluate_monday_reply(
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    llm: LlmClient,
    collection: str,
    owner: str,
    week_number: int,
    chunks: list[dict],
    transcribed_reply: str,
) -> str:
    """Monday asks for one chunk used in an original sentence (spec 2.1
    point 7) -- reuses evaluate_chunk_usage exactly as Friday does (same
    judgment task, just "at least 1" instead of "at least 2"), writing back
    user_sentence/user_sentence_source="mon_reply" for whichever chunk(s)
    were used. Spec Section 3 has no dedicated feedback-report format for
    Monday (unlike Tue-Sat), so this only needs a short acknowledgement,
    not a structured report. This function was missing through v1.11-v1.14
    -- without it Monday could never be marked completed, so it would
    always show up in Saturday's skipped-task list even when the user did
    reply -- added here while wiring up full daily-completion tracking."""
    chunk_usage = evaluate_chunk_usage(llm, chunks, transcribed_reply)
    used_any = False
    for result in chunk_usage["chunk_results"]:
        used = bool(result["used_correctly"])
        sentence = (result.get("user_sentence") or "").strip()
        if not used:
            continue
        used_any = True
        record_chunk_usage(
            qdrant,
            embeddings,
            collection,
            owner,
            week_number,
            result["phrase"],
            used_correctly=True,
            user_sentence=sentence,
            source="mon_reply",
        )
    mark_task_completed(qdrant, collection, week_number, "mon", owner)
    if used_any:
        return "Nice -- got it, thanks for the example sentence."
    return (
        "Thanks for the reply -- I couldn't spot one of this week's chunks in there, "
        "but no worries, you'll get more chances this week."
    )


def select_longest_guest_stretch(llm: LlmClient, transcript_block: str) -> dict:
    """Pick the guest's single longest continuous stretch (no host
    interruption at all, not even backchannels) within Monday's already-
    selected window. Length is NOT forced to any duration -- accept
    whatever the longest real stretch is (spec Section 0 point 4)."""
    prompt = (
        "This is a timestamped transcript excerpt already known to be from a "
        "guest-dominant window of a radio interview. Find the single longest "
        "continuous stretch spoken by the guest with absolutely no host "
        "interruption, not even a backchannel -- this is for a close "
        "listening/shadowing exercise, so it must be pure guest speech only. "
        "It does not need to be any particular length; return whatever the "
        "longest such stretch actually is. Reply with its start_seconds, "
        "end_seconds (from the transcript's own timestamps), and the "
        "verbatim stretch_text.\n\n"
        f"Transcript:\n{transcript_block}"
    )
    raw = llm.chat_json(prompt, STRETCH_SCHEMA, schema_name="longest_guest_stretch", max_tokens=600)
    return json.loads(raw)


def annotate_for_shadowing(llm: LlmClient, stretch_text: str) -> str:
    """Bold stress, `/` pause marks, weak-form notes -- an LLM best-guess
    from the text using general English stress patterns, NOT a real
    acoustic analysis of this specific recording (Whisper gives text +
    timestamps only, no prosody)."""
    prompt = (
        "Annotate this English text for a shadowing (echo-reading) exercise. "
        "Mark stressed content words in **bold**, insert a `/` at natural "
        "chunk/pause boundaries, and where a function word (of/to/for/a/the/"
        "and) would naturally reduce to a weak form (schwa) in fluent speech, "
        "note it in parentheses right after the word, e.g. \"for(schwa)\". "
        "This is a linguistic best-guess from the text alone, not an "
        "analysis of the actual audio.\n\n"
        f"Text:\n{stretch_text}"
    )
    raw = llm.chat_json(prompt, ANNOTATION_SCHEMA, schema_name="shadowing_annotation", max_tokens=600)
    data = json.loads(raw)
    return data["annotated_text"]


def build_tuesday_message(annotated_text: str) -> str:
    return (
        "Shadowing practice\n\n"
        f"{annotated_text}\n\n"
        "Stress is **bold**, `/` marks a natural pause, and (schwa) notes are "
        "a linguistic best guess from the text -- not a measurement of the "
        "actual recording. Listen to the clip above, then reply with a voice "
        "message echoing it as closely as you can."
    )


@dataclass(frozen=True)
class TuesdayTask:
    annotated: str
    stretch_text: str


def run_tuesday_task(
    *,
    week_number: int,
    clip_client: AudioClipClient,
    llm: LlmClient,
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    owners: list[str],
    send_message: Callable[[str, str], None],
    send_audio: Callable[[str, Path, str], None],
    workspace_dir: Path,
) -> TuesdayTask:
    """Reuses Monday's already-clipped window.mp3 and stored transcript --
    no redownload, no re-transcription. Returns both the annotated text
    (already sent to the user) and the raw, unannotated stretch_text --
    the wiring layer needs the raw form as evaluate_tuesday_reply's
    reference_text, since the **bold**/`/`/(schwa) annotation marks would
    corrupt a word-level diff if compared against directly. The raw text
    is never persisted anywhere else (LLM-generated fresh each Tuesday),
    so it has to come from this return value, not a later Qdrant read."""
    payload = read_this_week_payload(qdrant, collection, week_number)
    window_segments = [
        TranscriptSegment(start=s["start"], end=s["end"], text=s["text"])
        for s in json.loads(payload.get("window_segments_json") or "[]")
    ]
    window_relative_start = float(payload["segment_start"]) - INTRO_SKIP_SECONDS
    window_relative_end = float(payload["segment_end"]) - INTRO_SKIP_SECONDS
    excerpt_segments = [
        s for s in window_segments if s.start >= window_relative_start and s.end <= window_relative_end
    ]
    if not excerpt_segments:
        excerpt_segments = window_segments

    transcript_block = _format_transcript_for_prompt(excerpt_segments)
    stretch = select_longest_guest_stretch(llm, transcript_block)
    annotated = annotate_for_shadowing(llm, stretch["stretch_text"])

    window_path = workspace_dir / "window.mp3"
    clip_path = workspace_dir / "tuesday_clip.mp3"
    clip_client.clip(window_path, float(stretch["start_seconds"]), float(stretch["end_seconds"]), clip_path)

    message = build_tuesday_message(annotated)
    vector = embeddings.embed(message)
    for owner in owners:
        send_message(owner, message)
        send_audio(owner, clip_path, "Shadow this clip -- listen, then record yourself echoing it.")
        mark_task_pushed(qdrant, collection, week_number, "tue", owner, vector)

    return TuesdayTask(annotated=annotated, stretch_text=stretch["stretch_text"])


# ---------------------------------------------------------------------------
# Voice evaluation (Section 3): word-level (not character-level) diff, since
# Whisper's spelling/punctuation/case noise makes character-level diffing too
# sensitive -- word-level (WER-style) is the speech-evaluation industry
# standard, per spec Section 3.
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z']+")


def _tokenize_words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


@dataclass(frozen=True)
class WordDiffResult:
    reference_word_count: int
    missing_words: list[str]
    extra_words: list[str]
    error_rate: float


def word_level_diff(reference: str, hypothesis: str) -> WordDiffResult:
    """Standard Word Error Rate (WER) computation via edit-distance dynamic
    programming on word tokens, with a proper backtrace so substitutions
    count as one error each (not conflated with delete+insert)."""
    ref = _tokenize_words(reference)
    hyp = _tokenize_words(hypothesis)
    n, m = len(ref), len(hyp)

    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])

    missing: list[str] = []
    extra: list[str] = []
    substitutions = 0
    deletions = 0
    insertions = 0
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref[i - 1] == hyp[j - 1]:
            i, j = i - 1, j - 1
            continue
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            substitutions += 1
            missing.append(ref[i - 1])
            extra.append(hyp[j - 1])
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            deletions += 1
            missing.append(ref[i - 1])
            i -= 1
        else:
            insertions += 1
            extra.append(hyp[j - 1])
            j -= 1
    missing.reverse()
    extra.reverse()

    ref_count = n or 1
    error_rate = (substitutions + deletions + insertions) / ref_count
    return WordDiffResult(
        reference_word_count=n,
        missing_words=missing,
        extra_words=extra,
        error_rate=round(error_rate, 4),
    )


def compute_wpm(text: str, duration_seconds: float) -> float:
    if duration_seconds <= 0:
        return 0.0
    word_count = len(_tokenize_words(text))
    return round(word_count / (duration_seconds / 60.0), 1)


def evaluate_tuesday_reply(
    reference_text: str,
    transcribed_reply: str,
    reply_duration_seconds: float,
    *,
    qdrant: QdrantClient,
    collection: str,
    owner: str,
    week_number: int,
) -> str:
    diff = word_level_diff(reference_text, transcribed_reply)
    wpm = compute_wpm(transcribed_reply, reply_duration_seconds)
    match_pct = max(0.0, 1 - diff.error_rate) * 100
    lines = [
        f'Transcribed: "{transcribed_reply}"',
        f"Word match: {match_pct:.0f}% (word-level, not letter-level)",
    ]
    if diff.missing_words:
        lines.append(f"Missing/changed: {', '.join(diff.missing_words[:8])}")
    if diff.extra_words:
        lines.append(f"Extra words: {', '.join(diff.extra_words[:8])}")
    lines.append(
        f"Pace: {wpm} WPM (this is shadowing -- aim to match the original clip's pace, "
        "not a fixed target)"
    )
    mark_task_completed(qdrant, collection, week_number, "tue", owner)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wednesday: IELTS Speaking Part 2 (spec 2.3). Fixed question bank, Part 2
# only for now -- the week_number > 17 Part 2+3 combo is v1.16, not here.
#
# The 12 cue cards below are the verbatim English text the consultant
# actually provided (IELTS Liz / IELTS Advantage sourcing, per spec 2.3),
# not a paraphrase -- the spec file only carries the short Chinese topic
# summaries, so the literal English wording is transcribed here from the
# original conversation turn where it was given. The remaining 48
# questions (to reach the spec's 60-question, zero-repeat-for-38-weeks
# target) are a content task for later, per spec Section 0.
# ---------------------------------------------------------------------------

IELTS_QUESTION_BANK: list[dict] = [
    {
        "id": "ielts_p2_event_01",
        "category": "event",
        "cue_card": (
            "Describe a time when you received bad customer service and how "
            "you handled it.\n\nYou should say:\n"
            "- when and where it was\n"
            "- what happened\n"
            "- who you spoke to\n"
            "and explain what action was taken to resolve the issue."
        ),
    },
    {
        "id": "ielts_p2_event_02",
        "category": "event",
        "cue_card": (
            "Describe an occasion when you had to adapt to a sudden change "
            "of plans.\n\nYou should say:\n"
            "- what the original plan was\n"
            "- why it changed\n"
            "- what you did instead\n"
            "and explain how you felt about the sudden disruption."
        ),
    },
    {
        "id": "ielts_p2_event_03",
        "category": "event",
        "cue_card": (
            "Describe a challenging task you completed under tight time "
            "pressure.\n\nYou should say:\n"
            "- what the task was\n"
            "- why time was limited\n"
            "- how you managed your time\n"
            "and explain the result of your effort."
        ),
    },
    {
        "id": "ielts_p2_people_01",
        "category": "people",
        "cue_card": (
            "Describe a mentor, colleague, or friend who gave you valuable "
            "advice.\n\nYou should say:\n"
            "- who this person is\n"
            "- what the advice was\n"
            "- why you needed it at that time\n"
            "and explain how following this advice affected your life."
        ),
    },
    {
        "id": "ielts_p2_people_02",
        "category": "people",
        "cue_card": (
            "Describe someone you met recently who made a strong first "
            "impression on you.\n\nYou should say:\n"
            "- where you met them\n"
            "- what you talked about\n"
            "- what stood out about their character\n"
            "and explain why they left an impression."
        ),
    },
    {
        "id": "ielts_p2_people_03",
        "category": "people",
        "cue_card": (
            "Describe an occasion when you helped an elderly person or "
            "neighbor.\n\nYou should say:\n"
            "- who they were\n"
            "- what kind of help they needed\n"
            "- what you did\n"
            "and explain how they reacted afterwards."
        ),
    },
    {
        "id": "ielts_p2_place_01",
        "category": "place",
        "cue_card": (
            "Describe a quiet place you often visit to relax or think.\n\n"
            "You should say:\n"
            "- where this place is\n"
            "- how often you go there\n"
            "- what you do when you are there\n"
            "and explain why it helps you clear your mind."
        ),
    },
    {
        "id": "ielts_p2_place_02",
        "category": "place",
        "cue_card": (
            "Describe a town or city you visited that was completely "
            "different from what you expected.\n\nYou should say:\n"
            "- where it was\n"
            "- what your initial expectations were\n"
            "- what it was actually like\n"
            "and explain whether the surprise was pleasant or disappointing."
        ),
    },
    {
        "id": "ielts_p2_place_03",
        "category": "place",
        "cue_card": (
            "Describe an open-air market or local shop you enjoy "
            "visiting.\n\nYou should say:\n"
            "- where it is located\n"
            "- what goods are sold there\n"
            "- who you usually go with\n"
            "and explain why you prefer it to a regular supermarket."
        ),
    },
    {
        "id": "ielts_p2_object_01",
        "category": "object",
        "cue_card": (
            "Describe an item of clothing or equipment you bought that "
            "turned out to be useless.\n\nYou should say:\n"
            "- what the item was\n"
            "- why you bought it\n"
            "- why it didn't meet your expectations\n"
            "and explain what you eventually did with it."
        ),
    },
    {
        "id": "ielts_p2_object_02",
        "category": "object",
        "cue_card": (
            "Describe an old personal possession that has strong sentimental "
            "value to you.\n\nYou should say:\n"
            "- what it is\n"
            "- how long you have owned it\n"
            "- who gave it to you or where you got it\n"
            "and explain why it means so much to you."
        ),
    },
    {
        "id": "ielts_p2_object_03",
        "category": "object",
        "cue_card": (
            "Describe a piece of technology (other than a smartphone) that "
            "broke down when you needed it most.\n\nYou should say:\n"
            "- what the device was\n"
            "- what went wrong with it\n"
            "- what you were trying to do at the time\n"
            "and explain how you handled the situation."
        ),
    },
]

STAR_EVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "situation_present": {"type": "boolean"},
        "task_present": {"type": "boolean"},
        "action_present": {"type": "boolean"},
        "result_present": {"type": "boolean"},
        "overused_words": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "word": {"type": "string", "minLength": 1},
                    "replacement": {"type": "string", "minLength": 1},
                },
                "required": ["word", "replacement"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "situation_present",
        "task_present",
        "action_present",
        "result_present",
        "overused_words",
    ],
    "additionalProperties": False,
}


def pick_ielts_question(qdrant: QdrantClient, collection: str) -> dict:
    """Random pick from the fixed bank, excluding questions already asked
    (tag:eng_ielts_topics, shared, no owner -- spec 2.3 Agent step 1). The
    bank only has 12 of its eventual 60 questions right now, so once every
    question has been asked at least once the "already asked" exclusion
    resets (picks from the full bank again) rather than raising -- a small
    bank cycling is preferable to the task silently failing to run."""
    asked_points = qdrant.scroll_by_filters(collection, {"tag": "eng_ielts_topics"}, limit=512)
    asked_ids = {(p.get("payload") or {}).get("question_id") for p in asked_points}
    available = [q for q in IELTS_QUESTION_BANK if q["id"] not in asked_ids]
    if not available:
        available = IELTS_QUESTION_BANK
    return random.choice(available)


def mark_ielts_question_asked(
    qdrant: QdrantClient, embeddings: EmbeddingClient, collection: str, question: dict
) -> None:
    text = f"ielts question asked: {question['id']}"
    vector = embeddings.embed(text)
    qdrant.upsert_text(
        collection,
        text,
        vector,
        {"tag": "eng_ielts_topics", "kind": "ielts_asked", "question_id": question["id"], "category": question["category"]},
    )


def build_wednesday_message(question: dict, part3_question: str | None = None) -> str:
    if part3_question:
        return (
            "IELTS Speaking Part 2 + Part 3\n\n"
            f"{question['cue_card']}\n\n"
            "Use the STAR principle (Situation, Task, Action, Result). Don't "
            "write a draft -- think for 1 minute, then speak for about 1 "
            "minute on Part 2.\n\n"
            f"Then, Part 3: {part3_question}\n\n"
            "Speak for about 1 more minute on Part 3 -- state a claim, "
            "acknowledge a counter-argument or limitation, then give your "
            "conclusion. Send both parts as one voice message, about 2 "
            "minutes total."
        )
    return (
        "IELTS Speaking Part 2\n\n"
        f"{question['cue_card']}\n\n"
        "Use the STAR principle (Situation, Task, Action, Result). Don't "
        "write a draft -- think for 1 minute, then speak continuously for "
        "1.5 to 2 minutes and send it as a voice message."
    )


# ---------------------------------------------------------------------------
# v1.16: from week_number > 17 (~month 5 of the 9-month plan), Wednesday
# becomes Part 2 + Part 3 "same-topic deep dive" (spec Section 0's SLA-review
# addendum, consultant Plan A) -- not a full switch away from Part 2 and not
# alternating weeks, so Part 2 narrative practice never goes stale. week_number
# <= 17 is completely unchanged from v1.13: build_wednesday_message/
# run_wednesday_task/evaluate_wednesday_reply all default part3_question to
# None, which reproduces the old single-part behaviour byte-for-byte.
# ---------------------------------------------------------------------------

WEDNESDAY_COMBO_THRESHOLD_WEEK = 17


def is_part2_3_combo_week(week_number: int) -> bool:
    return week_number > WEDNESDAY_COMBO_THRESHOLD_WEEK


# Real IELTS Part 3 examiners ask a live follow-up drawn from a macro theme,
# not a fixed cue card -- these 6 themes are the consultant's exact list
# (spec 2.3 point 2), used verbatim, not paraphrased.
PART3_THEMES = [
    "education reform",
    "environment and urbanization",
    "technology ethics",
    "consumerism",
    "cultural heritage preservation",
    "globalization and remote work",
]

PART3_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {"part3_question": {"type": "string", "minLength": 1}},
    "required": ["part3_question"],
    "additionalProperties": False,
}

ARGUMENT_EVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "claim_present": {"type": "boolean"},
        "concession_present": {"type": "boolean"},
        "conclusion_present": {"type": "boolean"},
    },
    "required": ["claim_present", "concession_present", "conclusion_present"],
    "additionalProperties": False,
}


def generate_part3_question(llm: LlmClient, part2_cue_card: str) -> str:
    """LLM extends the Part 2 topic into one debatable Part 3 macro
    question -- not a fixed bank, real examiners improvise this live (spec
    2.3 point 2)."""
    themes = ", ".join(PART3_THEMES)
    prompt = (
        "This is IELTS Speaking Part 3. The candidate just answered this "
        f"Part 2 cue card:\n{part2_cue_card}\n\n"
        "Generate ONE follow-up Part 3 question that extends the Part 2 "
        "topic into a debatable, evaluative macro-level discussion -- the "
        "kind a real examiner would ask to probe abstract reasoning, not a "
        "trivial follow-up about the same personal story. Draw on one of "
        f"these broader themes if it fits naturally: {themes}. For example, "
        "if Part 2 was about solving a work problem, a good Part 3 "
        "extension would be: \"Does automation and AI reduce human "
        "problem-solving skills, or does it enhance them?\""
    )
    raw = llm.chat_json(prompt, PART3_QUESTION_SCHEMA, schema_name="part3_question", max_tokens=150)
    return json.loads(raw)["part3_question"]


def evaluate_part3_argument(llm: LlmClient, part3_question: str, transcribed_reply: str) -> dict:
    """Part 3's own evaluation standard -- argument structure (Claim ->
    Concession/counter-argument -> Conclusion), a distinct schema from Part
    2's STAR narrative check (spec Section 3, week_number > 17)."""
    prompt = (
        "This is an IELTS Speaking Part 3 response to a discussion "
        f"question:\n\"{part3_question}\"\n\n"
        f"The spoken response (transcribed) was:\n\"{transcribed_reply}\"\n\n"
        "Judge whether each of these three argument-structure elements is "
        "present: Claim (a clear position/opinion is stated), Concession "
        "or counter-argument (acknowledges the other side or a "
        "limitation), Conclusion (wraps up with a final judgment)."
    )
    raw = llm.chat_json(
        prompt, ARGUMENT_EVAL_SCHEMA, schema_name="part3_argument_evaluation", max_tokens=300
    )
    return json.loads(raw)


def run_wednesday_task(
    *,
    llm: LlmClient,
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    week_number: int,
    owners: list[str],
    send_message: Callable[[str, str], None],
) -> dict:
    question = pick_ielts_question(qdrant, collection)
    mark_ielts_question_asked(qdrant, embeddings, collection, question)

    part3_question = None
    if is_part2_3_combo_week(week_number):
        part3_question = generate_part3_question(llm, question["cue_card"])

    message = build_wednesday_message(question, part3_question=part3_question)
    vector = embeddings.embed(message)
    for owner in owners:
        send_message(owner, message)
        mark_task_pushed(qdrant, collection, week_number, "wed", owner, vector)

    result = dict(question)
    if part3_question:
        result["part3_question"] = part3_question
    return result


def evaluate_wednesday_reply(
    llm: LlmClient,
    cue_card: str,
    transcribed_reply: str,
    *,
    qdrant: QdrantClient,
    collection: str,
    owner: str,
    week_number: int,
    part3_question: str | None = None,
) -> str:
    """LLM judges STAR-element presence and identifies overused basic
    vocabulary with band-7.5+ replacements -- no separate word-frequency
    pre-pass, per spec Section 3's confirmed decision. When part3_question
    is given (week_number > 17 combo weeks), also runs the Part 3
    argument-structure check on the same transcribed_reply and reports both
    sections separately, but still calls mark_task_completed exactly once
    regardless of one or two parts (spec Section 0's SLA addendum). Omitting
    part3_question reproduces the pre-v1.16 single-part behaviour exactly."""
    prompt = (
        "This is an IELTS Speaking Part 2 response. The cue card was:\n"
        f"{cue_card}\n\n"
        f"The spoken response (transcribed) was:\n\"{transcribed_reply}\"\n\n"
        "Judge whether each of the four STAR elements is present: "
        "Situation (context/background), Task (what needed to be done), "
        "Action (what the speaker actually did), Result (the outcome and "
        "how they felt). Also identify basic/simple words that were "
        "overused and suggest an IELTS band-7.5+ alternative for each."
    )
    raw = llm.chat_json(prompt, STAR_EVAL_SCHEMA, schema_name="star_evaluation", max_tokens=500)
    data = json.loads(raw)

    star_elements = {
        "Situation": data["situation_present"],
        "Task": data["task_present"],
        "Action": data["action_present"],
        "Result": data["result_present"],
    }
    missing = [name for name, present in star_elements.items() if not present]

    lines = [f'Transcribed: "{transcribed_reply}"']
    if part3_question:
        lines.append("")
        lines.append("Part 2 (STAR):")
    if missing:
        lines.append(f"STAR structure: missing {', '.join(missing)}")
    else:
        lines.append("STAR structure: complete (Situation, Task, Action, Result all present)")
    for item in data["overused_words"]:
        lines.append(f'Overused: "{item["word"]}" -> try "{item["replacement"]}" (band 7.5+)')

    if part3_question:
        argument = evaluate_part3_argument(llm, part3_question, transcribed_reply)
        argument_elements = {
            "Claim": argument["claim_present"],
            "Concession/counter-argument": argument["concession_present"],
            "Conclusion": argument["conclusion_present"],
        }
        argument_missing = [name for name, present in argument_elements.items() if not present]
        lines.append("")
        lines.append(f'Part 3 question: "{part3_question}"')
        lines.append("Part 3 (Argument structure):")
        if argument_missing:
            lines.append(f"Argument structure: missing {', '.join(argument_missing)}")
        else:
            lines.append(
                "Argument structure: complete (Claim, Concession/counter-argument, "
                "Conclusion all present)"
            )

    mark_task_completed(qdrant, collection, week_number, "wed", owner)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Thursday: British social small talk (spec 2.4). 7 fixed topic categories,
# but the opener itself is generated fresh by the LLM each time -- not a
# fixed script like Wednesday's cue cards. The safe/avoid table below is the
# consultant's authenticity guardrail and MUST actually land in the prompt
# text (spec Section 6 v1.13 calls this out as a named regression risk).
# ---------------------------------------------------------------------------

THURSDAY_CATEGORIES: list[str] = [
    "weather_banter",
    "home_garden_diy",
    "weekend_getaway",
    "commute_complaints",
    "pub_meetup",
    "sports_banter",
    "bank_holiday_plans",
]

# bank_holiday_plans has no dedicated safe/avoid table from the consultant --
# it deliberately shares the same understated, self-deprecating tone as
# every other category (spec 2.4's own note), so it's left empty here and
# _format_safe_avoid_for_prompt() falls back to that shared-tone instruction.
THURSDAY_SAFE_AVOID: dict[str, dict[str, list[str]]] = {
    "weather_banter": {
        "safe": [
            "Typical British summer, can't make up its mind.",
            "Make the most of it before it chucks it down.",
        ],
        "avoid": [
            "\"It's raining cats and dogs.\" (a textbook cliche real people rarely say)",
            "Stating an exact temperature -- too formal/serious for small talk",
        ],
    },
    "home_garden_diy": {
        "safe": [
            "Trying to keep on top of the weeds is a losing battle.",
            "The lawn is out of control, quite frankly.",
        ],
        "avoid": [
            "Boasting, or describing it like a professional landscaping report",
            "\"I executed landscape renovation\" -- far too formal a phrase",
        ],
    },
    "commute_complaints": {
        "safe": [
            "Southern/Thameslink was in a right mess this morning.",
            "Signal failures again, standard.",
        ],
        "avoid": [
            "Genuine political ranting or losing your temper -- British "
            "commute complaints are dry, understated sarcasm, not angry "
            "lecturing",
        ],
    },
    "weekend_getaway": {
        "safe": [
            "Just a quiet one, took the dog for a decent walk.",
            "Managed to nip away for a couple of days.",
        ],
        "avoid": [
            "Giving a blow-by-blow itinerary",
            "Making an ordinary weekend sound like a huge achievement",
        ],
    },
    "pub_meetup": {
        "safe": [
            "A swift half after work?",
            "Whose round is it?",
        ],
        "avoid": [
            "\"Shall we visit the public house to consume alcohol?\" -- "
            "textbook-formal, nobody talks like this",
        ],
    },
    "sports_banter": {
        "safe": [
            "Didn't catch the full match, just saw the highlights.",
            "Don't ask me, our defence is an absolute shambles this season.",
        ],
        "avoid": [
            "Pretending to know detailed tactics/stats you don't actually know",
            "Lecturing a colleague seriously right after their team lost",
        ],
    },
    "bank_holiday_plans": {"safe": [], "avoid": []},
}

THURSDAY_OPENER_SCHEMA = {
    "type": "object",
    "properties": {"opener": {"type": "string", "minLength": 1}},
    "required": ["opener"],
    "additionalProperties": False,
}

THURSDAY_EVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "anchor_present": {"type": "boolean"},
        "bounce_present": {"type": "boolean"},
        "vocabulary_original": {"type": "string"},
        "vocabulary_replacement": {"type": "string"},
        "banter_reply": {"type": "string", "minLength": 1},
    },
    "required": [
        "anchor_present",
        "bounce_present",
        "vocabulary_original",
        "vocabulary_replacement",
        "banter_reply",
    ],
    "additionalProperties": False,
}


def _format_safe_avoid_for_prompt(category: str) -> str:
    table = THURSDAY_SAFE_AVOID.get(category, {"safe": [], "avoid": []})
    if not table["safe"] and not table["avoid"]:
        return (
            "No specific example phrases for this category -- use the same "
            "understated, self-deprecating British tone as the other "
            "categories."
        )
    lines = ["Natural things a British person might actually say:"]
    lines.extend(f"- {s}" for s in table["safe"])
    lines.append("Avoid (outdated textbook idioms / too formal / too serious):")
    lines.extend(f"- {a}" for a in table["avoid"])
    return "\n".join(lines)


def generate_thursday_opener(llm: LlmClient, category: str) -> str:
    guidance = _format_safe_avoid_for_prompt(category)
    topic_label = category.replace("_", " ")
    prompt = (
        f"Generate a 2-3 sentence opener a British colleague might use for "
        f"small talk about {topic_label}, ending in a question. Do not use "
        "outdated idioms like \"rain cats and dogs\"; reflect everyday "
        "British understatement and self-deprecating banter.\n\n"
        f"{guidance}"
    )
    raw = llm.chat_json(prompt, THURSDAY_OPENER_SCHEMA, schema_name="thursday_opener", max_tokens=200)
    return json.loads(raw)["opener"]


def build_thursday_message(category: str, opener: str) -> str:
    return (
        f"British small talk ({category.replace('_', ' ')})\n\n"
        f"{opener}\n\n"
        "Reply with a voice message using Anchor & Bounce: acknowledge it, "
        "share your own situation, then bounce back an open question."
    )


def run_thursday_task(
    *,
    llm: LlmClient,
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    week_number: int,
    owners: list[str],
    send_message: Callable[[str, str], None],
) -> str:
    category = random.choice(THURSDAY_CATEGORIES)
    opener = generate_thursday_opener(llm, category)
    message = build_thursday_message(category, opener)
    vector = embeddings.embed(message)
    for owner in owners:
        send_message(owner, message)
        mark_task_pushed(qdrant, collection, week_number, "thu", owner, vector)
    return opener


def evaluate_thursday_reply(
    llm: LlmClient,
    opener: str,
    transcribed_reply: str,
    *,
    qdrant: QdrantClient,
    collection: str,
    owner: str,
    week_number: int,
) -> str:
    """Judges Anchor & Bounce structure and -- per spec Section 0's SLA-
    review addendum -- appends a text-only in-character colleague banter
    reply. This is the reinstated TEXT version only; the earlier TTS-based
    voice-reply idea stays removed (no TTS capability exists)."""
    prompt = (
        "This is a British workplace social small-talk exercise. "
        f"The opener was:\n\"{opener}\"\n\n"
        f"The reply (transcribed) was:\n\"{transcribed_reply}\"\n\n"
        "Judge whether the reply follows an Anchor & Bounce structure: "
        "Anchor = acknowledges/empathizes with the opener and shares the "
        "speaker's own situation; Bounce = throws back an open-ended "
        "question to keep the conversation going. If any phrase in the "
        "reply sounds unnatural or too literal, suggest one more idiomatic "
        "replacement (leave vocabulary_original and vocabulary_replacement "
        "as empty strings if the reply is already natural). Finally, write "
        "a short, authentic British colleague-style text reply (Pub Banter "
        "tone, understated and self-deprecating, not TTS -- just text) as "
        "if you were the colleague responding to what they just said."
    )
    raw = llm.chat_json(prompt, THURSDAY_EVAL_SCHEMA, schema_name="thursday_evaluation", max_tokens=500)
    data = json.loads(raw)

    lines = [
        "Social Bounce evaluation report:",
        f'- Transcribed: "{transcribed_reply}"',
    ]
    if data["anchor_present"] and data["bounce_present"]:
        lines.append("- Anchor & Bounce structure: complete (empathy, own situation, and a bounce-back question)")
    else:
        missing = []
        if not data["anchor_present"]:
            missing.append("Anchor (empathy + your own situation)")
        if not data["bounce_present"]:
            missing.append("Bounce (an open question thrown back)")
        lines.append(f"- Anchor & Bounce structure: missing {', '.join(missing)}")
    if data.get("vocabulary_original"):
        lines.append("")
        lines.append("Suggestion:")
        lines.append(f'  - Original: "{data["vocabulary_original"]}"')
        lines.append(f'  - More natural: "{data["vocabulary_replacement"]}"')
    lines.append("")
    lines.append("Colleague text reply (Pub Banter):")
    lines.append(f'"{data["banter_reply"]}"')
    mark_task_completed(qdrant, collection, week_number, "thu", owner)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shared per-chunk-per-owner progress (spec 2.5/2.6/Section 4). Friday and
# Saturday write/read the SAME record per (week_number, chunk, owner): a
# deterministic tag + chunk-name lookup (create on first write, update via
# set_payload after), not a fresh point every time. needs_review is sticky
# within a week -- once any single evaluation (Friday's speaking OR
# Saturday's cloze) marks it True, a later correct evaluation that week does
# NOT clear it back to False (spec 2.5 point 4: "任一次判定錯誤...標記
# needs_review", no mention of un-flagging on a later success).
#
# NOTE for the coordinator: the spec says a failure "留存至下一週期"
# (persists into the next cycle) -- read literally, that could mean a
# flagged chunk should stay visible as a review item in FUTURE weeks too,
# not just within this week's Friday/Saturday pair. This implementation
# scopes needs_review to the week (tag includes week_number), matching
# every test this milestone's spec section actually names (the weekly
# Monday->Friday->Saturday chain). Building a cross-week review backlog
# that aggregates needs_review=True chunks across many past weeks is not
# covered by any milestone in spec Section 6 (v1.11-v1.16) -- flagging this
# as an open question rather than guessing at unscoped work.
# ---------------------------------------------------------------------------


def _chunk_progress_tag(week_number: int) -> str:
    return f"eng_wk{week_number}"


def _read_chunk_progress_point(
    qdrant: QdrantClient, collection: str, owner: str, week_number: int, phrase: str
) -> dict | None:
    points = read_owned_points(
        qdrant,
        collection,
        owner,
        {"tag": _chunk_progress_tag(week_number), "kind": "chunk_progress", "chunk": phrase},
        limit=1,
    )
    return points[0] if points else None


def read_chunk_progress(
    qdrant: QdrantClient, collection: str, owner: str, week_number: int, phrase: str
) -> dict | None:
    point = _read_chunk_progress_point(qdrant, collection, owner, week_number, phrase)
    return point["payload"] if point else None


def record_chunk_usage(
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    owner: str,
    week_number: int,
    phrase: str,
    *,
    used_correctly: bool,
    user_sentence: str,
    source: str,
) -> None:
    """Create or update this user's per-chunk progress record for the week.
    Only overwrites user_sentence/user_sentence_source when a new non-empty
    sentence is given (Saturday's cloze-answer check has no new sentence to
    record, only a correctness verdict)."""
    existing = _read_chunk_progress_point(qdrant, collection, owner, week_number, phrase)
    needs_review = not used_correctly
    if existing:
        if (existing["payload"] or {}).get("needs_review"):
            needs_review = True  # sticky: stays flagged once set this week
        fields: dict = {"needs_review": needs_review}
        if user_sentence:
            fields["user_sentence"] = user_sentence
            fields["user_sentence_source"] = source
        qdrant.set_payload(collection, existing["id"], fields)
        return

    text = f"{phrase} usage: {user_sentence or '(not used)'}"
    vector = embeddings.embed(text)
    payload = {
        "tag": _chunk_progress_tag(week_number),
        "kind": "chunk_progress",
        "chunk": phrase,
        "needs_review": needs_review,
        "mastered": False,
    }
    if user_sentence:
        payload["user_sentence"] = user_sentence
        payload["user_sentence_source"] = source
    write_owned_point(qdrant, collection, owner, text, vector, payload)


# ---------------------------------------------------------------------------
# Friday: chunk activation (spec 2.5). Fully resolved, no open design
# questions -- read this week's chunks, ask for a 1-minute impromptu voice
# ramble using at least 2 of them, LLM judges usage semantically (spoken
# chunks morph -- "spread oneself too thin" becomes "I was spreading myself
# too thin"), and critically writes back the user's ACTUAL sentence
# (user_sentence_source="fri_voice") for Saturday to quiz from.
# ---------------------------------------------------------------------------

FRIDAY_EVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "chunk_results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string", "minLength": 1},
                    "used_correctly": {"type": "boolean"},
                    "user_sentence": {"type": "string"},
                },
                "required": ["phrase", "used_correctly", "user_sentence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["chunk_results"],
    "additionalProperties": False,
}


def build_friday_message(chunks: list[dict]) -> str:
    lines = ["Friday chunk activation", "", "This week's chunks:"]
    for chunk in chunks:
        lines.append(f"- {chunk['phrase']} ({chunk['definition']})")
    lines.append("")
    lines.append(
        "Record a 1-minute impromptu voice ramble on anything -- naturally "
        "work in at least 2 of these chunks."
    )
    return "\n".join(lines)


def run_friday_task(
    *,
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    week_number: int,
    owners: list[str],
    send_message: Callable[[str, str], None],
) -> list[dict]:
    chunks = read_this_week_chunks(qdrant, collection, week_number)
    message = build_friday_message(chunks)
    vector = embeddings.embed(message)
    for owner in owners:
        send_message(owner, message)
        mark_task_pushed(qdrant, collection, week_number, "fri", owner, vector)
    return chunks


def evaluate_chunk_usage(llm: LlmClient, chunks: list[dict], transcribed_reply: str) -> dict:
    """LLM judges chunk usage via semantic understanding, not string
    matching -- spoken chunks morph, e.g. "spread oneself too thin" becomes
    "I was spreading myself too thin" (spec 2.5 point 3). Also extracts the
    user's actual sentence for each chunk they used."""
    phrase_list = "\n".join(f"- {c['phrase']}" for c in chunks)
    prompt = (
        "This week's target English chunks are:\n"
        f"{phrase_list}\n\n"
        f"The speaker's impromptu voice reply (transcribed) was:\n\"{transcribed_reply}\"\n\n"
        "For each chunk, judge whether it was used correctly and naturally "
        "in the reply. Spoken language morphs a chunk's exact wording -- "
        "e.g. \"spread oneself too thin\" might be said as \"I was "
        "spreading myself too thin\" -- that still counts as correct use. "
        "If used, quote the exact sentence or snippet from the reply "
        "containing it as user_sentence; leave user_sentence as an empty "
        "string if the chunk was not used."
    )
    raw = llm.chat_json(prompt, FRIDAY_EVAL_SCHEMA, schema_name="friday_chunk_usage", max_tokens=600)
    return json.loads(raw)


def evaluate_friday_reply(
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    llm: LlmClient,
    collection: str,
    owner: str,
    week_number: int,
    chunks: list[dict],
    transcribed_reply: str,
) -> str:
    chunk_usage = evaluate_chunk_usage(llm, chunks, transcribed_reply)
    lines = ["Friday chunk-activation evaluation:", f'- Transcribed: "{transcribed_reply}"', ""]
    for result in chunk_usage["chunk_results"]:
        phrase = result["phrase"]
        used = bool(result["used_correctly"])
        sentence = (result.get("user_sentence") or "").strip()
        record_chunk_usage(
            qdrant,
            embeddings,
            collection,
            owner,
            week_number,
            phrase,
            used_correctly=used,
            user_sentence=sentence,
            source="fri_voice",
        )
        status = "used correctly" if used else "not detected / not used naturally"
        lines.append(f"- {phrase}: {status}")
    mark_task_completed(qdrant, collection, week_number, "fri", owner)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Saturday: weekly cloze review (spec 2.6). Cloze generation is deterministic
# string manipulation, not an LLM call -- spec Section 6 explicitly names
# this as the priority to test thoroughly. Prefers the user's own sentence
# (from Monday's reply or Friday's voice ramble) over the shared
# context_sentence, per spec 2.6's confirmed fallback order.
#
# Answer judging is also deterministic (word/phrase match, not LLM) -- spec
# Section 6 leaves this open but a known target string is checked more
# reliably this way than via a semantic judgment call.
#
# The pending-answer state machine (PENDING_ANSWER in
# app/openclaw_telegram_gateway.py) is NOT touched directly here --
# english_bot.py stays gateway-agnostic, matching how send_message/
# send_audio are already injected rather than imported. run_saturday_task
# takes a set_pending_answer callable; the not-yet-built gateway/cron wiring
# step supplies the real one.
# ---------------------------------------------------------------------------

_REFLEXIVE_VARIANTS = [
    "myself",
    "yourself",
    "himself",
    "herself",
    "itself",
    "ourselves",
    "yourselves",
    "themselves",
    "oneself",
]


def _build_phrase_pattern(phrase: str) -> re.Pattern:
    """Match a chunk phrase inside a sentence, tolerating the spoken-language
    morphology that's common for this kind of idiom: 'oneself' standing in
    for any actual reflexive pronoun, and verb-ending variation that keeps
    the literal stem as a prefix (spread/spreading, take/takes/taken) via a
    loose word-stem + \\w* match. Does NOT handle irregular stem changes
    (take -> took) or silent-e-drop spellings (take -> taking) -- callers
    fall back to context_sentence or a phrase-only prompt in that case, see
    build_cloze_question."""
    words = phrase.split()
    parts = []
    for word in words:
        if word.lower() == "oneself":
            parts.append("(?:" + "|".join(_REFLEXIVE_VARIANTS) + ")")
        elif word.isalpha():
            parts.append(re.escape(word) + r"\w*")
        else:
            parts.append(re.escape(word))
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


def generate_cloze(phrase: str, sentence: str) -> str | None:
    """Deterministic string blanking: find the phrase's occurrence in the
    sentence (tolerating morphology, see _build_phrase_pattern) and replace
    it with a blank. Returns None if no match is found at all -- the caller
    decides the fallback (spec 2.6 point 1's context_sentence fallback, or
    ultimately a phrase-only prompt)."""
    if not phrase or not sentence:
        return None
    pattern = _build_phrase_pattern(phrase)
    match = pattern.search(sentence)
    if not match:
        return None
    return sentence[: match.start()] + "____" + sentence[match.end() :]


def judge_cloze_answer(phrase: str, user_answer: str) -> bool:
    """Deterministic check: does the answer contain a recognizable form of
    the target phrase (same tolerance as generate_cloze)? Simpler and more
    reliable than an LLM call for checking one known target string."""
    if not phrase or not user_answer:
        return False
    return bool(_build_phrase_pattern(phrase).search(user_answer))


@dataclass(frozen=True)
class ClozeQuestion:
    phrase: str
    prompt_text: str
    source: str  # "user_sentence" | "context_sentence" | "phrase_only"


def build_cloze_question(chunk_payload: dict, progress_payload: dict | None) -> ClozeQuestion:
    """Prefers the user's own sentence (spec 2.6: "優先" -- priority) over
    the shared LLM-generated context_sentence, falling back further to a
    phrase-only prompt if neither sentence actually contains a matchable
    form of the phrase."""
    phrase = chunk_payload["phrase"]
    user_sentence = (progress_payload or {}).get("user_sentence") or ""
    if user_sentence:
        blanked = generate_cloze(phrase, user_sentence)
        if blanked:
            return ClozeQuestion(phrase=phrase, prompt_text=blanked, source="user_sentence")

    context_sentence = chunk_payload.get("context_sentence") or ""
    blanked = generate_cloze(phrase, context_sentence)
    if blanked:
        return ClozeQuestion(phrase=phrase, prompt_text=blanked, source="context_sentence")

    return ClozeQuestion(
        phrase=phrase,
        prompt_text=f'(no example sentence on file) Use "{phrase}" correctly in a sentence.',
        source="phrase_only",
    )


def build_saturday_message(questions: list[ClozeQuestion]) -> str:
    lines = ["Saturday review quiz", "", "Fill in each blank:"]
    for index, question in enumerate(questions, start=1):
        lines.append(f"{index}. {question.prompt_text}")
    lines.append("")
    lines.append("Reply with your answers (text or voice) -- one message covers all three.")
    return "\n".join(lines)


def run_saturday_task(
    *,
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    week_number: int,
    owners: list[str],
    send_message: Callable[[str, str], None],
    set_pending_answer: Callable[[str, dict], None],
) -> dict[str, list[ClozeQuestion]]:
    chunks = read_this_week_chunks(qdrant, collection, week_number)
    result: dict[str, list[ClozeQuestion]] = {}
    for owner in owners:
        questions = [
            build_cloze_question(
                chunk, read_chunk_progress(qdrant, collection, owner, week_number, chunk["phrase"])
            )
            for chunk in chunks
        ]
        message = build_saturday_message(questions)
        vector = embeddings.embed(message)
        send_message(owner, message)
        mark_task_pushed(qdrant, collection, week_number, "sat", owner, vector)
        set_pending_answer(
            owner,
            {"kind": "eng_saturday_quiz", "week_number": week_number, "phrases": [q.phrase for q in questions]},
        )
        result[owner] = questions
    return result


def evaluate_saturday_answers(
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    owner: str,
    week_number: int,
    phrases: list[str],
    transcribed_answer: str,
) -> str:
    lines = ["Saturday review results:", f'- Your answer: "{transcribed_answer}"', ""]
    for phrase in phrases:
        correct = judge_cloze_answer(phrase, transcribed_answer)
        record_chunk_usage(
            qdrant,
            embeddings,
            collection,
            owner,
            week_number,
            phrase,
            used_correctly=correct,
            user_sentence="",
            source="",
        )
        lines.append(f"- {phrase}: {'correct' if correct else 'needs review'}")
    mark_task_completed(qdrant, collection, week_number, "sat", owner)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sunday: British culture painless reading (spec 2.7). RSS URLs, the 3-tier
# freshness/dedup fallback, and the Guardian HTML structure to scrape were
# all consultant-verified. During implementation review, the consultant's
# exact series-slug RSS URLs (.../series/tim-dowling-column/rss and
# .../series/grace-dent-on-restaurants/rss) were re-checked against the
# live site and both now 404 -- the series pages appear to have been
# reorganized since the consultant checked. The equivalent author-profile
# RSS format (.../profile/<name>/rss) was verified live and returns 200
# with real, current articles by the same columnists, so that's what's
# used here instead. .../uk/lifeandstyle/rss (tier 3) was unaffected and
# also verified live. Re-check these three URLs periodically -- Guardian's
# URL structure isn't something this codebase controls.
# ---------------------------------------------------------------------------

GUARDIAN_TIM_DOWLING_RSS = "https://www.theguardian.com/profile/timdowling/rss"
GUARDIAN_GRACE_DENT_RSS = "https://www.theguardian.com/profile/gracedent/rss"
GUARDIAN_LIFESTYLE_RSS = "https://www.theguardian.com/uk/lifeandstyle/rss"
SUNDAY_FEEDS = [GUARDIAN_TIM_DOWLING_RSS, GUARDIAN_GRACE_DENT_RSS, GUARDIAN_LIFESTYLE_RSS]

# A real desktop browser UA, per spec 2.7 point 3 -- Guardian's CDN can 403
# the default OpenClaw-Arm-Continuum/0.1 UA.
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SUNDAY_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string", "minLength": 1, "maxLength": 400}},
    "required": ["summary"],
    "additionalProperties": False,
}


def is_article_pushed(qdrant: QdrantClient, collection: str, article_url: str) -> bool:
    if not article_url:
        return False
    points = qdrant.scroll_by_filters(
        collection, {"tag": "eng_sunday_pushed", "article_url": article_url}, limit=1
    )
    return bool(points)


def mark_article_pushed(
    qdrant: QdrantClient, embeddings: EmbeddingClient, collection: str, article_url: str
) -> None:
    text = f"sunday article pushed: {article_url}"
    vector = embeddings.embed(text)
    qdrant.upsert_text(
        collection,
        text,
        vector,
        {"tag": "eng_sunday_pushed", "kind": "sunday_article", "article_url": article_url},
    )


def _freshest_unpushed_item(
    qdrant: QdrantClient, collection: str, items: list[RssItem], *, now: datetime | None = None
) -> RssItem | None:
    """Tier 1 + Tier 2 for a single feed (spec 2.7 point 1 / Section 5):
    prefer the newest item if it's both fresh (<=7 days old) and not yet
    pushed; otherwise fall back to the newest not-yet-pushed item from the
    last ~2 months among this feed's first 5 items. Returns None if this
    feed has nothing usable at all (caller then tries the next feed --
    Tier 3)."""
    if not items:
        return None
    reference_now = now or datetime.now(timezone.utc)
    newest = items[0]
    if (
        newest.pub_date
        and (reference_now - newest.pub_date) <= timedelta(days=7)
        and not is_article_pushed(qdrant, collection, newest.link)
    ):
        return newest
    for item in items:
        if (
            item.pub_date
            and (reference_now - item.pub_date) <= timedelta(days=60)
            and not is_article_pushed(qdrant, collection, item.link)
        ):
            return item
    return None


def select_sunday_article(
    qdrant: QdrantClient,
    collection: str,
    fetch_rss: Callable[[str], str],
    *,
    now: datetime | None = None,
) -> RssItem:
    """Tier 3: if the primary feed has nothing usable (all pushed, or all
    stale), move to the next feed in SUNDAY_FEEDS and retry Tiers 1+2 there.
    Raises if all three feeds are exhausted -- an extremely unlikely edge
    case (would mean months of Guardian downtime across 3 different
    columns), but the caller (a future cron step) needs a signal to skip
    Sunday's push entirely rather than crash on a None."""
    for feed_url in SUNDAY_FEEDS:
        rss_xml = fetch_rss(feed_url)
        items = parse_rss_items(rss_xml)[:5]
        chosen = _freshest_unpushed_item(qdrant, collection, items, now=now)
        if chosen:
            return chosen
    raise ValueError("no usable Sunday article found across primary and fallback Guardian feeds")


class _ArticleTextExtractor(HTMLParser):
    """Extracts text from <p> tags nested inside an <article> element --
    Guardian's stable structure, consultant-verified (spec 2.7 point 3).
    Ignores everything outside <article>; inline tags inside a <p> (links,
    bold, etc.) still contribute their text since only "article"/"p" affect
    scope tracking."""

    def __init__(self) -> None:
        super().__init__()
        self._article_depth = 0
        self._p_depth = 0
        self._paragraphs: list[str] = []
        self._current: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "article":
            self._article_depth += 1
        elif tag == "p" and self._article_depth > 0:
            self._p_depth += 1
            self._current = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "article" and self._article_depth > 0:
            self._article_depth -= 1
        elif tag == "p" and self._p_depth > 0:
            self._p_depth -= 1
            text = "".join(self._current).strip()
            if text:
                self._paragraphs.append(text)
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._p_depth > 0:
            self._current.append(data)

    @property
    def text(self) -> str:
        return "\n\n".join(self._paragraphs)


def extract_article_text(html: str) -> str:
    parser = _ArticleTextExtractor()
    parser.feed(html)
    return parser.text


def summarize_sunday_article(llm: LlmClient, article_text: str) -> str:
    """LLM reads the real fetched article text and writes a ~50-word
    cultural-background summary -- explicitly grounded in the actual
    content, not guessed from the title/URL (spec 2.7 point 4)."""
    prompt = (
        "This is the full text of a British lifestyle/culture newspaper "
        "column. Write a roughly 50-word summary in English that gives a "
        "Traditional-Chinese-speaking reader enough cultural background to "
        "understand and enjoy the piece -- don't retell the whole plot, "
        "just orient them.\n\n"
        f"Article text:\n{article_text}"
    )
    raw = llm.chat_json(prompt, SUNDAY_SUMMARY_SCHEMA, schema_name="sunday_summary", max_tokens=200)
    return json.loads(raw)["summary"]


def build_sunday_message(article_url: str, summary: str) -> str:
    return (
        "Sunday painless reading\n\n"
        f"{article_url}\n\n"
        f"{summary}\n\n"
        "Read for the gist -- no dictionary, no notes. Understanding "
        "70-80% of it is the goal."
    )


def run_sunday_task(
    *,
    llm: LlmClient,
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    week_number: int,
    owners: list[str],
    send_message: Callable[[str, str], None],
    fetch_rss: Callable[[str], str] = lambda url: get_text(url, timeout=30),
    fetch_article_html: Callable[[str], str] = lambda url: get_text(
        url, timeout=30, user_agent=DESKTOP_USER_AGENT
    ),
) -> str:
    article = select_sunday_article(qdrant, collection, fetch_rss)
    article_html = fetch_article_html(article.link)
    article_text = extract_article_text(article_html)
    if not article_text:
        raise ValueError(f"could not extract any <article>/<p> text from {article.link}")

    summary = summarize_sunday_article(llm, article_text)
    message = build_sunday_message(article.link, summary)
    vector = embeddings.embed(message)
    mark_article_pushed(qdrant, embeddings, collection, article.link)
    for owner in owners:
        send_message(owner, message)
        mark_task_pushed(qdrant, collection, week_number, "sun", owner, vector)
        # No reply is expected on Sundays (spec 2.7: purely passive reading,
        # no evaluation step at all) -- mark complete immediately, otherwise
        # every Sunday would wrongly show up in the skipped-task list even
        # though the content was successfully delivered.
        mark_task_completed(qdrant, collection, week_number, "sun", owner)
    return summary


# ---------------------------------------------------------------------------
# Daily-completion-tracking wiring (spec Section 5 point 3). Every
# run_<day>_task already calls mark_task_pushed (built in v1.11-v1.14); what
# was still missing is the completion side -- each evaluate_<day>_* function
# now also calls mark_task_completed once it has successfully processed a
# reply, plus a thin sweep orchestrator for the 21:00 cron step.
# ---------------------------------------------------------------------------

ALL_DAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def run_daily_completion_sweep(
    qdrant: QdrantClient, collection: str, week_number: int, owners: list[str]
) -> dict[str, list[str]]:
    """Meant for a future 21:00 /cron entry: sweep every day of the current
    week, marking skipped=True for any owner whose task is still
    incomplete. Returns {day_code: [owners swept]} for logging."""
    return {
        day: sweep_incomplete_to_skipped(qdrant, collection, week_number, day, owners)
        for day in ALL_DAY_CODES
    }
