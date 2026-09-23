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


def build_wednesday_message(question: dict) -> str:
    return (
        "IELTS Speaking Part 2\n\n"
        f"{question['cue_card']}\n\n"
        "Use the STAR principle (Situation, Task, Action, Result). Don't "
        "write a draft -- think for 1 minute, then speak continuously for "
        "1.5 to 2 minutes and send it as a voice message."
    )


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

    message = build_wednesday_message(question)
    vector = embeddings.embed(message)
    for owner in owners:
        send_message(owner, message)
        mark_task_pushed(qdrant, collection, week_number, "wed", owner, vector)
    return question


def evaluate_wednesday_reply(llm: LlmClient, cue_card: str, transcribed_reply: str) -> str:
    """LLM judges STAR-element presence and identifies overused basic
    vocabulary with band-7.5+ replacements -- no separate word-frequency
    pre-pass, per spec Section 3's confirmed decision."""
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
    if missing:
        lines.append(f"STAR structure: missing {', '.join(missing)}")
    else:
        lines.append("STAR structure: complete (Situation, Task, Action, Result all present)")
    for item in data["overused_words"]:
        lines.append(f'Overused: "{item["word"]}" -> try "{item["replacement"]}" (band 7.5+)')
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


def evaluate_thursday_reply(llm: LlmClient, opener: str, transcribed_reply: str) -> str:
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
    return "\n".join(lines)
