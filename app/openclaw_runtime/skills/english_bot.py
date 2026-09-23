"""Monday + Tuesday task logic for the English-learning bot (bot4,
lc9_dgx4_en), per openclaw_eng_spec.md Section 0/2.1/2.2.

Not wired into the SkillRouter/skills.json -- Monday/Tuesday are triggered
by time (a future /cron entry), not by a user's message text, so they don't
fit the existing can_handle(text)/run(text) Skill protocol. This module
exposes small, independently testable functions plus one orchestrator per
day; a later wiring step (cron + gateway) calls the orchestrators and
supplies real send_message/send_audio callables and owner chat_ids.

Weekly content (episode, chunks, transcript) is SHARED across the family --
written with plain qdrant.upsert_text(), not owned_records.write_owned_point(),
since that module deliberately requires an owner and this content has none
(see spec Section 0 point 2). Per-user completion tracking still goes
through daily_task_tracking, which is owner-scoped.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from openclaw_runtime.audio_clip_client import AudioClipClient
from openclaw_runtime.daily_task_tracking import mark_task_pushed
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.http_client import get_bytes, get_text
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.rss_client import RssItem, parse_rss_items
from openclaw_runtime.transcription_client import TranscriptionClient, TranscriptSegment


BBC_DESERT_ISLAND_DISCS_RSS = "https://podcasts.files.bbci.co.uk/b006qnmr.rss"

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
    items = parse_rss_items(rss_xml)
    return items[0] if items else None


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
) -> str:
    """Reuses Monday's already-clipped window.mp3 and stored transcript --
    no redownload, no re-transcription."""
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

    return annotated


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


def evaluate_tuesday_reply(reference_text: str, transcribed_reply: str, reply_duration_seconds: float) -> str:
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
    return "\n".join(lines)
