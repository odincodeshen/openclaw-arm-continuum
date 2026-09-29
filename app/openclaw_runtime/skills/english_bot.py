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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from openclaw_runtime.audio_clip_client import AudioClipClient
from openclaw_runtime.daily_task_tracking import mark_task_completed, mark_task_pushed, sweep_incomplete_to_skipped
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.http_client import get_bytes, get_text
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.message_cards import (
    DONE_STEP,
    FeedbackCard,
    TaskCard,
    bold,
    esc,
    expandable,
    italic,
    markdown_bold_to_html,
    render_feedback_card,
    render_task_card,
)
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
    episode_link: str = ""  # the programme page, for listening to the whole episode
    # Listening check (gist question + a dictation gap-fill from the clip).
    # Weeks stored before this existed have listening_check False and are
    # graded on the chunk sentence alone.
    listening_check: bool = False
    dictation_prompt: str = ""
    dictation_answers: list[str] = field(default_factory=list)


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

CHINESE_SHADOWING_FEEDBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis_zh": {"type": "string", "minLength": 1},
    },
    "required": ["analysis_zh"],
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


def full_length_episodes(rss_xml: str) -> list[RssItem]:
    """Every item long enough to be a real episode, not a short highlight
    clip (see MIN_EPISODE_DURATION_SECONDS), newest first -- parse_rss_items
    already sorts by pubDate. An item with no <itunes:duration> at all is
    treated as unknown-but-acceptable rather than excluded, since the tag
    isn't guaranteed by every feed."""
    return [
        item
        for item in parse_rss_items(rss_xml)
        if item.duration_seconds is None or item.duration_seconds >= MIN_EPISODE_DURATION_SECONDS
    ]


def fetch_latest_episode(rss_xml: str) -> RssItem | None:
    """Newest full-length episode in the feed."""
    episodes = full_length_episodes(rss_xml)
    return episodes[0] if episodes else None


def pick_unused_episode(rss_xml: str, is_processed: Callable[[str], bool]) -> RssItem | None:
    """Newest full-length episode not used for a week yet. The show takes
    breaks (only daily highlight clips for weeks at a time), so when the
    newest episode has already been used, an older one from the feed's
    back catalogue still gives the week fresh material. None only when
    every full-length episode in the feed has been used."""
    for episode in full_length_episodes(rss_xml):
        if episode.enclosure_url and not is_processed(episode.guid):
            return episode
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
        "verbatim from the excerpt). The example sentence (context_sentence) "
        "MUST be written in English and MUST contain the phrase itself -- "
        "its verb may be inflected and placeholders like someone/one's "
        "replaced by real words. It is used to quiz the learner, so a Chinese "
        "sentence or a translation is useless.\n\n"
        f"Excerpt:\n{excerpt_text}"
    )
    raw = llm.chat_json(prompt, CHUNK_EXTRACTION_SCHEMA, schema_name="chunk_extraction", max_tokens=800)
    data = json.loads(raw)
    chunks = [
        Chunk(phrase=c["phrase"], definition=c["definition"], context_sentence=c["context_sentence"])
        for c in data["chunks"]
    ]
    return ensure_english_examples(llm, chunks)


EXAMPLE_SENTENCES_SCHEMA = {
    "type": "object",
    "properties": {
        "examples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string", "minLength": 1},
                    "sentence": {"type": "string", "minLength": 1},
                },
                "required": ["phrase", "sentence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["examples"],
    "additionalProperties": False,
}


def ensure_english_examples(llm: LlmClient, chunks: list[Chunk]) -> list[Chunk]:
    """Every chunk needs an English example sentence that actually contains
    the phrase: the Monday card shows it, and Saturday's cloze (and the
    word review) blank the phrase out of it. The model sometimes answers in
    the bot's reply language instead -- a Chinese sentence with no phrase to
    blank -- so ask again, once, for just those. A chunk still without a
    usable sentence keeps what it had (the quiz falls back to "use it in a
    sentence")."""
    missing = [chunk for chunk in chunks if not generate_cloze(chunk.phrase, chunk.context_sentence)]
    if not missing:
        return chunks
    phrases = "\n".join(f"- {chunk.phrase}" for chunk in missing)
    prompt = (
        "Write one natural English example sentence for each of these English "
        "phrases, set in a tech-workplace or daily-life context. Each sentence "
        "must be in English and contain the phrase itself (the verb may be "
        "inflected; placeholders like someone/one's become real words). Do "
        f"not translate anything into Chinese.\n\n{phrases}"
    )
    try:
        data = json.loads(
            llm.chat_json(prompt, EXAMPLE_SENTENCES_SCHEMA, schema_name="english_examples", max_tokens=500)
        )
    except Exception:  # noqa: BLE001 - keep the original sentences rather than fail the whole Monday
        return chunks
    fixed = {item["phrase"].strip().lower(): item["sentence"].strip() for item in data.get("examples", [])}
    result = []
    for chunk in chunks:
        sentence = fixed.get(chunk.phrase.strip().lower(), "")
        if chunk in missing and sentence and generate_cloze(chunk.phrase, sentence):
            chunk = Chunk(phrase=chunk.phrase, definition=chunk.definition, context_sentence=sentence)
        result.append(chunk)
    return result


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
                "listening_check": content.listening_check,
                "dictation_prompt": content.dictation_prompt,
                "dictation_answers": content.dictation_answers,
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


# Words never blanked in the dictation: too short or too predictable to be
# worth listening for.
_DICTATION_SKIP_WORDS = {
    "about", "after", "again", "also", "because", "been", "before", "being", "could", "didn't",
    "does", "doing", "don't", "down", "each", "even", "from", "have", "having", "here", "into",
    "it's", "just", "know", "like", "make", "many", "more", "much", "only", "other", "over",
    "really", "said", "some", "such", "than", "that", "that's", "their", "them", "then", "there",
    "these", "they", "thing", "things", "think", "this", "those", "very", "want", "well", "were",
    "what", "when", "where", "which", "while", "will", "with", "would", "your", "yeah",
}
DICTATION_BLANKS = 3


def _core(token: str) -> str:
    return token.strip(".,!?;:\"'“”‘’()[]-—…")


def choose_dictation(
    segments: list[TranscriptSegment], start: float, end: float, avoid_words: set[str] | None = None
) -> tuple[str, list[str]] | None:
    """Pick one transcript sentence from the selected stretch and blank out
    three content words, spread across it -- a short dictation to check the
    learner caught the exact words. Deterministic (no LLM): the segment
    nearest the middle of the stretch with 8-25 words and at least three
    candidate words (letters only, 4+ long, not a stock function word, and
    not in avoid_words -- the week's chunk phrases are printed on the same
    card, so blanking one of their words would give the answer away)."""
    avoid = {word.lower() for word in (avoid_words or set())}
    middle = (start + end) / 2
    candidates = []
    for segment in segments:
        if segment.end < start or segment.start > end:
            continue
        tokens = segment.text.split()
        if not 8 <= len(tokens) <= 25:
            continue
        usable = [
            i
            for i, token in enumerate(tokens)
            if i > 0 and _core(token).isalpha() and len(_core(token)) >= 4
            and _core(token).lower() not in _DICTATION_SKIP_WORDS
            and _core(token).lower() not in avoid
        ]
        if len(usable) >= DICTATION_BLANKS:
            candidates.append((abs((segment.start + segment.end) / 2 - middle), tokens, usable))
    if not candidates:
        return None
    _, tokens, usable = min(candidates, key=lambda item: item[0])
    # three picks spread evenly over the usable words
    picks = sorted({usable[round(k * (len(usable) - 1) / (DICTATION_BLANKS - 1))] for k in range(DICTATION_BLANKS)})
    answers = []
    for i in picks:
        core = _core(tokens[i])
        answers.append(core)
        tokens[i] = tokens[i].replace(core, "____", 1)
    return " ".join(tokens), answers


def _mmss(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def build_monday_message(content: WeeklyContent) -> str:
    chunk_blocks = [
        f"{bold(f'{index}. {chunk.phrase}')}\n{esc(chunk.definition)}\n{italic(chunk.context_sentence)}"
        for index, chunk in enumerate(content.chunks, start=1)
    ]
    lines = [
        "(This part of the episode is sent as audio just before this message.)",
        "",
        f"Episode: {esc(content.episode_title)}",
        f"Listen: {_mmss(content.segment_start)} – {_mmss(content.segment_end)}",
    ]
    if content.episode_link:
        lines.append(f"Full episode: {esc(content.episode_link)}")
    if not content.listening_check:
        content_html = "\n".join(lines) + "\n\n" + "\n\n".join(chunk_blocks)
        return render_task_card(
            TaskCard(
                day_code="mon",
                week_number=content.week_number,
                goal="Understand one interview clip and learn 3 chunks",
                duration="~20 min",
                reply_mode="Text or voice",
                content_html=content_html,
                steps=[
                    "Listen to the audio clip (or that part of the full episode)",
                    "Pick one chunk and use it in your own real-life sentence",
                    DONE_STEP,
                ],
            )
        )
    parts = [f"{bold('1. Gist')}\nIn one or two English sentences: what is the guest talking about?"]
    if content.dictation_prompt:
        parts.append(
            f"{bold('2. Dictation')}\nFill the {len(content.dictation_answers)} gaps with the exact words you hear:\n"
            f"{italic(content.dictation_prompt)}"
        )
    else:
        parts.append(f"{bold('2. Dictation')}\n(No dictation this week -- skip to 3.)")
    parts.append(f"{bold('3. Chunk')}\nUse one of these in a sentence about your own life:\n\n" + "\n\n".join(chunk_blocks))
    content_html = "\n".join(lines) + "\n\n" + "\n\n".join(parts)
    return render_task_card(
        TaskCard(
            day_code="mon",
            week_number=content.week_number,
            goal="Understand one interview clip and learn 3 chunks",
            duration="~25 min",
            reply_mode="One message, by number",
            content_html=content_html,
            steps=[
                "Listen to the audio clip as many times as you like",
                "Reply in one message: 1. the gist  2. the missing words  3. your sentence "
                "(type the dictation; voice is fine for 1 and 3)",
                DONE_STEP,
            ],
        )
    )


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
    send_audio: Callable[[str, Path, str], None] | None = None,
) -> WeeklyContent | None:
    """Full Monday pipeline, on the newest episode not used yet. Returns None
    (and sends nothing) only when every full-length episode in the feed has
    already been used."""
    rss_xml = get_text(rss_url, timeout=30)
    if not any(episode.enclosure_url for episode in full_length_episodes(rss_xml)):
        raise ValueError("BBC RSS feed returned no usable episode with an audio enclosure")
    episode = pick_unused_episode(rss_xml, lambda guid: is_episode_processed(qdrant, collection, guid))
    if episode is None:
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
    chunk_words = {_core(word).lower() for chunk in chunks for word in chunk.phrase.split()}
    dictation = choose_dictation(
        transcript.segments, float(window["start_seconds"]), float(window["end_seconds"]), chunk_words
    )
    content = WeeklyContent(
        week_number=week_number,
        episode_title=episode.title,
        episode_guid=episode.guid,
        segment_start=INTRO_SKIP_SECONDS + float(window["start_seconds"]),
        segment_end=INTRO_SKIP_SECONDS + float(window["end_seconds"]),
        transcript_excerpt=window["excerpt_text"],
        chunks=chunks,
        window_segments=transcript.segments,
        episode_link=(episode.link or "").replace("http://www.bbc.co.uk/", "https://www.bbc.co.uk/"),
        listening_check=True,
        dictation_prompt=dictation[0] if dictation else "",
        dictation_answers=dictation[1] if dictation else [],
    )
    store_weekly_content(qdrant, embeddings, collection, content)

    # The selected stretch as its own audio clip, so the listening task can be
    # done straight from Telegram (window.mp3 starts at INTRO_SKIP_SECONDS,
    # so the window's own timestamps are already relative to it).
    # .ogg -> Opus, delivered as a Telegram voice message (waveform + speed
    # control) rather than an easy-to-miss music attachment.
    clip_path = workspace_dir / "monday_clip.ogg"
    if send_audio is not None:
        clip_client.clip(window_path, float(window["start_seconds"]), float(window["end_seconds"]), clip_path)

    message = build_monday_message(content)
    vector = embeddings.embed(message)
    for owner in owners:
        if send_audio is not None:
            send_audio(owner, clip_path, "This week's listening -- the part the chunks come from.")
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
    reply -- added here while wiring up full daily-completion tracking.

    Weeks created with the listening check (gist + dictation) are graded
    on all three parts by evaluate_monday_listening instead."""
    try:
        payload = read_this_week_payload(qdrant, collection, week_number)
    except ValueError:
        payload = {}  # no stored week to read the listening check from -- grade the chunk alone
    if payload.get("listening_check"):
        return evaluate_monday_listening(
            qdrant, embeddings, llm, collection, owner, week_number, chunks, payload, transcribed_reply
        )
    chunk_usage = evaluate_chunk_usage(
        llm, chunks, transcribed_reply, model_answer_instruction=MONDAY_MODEL_ANSWER_INSTRUCTION
    )
    used_phrases: list[str] = []
    for result in chunk_usage["chunk_results"]:
        used = bool(result["used_correctly"])
        sentence = (result.get("user_sentence") or "").strip()
        if not used:
            continue
        used_phrases.append(result["phrase"])
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
            evaluation="mon",
        )
    mark_task_completed(qdrant, collection, week_number, "mon", owner)
    if used_phrases:
        result_lines = [f"✅ {phrase} — used correctly" for phrase in used_phrases]
        tips: list[str] = []
    else:
        result_lines = ["❌ No chunk from this week spotted"]
        tips = ["Use one of this week's chunks in a sentence about your own life"]
    return render_feedback_card(
        FeedbackCard(
            day_code="mon",
            result_lines=result_lines,
            tip_lines=tips,
            example=chunk_usage.get("model_answer") or "",
            answer=transcribed_reply,
        )
    )


MONDAY_LISTENING_SCHEMA = {
    "type": "object",
    "properties": {
        "gist_correct": {"type": "boolean"},
        "gist_feedback": {"type": "string"},
        "gist_model": {"type": "string"},
        "dictation_words": {"type": "array", "items": {"type": "string"}},
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
        },
        "model_answer": {"type": "string"},
    },
    "required": ["gist_correct", "gist_feedback", "gist_model", "dictation_words", "chunk_results", "model_answer"],
    "additionalProperties": False,
}


def _normalize_word(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower().replace("’", "'"))


def _within_one_edit(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    return any(longer[:i] + longer[i + 1 :] == shorter for i in range(len(longer)))


def dictation_word_correct(given: str, expected: str) -> bool:
    """Case- and punctuation-insensitive; one typo allowed on longer words,
    since the expected word itself comes from a Whisper transcript."""
    a, b = _normalize_word(given), _normalize_word(expected)
    if not a:
        return False
    return a == b or (len(b) >= 5 and _within_one_edit(a, b))


def _record_listening_check(
    qdrant: QdrantClient, collection: str, owner: str, week_number: int, fields: dict, vector_source
) -> None:
    """One record per learner per week, overwritten by each attempt."""
    tag = f"eng_wk{week_number}"
    existing = read_owned_points(qdrant, collection, owner, {"tag": tag, "kind": "listening_check"}, limit=1)
    if existing:
        qdrant.set_payload(collection, existing[0]["id"], fields)
        return
    text = f"week {week_number} listening check"
    write_owned_point(qdrant, collection, owner, text, vector_source(text), {"tag": tag, "kind": "listening_check", **fields})


def read_listening_check(qdrant: QdrantClient, collection: str, owner: str, week_number: int) -> dict | None:
    points = read_owned_points(
        qdrant, collection, owner, {"tag": f"eng_wk{week_number}", "kind": "listening_check"}, limit=1
    )
    return (points[0].get("payload") or {}) if points else None


def evaluate_monday_listening(
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    llm: LlmClient,
    collection: str,
    owner: str,
    week_number: int,
    chunks: list[dict],
    payload: dict,
    reply: str,
) -> str:
    """Monday with the listening check: one LLM call judges the gist against
    the transcript, pulls out the learner's dictation words (graded here,
    deterministically) and judges the chunk sentence. The feedback reveals
    the transcript -- the "check what you heard" step of intensive
    listening."""
    transcript = payload.get("transcript_excerpt") or ""
    dictation_prompt = payload.get("dictation_prompt") or ""
    answers = list(payload.get("dictation_answers") or [])
    phrase_list = "\n".join(f"- {c['phrase']}" for c in chunks)
    prompt = (
        "A language learner listened to a clip from a British radio interview and "
        "answered in one message: 1. the gist, 2. a dictation (the missing words), "
        "3. a sentence using one of this week's chunks.\n\n"
        f"What was said in the clip:\n\"{transcript}\"\n\n"
        f"Dictation sentence (with gaps):\n{dictation_prompt or '(none this week)'}\n\n"
        f"This week's chunks:\n{phrase_list}\n\n"
        f"The learner's reply (transcribed if spoken):\n\"{reply}\"\n\n"
        "gist_correct: does part 1 capture what the guest is mainly talking about? "
        "gist_feedback: one short sentence on what they got or missed. gist_model: a "
        "good one- or two-sentence gist. dictation_words: the words the learner gave "
        "for the gaps, in order, copied exactly as written (spelling mistakes "
        "included, empty string for a gap they left out). chunk_results: for each "
        "chunk, whether part 3 uses it correctly and naturally (a spoken form like "
        "a changed tense still counts) and the learner's sentence that uses it. "
        "model_answer: one natural real-life example sentence for each chunk, one "
        "per line."
    )
    data = json.loads(llm.chat_json(prompt, MONDAY_LISTENING_SCHEMA, schema_name="monday_listening", max_tokens=1000))

    result_lines = [f"{'✅' if data['gist_correct'] else '❌'} Gist — {data['gist_feedback'].strip()}"]
    tips: list[str] = []
    dictation_correct = 0
    if answers:
        given = list(data.get("dictation_words") or [])
        given += [""] * (len(answers) - len(given))
        marks = []
        for index, (word, expected) in enumerate(zip(given, answers), start=1):
            if dictation_word_correct(word, expected):
                dictation_correct += 1
                marks.append(f"    ✅ {index}. {expected}")
            else:
                heard = f'"{word}"' if word.strip() else "(blank)"
                marks.append(f"    ❌ {index}. {heard} → {expected}")
        result_lines.append(f"Dictation {dictation_correct}/{len(answers)}")
        result_lines += marks
        if dictation_correct < len(answers):
            tips.append("Replay the clip and listen for the words you missed -- the transcript is below")

    used_phrases = []
    for result in data["chunk_results"]:
        if not result["used_correctly"]:
            continue
        used_phrases.append(result["phrase"])
        record_chunk_usage(
            qdrant, embeddings, collection, owner, week_number, result["phrase"],
            used_correctly=True, user_sentence=(result.get("user_sentence") or "").strip(),
            source="mon_reply", evaluation="mon",
        )
    if used_phrases:
        result_lines += [f"✅ {phrase} — used correctly" for phrase in used_phrases]
    else:
        result_lines.append("❌ No chunk from this week spotted")
        tips.append("Use one of this week's chunks in a sentence about your own life")

    _record_listening_check(
        qdrant, collection, owner, week_number,
        {"gist_correct": bool(data["gist_correct"]), "dictation_correct": dictation_correct,
         "dictation_total": len(answers)},
        embeddings.embed,
    )
    mark_task_completed(qdrant, collection, week_number, "mon", owner)
    example = (
        f"Gist:\n{data['gist_model'].strip()}\n\n"
        f"Chunk examples:\n{data['model_answer'].strip()}\n\n"
        f"What was said:\n{transcript}"
    )
    return render_feedback_card(
        FeedbackCard(day_code="mon", result_lines=result_lines, tip_lines=tips, example=example, answer=reply)
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




def build_tuesday_message(annotated_text: str, week_number: int) -> str:
    content_html = (
        "(The audio clip is sent just before this message.)\n\n"
        f"{markdown_bold_to_html(annotated_text)}\n\n"
        + italic(
            "Bold = stress, / = natural pause, (schwa) = weak form. A best guess "
            "from general pronunciation rules, not a measurement of the recording."
        )
    )
    return render_task_card(
        TaskCard(
            day_code="tue",
            week_number=week_number,
            goal="Copy the stress and pauses",
            duration="~10 min",
            reply_mode="Voice",
            content_html=content_html,
            steps=["Listen to the clip twice", "Shadow it and record a voice message", DONE_STEP],
        )
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

    message = build_tuesday_message(annotated, week_number)
    vector = embeddings.embed(message)
    for owner in owners:
        # Audio first: the task card below refers to "the clip sent just
        # before this message".
        send_audio(owner, clip_path, "Shadow this clip -- listen, then record yourself echoing it.")
        send_message(owner, message)
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


def analyze_tuesday_shadowing_in_chinese(
    llm: LlmClient,
    reference_text: str,
    transcribed_reply: str,
    diff: WordDiffResult,
    wpm: float,
) -> dict:
    """Chinese-language analysis of THIS shadowing attempt's correctness,
    generated only AFTER the user's reply (spec change: the Tuesday push
    message itself stays English + stress-annotation only -- Chinese
    content lives entirely in the evaluation step). No translation of the
    original text -- per user request, the point is judging accuracy
    (pronunciation/word/pace fidelity to the original), not comprehension."""
    missing = ", ".join(diff.missing_words) or "無"
    extra = ", ".join(diff.extra_words) or "無"
    prompt = (
        "A language learner just did a shadowing (echo-reading) exercise in English.\n\n"
        f"Original text:\n{reference_text}\n\n"
        f"What they said (transcribed):\n{transcribed_reply}\n\n"
        f"Missing/changed words: {missing}\n"
        f"Extra words: {extra}\n"
        f"Their speaking pace: {wpm:.0f} words per minute.\n\n"
        "In Traditional Chinese (繁體中文), give a short, encouraging analysis "
        "of the CORRECTNESS of their shadowing attempt -- not a translation "
        "of the original text. Comment on which words they got right, which "
        "they missed or changed, and whether their pace was close to the "
        "original."
    )
    raw = llm.chat_json(
        prompt, CHINESE_SHADOWING_FEEDBACK_SCHEMA, schema_name="tuesday_chinese_feedback", max_tokens=500
    )
    return json.loads(raw)


def evaluate_tuesday_reply(
    llm: LlmClient,
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
    result_lines = [f"Word match {match_pct:.0f}% (word-level, not letter-level)"]
    if diff.missing_words:
        result_lines.append(f"❌ Missed/changed: {', '.join(diff.missing_words[:8])}")
    if diff.extra_words:
        result_lines.append(f"❌ Extra: {', '.join(diff.extra_words[:8])}")
    if not diff.missing_words and not diff.extra_words:
        result_lines.append("✅ No missed or extra words")
    result_lines.append(f"Pace: {wpm} WPM (aim to match the clip, not a fixed target)")
    feedback_zh = analyze_tuesday_shadowing_in_chinese(llm, reference_text, transcribed_reply, diff, wpm)
    mark_task_completed(qdrant, collection, week_number, "tue", owner)
    return render_feedback_card(
        FeedbackCard(
            day_code="tue",
            result_lines=result_lines,
            tip_lines=[feedback_zh["analysis_zh"]],
            example=reference_text,
            answer=transcribed_reply,
        )
    )


# ---------------------------------------------------------------------------
# Wednesday: IELTS Speaking Part 2 (spec 2.3). Fixed question bank, Part 2
# only for now -- the week_number > 17 Part 2+3 combo is v1.16, not here.
#
# The first 12 cue cards below are the verbatim English text the consultant
# actually provided (IELTS Liz / IELTS Advantage sourcing, per spec 2.3),
# not a paraphrase -- the spec file only carries the short Chinese topic
# summaries, so the literal English wording is transcribed here from the
# original conversation turn where it was given. The other 100 (added after
# the list) take the bank to 112, well past the spec's 60-question target,
# so no cue card repeats across the 38-week plan.
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

# The other 100 cue cards (25 per category), taking the bank past the spec's
# 60-question target to 112 -- over two years of Wednesdays without a repeat.
# Same Describe / You should say / and explain shape as the 12 above.
IELTS_QUESTION_BANK += [
    {"id": "ielts_p2_event_04", "category": "event", "cue_card": "Describe a time you learned something important from a mistake.\n\nYou should say:\n- when it happened\n- what the mistake was\n- what you did about it\nand explain what you learned from it."},
    {"id": "ielts_p2_event_05", "category": "event", "cue_card": "Describe a celebration you really enjoyed.\n\nYou should say:\n- what the occasion was\n- where it took place\n- who was there\nand explain why it was so memorable."},
    {"id": "ielts_p2_event_06", "category": "event", "cue_card": "Describe a time you had to give a presentation or speak in public.\n\nYou should say:\n- when and where it was\n- who the audience was\n- how you prepared\nand explain how you felt before and after."},
    {"id": "ielts_p2_event_07", "category": "event", "cue_card": "Describe a time you were stuck waiting for a long time.\n\nYou should say:\n- where you were\n- why there was a delay\n- what you did while you waited\nand explain how you dealt with the frustration."},
    {"id": "ielts_p2_event_08", "category": "event", "cue_card": "Describe a time you disagreed with someone at work or university.\n\nYou should say:\n- who the person was\n- what you disagreed about\n- how it was resolved\nand explain what you would do differently next time."},
    {"id": "ielts_p2_event_09", "category": "event", "cue_card": "Describe a trip that did not go as planned.\n\nYou should say:\n- where you were going\n- what went wrong\n- what you did about it\nand explain whether you would go there again."},
    {"id": "ielts_p2_event_10", "category": "event", "cue_card": "Describe a time you tried a new sport or hobby for the first time.\n\nYou should say:\n- what it was\n- why you decided to try it\n- how it went\nand explain whether you have kept doing it."},
    {"id": "ielts_p2_event_11", "category": "event", "cue_card": "Describe a time you received some very good news.\n\nYou should say:\n- what the news was\n- how you found out\n- who you told first\nand explain why it mattered so much to you."},
    {"id": "ielts_p2_event_12", "category": "event", "cue_card": "Describe a time you had to make a difficult decision.\n\nYou should say:\n- what the decision was\n- what options you had\n- what you finally chose\nand explain whether you think it was the right choice."},
    {"id": "ielts_p2_event_13", "category": "event", "cue_card": "Describe a time you lost something important.\n\nYou should say:\n- what you lost\n- where you think you lost it\n- how you tried to find it\nand explain how the experience affected you."},
    {"id": "ielts_p2_event_14", "category": "event", "cue_card": "Describe a time you organised an event or gathering.\n\nYou should say:\n- what the event was\n- who came\n- what you had to arrange\nand explain whether it was a success."},
    {"id": "ielts_p2_event_15", "category": "event", "cue_card": "Describe a time you worked in a team to achieve something.\n\nYou should say:\n- what the goal was\n- what your role was\n- what the result was\nand explain what made the teamwork effective."},
    {"id": "ielts_p2_people_04", "category": "people", "cue_card": "Describe a teacher who had a strong influence on you.\n\nYou should say:\n- who the teacher was\n- what subject they taught\n- what they did that stood out\nand explain why they influenced you so much."},
    {"id": "ielts_p2_people_05", "category": "people", "cue_card": "Describe a person you admire who is very good at their job.\n\nYou should say:\n- who the person is\n- what job they do\n- how you know them\nand explain what makes them so good at it."},
    {"id": "ielts_p2_people_06", "category": "people", "cue_card": "Describe a family member you spend a lot of time with.\n\nYou should say:\n- who the person is\n- what you do together\n- how often you see each other\nand explain why you enjoy their company."},
    {"id": "ielts_p2_people_07", "category": "people", "cue_card": "Describe a person who helped you settle into a new place.\n\nYou should say:\n- who the person was\n- where you had moved to\n- how they helped you\nand explain what difference their help made."},
    {"id": "ielts_p2_people_08", "category": "people", "cue_card": "Describe a well-known person you would like to meet.\n\nYou should say:\n- who the person is\n- what they are known for\n- what you would ask them\nand explain why you would like to meet them."},
    {"id": "ielts_p2_people_09", "category": "people", "cue_card": "Describe a neighbour you know well.\n\nYou should say:\n- who the person is\n- how you got to know them\n- what you do for each other\nand explain what you think makes a good neighbour."},
    {"id": "ielts_p2_people_10", "category": "people", "cue_card": "Describe someone you know who is very creative.\n\nYou should say:\n- who the person is\n- what they create\n- how you know them\nand explain what you find most impressive about them."},
    {"id": "ielts_p2_people_11", "category": "people", "cue_card": "Describe a person who stays calm under pressure.\n\nYou should say:\n- who the person is\n- a situation where you saw this\n- how they behaved\nand explain what you learned from watching them."},
    {"id": "ielts_p2_people_12", "category": "people", "cue_card": "Describe a friend you have known for a long time.\n\nYou should say:\n- how you met\n- what you usually do together\n- how the friendship has changed\nand explain why the friendship has lasted."},
    {"id": "ielts_p2_people_13", "category": "people", "cue_card": "Describe someone who taught you a practical skill.\n\nYou should say:\n- who the person was\n- what skill they taught you\n- how they taught it\nand explain how useful the skill has been."},
    {"id": "ielts_p2_people_14", "category": "people", "cue_card": "Describe a person who changed your mind about something.\n\nYou should say:\n- who the person was\n- what the topic was\n- how they changed your mind\nand explain why you were persuaded."},
    {"id": "ielts_p2_people_15", "category": "people", "cue_card": "Describe a child you know who impressed you.\n\nYou should say:\n- who the child is\n- what they did\n- how you reacted\nand explain why it impressed you."},
    {"id": "ielts_p2_place_04", "category": "place", "cue_card": "Describe a cafe or restaurant you like to go to.\n\nYou should say:\n- where it is\n- what you usually order\n- who you go there with\nand explain why you like it so much."},
    {"id": "ielts_p2_place_05", "category": "place", "cue_card": "Describe a park or garden in your area.\n\nYou should say:\n- where it is\n- what it looks like\n- when you usually go there\nand explain why it matters to local people."},
    {"id": "ielts_p2_place_06", "category": "place", "cue_card": "Describe a historic building you have visited.\n\nYou should say:\n- where it is\n- what it looked like\n- what you learned there\nand explain why you think it is worth preserving."},
    {"id": "ielts_p2_place_07", "category": "place", "cue_card": "Describe a place where you like to study or work.\n\nYou should say:\n- where it is\n- what it is like\n- how often you go there\nand explain why it helps you get things done."},
    {"id": "ielts_p2_place_08", "category": "place", "cue_card": "Describe a place you would like to visit in the future.\n\nYou should say:\n- where it is\n- how you heard about it\n- what you would do there\nand explain why you want to go there."},
    {"id": "ielts_p2_place_09", "category": "place", "cue_card": "Describe a very busy place you have visited.\n\nYou should say:\n- where it was\n- why it was so busy\n- what you did there\nand explain how you felt about being there."},
    {"id": "ielts_p2_place_10", "category": "place", "cue_card": "Describe a place near water that you enjoy.\n\nYou should say:\n- where it is\n- what you do there\n- how often you go\nand explain why it appeals to you."},
    {"id": "ielts_p2_place_11", "category": "place", "cue_card": "Describe a museum or gallery you have been to.\n\nYou should say:\n- which one it was\n- what you saw there\n- who you went with\nand explain whether you would recommend it."},
    {"id": "ielts_p2_place_12", "category": "place", "cue_card": "Describe a home, other than your own, that you enjoy visiting.\n\nYou should say:\n- whose home it is\n- what it is like\n- what you do when you are there\nand explain why you enjoy going there."},
    {"id": "ielts_p2_place_13", "category": "place", "cue_card": "Describe a shop you like that does not sell food.\n\nYou should say:\n- where it is\n- what you buy there\n- how often you go\nand explain why you prefer it to other shops."},
    {"id": "ielts_p2_place_14", "category": "place", "cue_card": "Describe a sports venue or gym you have been to.\n\nYou should say:\n- where it is\n- what you did there\n- what the atmosphere was like\nand explain how being there made you feel."},
    {"id": "ielts_p2_place_15", "category": "place", "cue_card": "Describe a place in your hometown that has changed a lot.\n\nYou should say:\n- where it is\n- what it used to be like\n- how it has changed\nand explain whether you think the change is for the better."},
    {"id": "ielts_p2_object_04", "category": "object", "cue_card": "Describe a book you have read more than once.\n\nYou should say:\n- what the book is\n- when you first read it\n- what it is about\nand explain why you went back to it."},
    {"id": "ielts_p2_object_05", "category": "object", "cue_card": "Describe a gift you gave to someone.\n\nYou should say:\n- what the gift was\n- who you gave it to\n- why you chose it\nand explain how the person reacted."},
    {"id": "ielts_p2_object_06", "category": "object", "cue_card": "Describe an app or website you use very often.\n\nYou should say:\n- what it is\n- what you use it for\n- how often you use it\nand explain why it is so useful to you."},
    {"id": "ielts_p2_object_07", "category": "object", "cue_card": "Describe something you bought recently that you are happy with.\n\nYou should say:\n- what it is\n- where you bought it\n- why you bought it\nand explain why you are pleased with it."},
    {"id": "ielts_p2_object_08", "category": "object", "cue_card": "Describe a photograph you like.\n\nYou should say:\n- what it shows\n- who took it\n- where you keep it\nand explain why it is special to you."},
    {"id": "ielts_p2_object_09", "category": "object", "cue_card": "Describe a piece of furniture in your home.\n\nYou should say:\n- what it is\n- where it is in your home\n- how long you have had it\nand explain why it is important to you."},
    {"id": "ielts_p2_object_10", "category": "object", "cue_card": "Describe something you borrowed from someone.\n\nYou should say:\n- what you borrowed\n- who you borrowed it from\n- why you needed it\nand explain how you felt about borrowing it."},
    {"id": "ielts_p2_object_11", "category": "object", "cue_card": "Describe a household appliance you could not live without.\n\nYou should say:\n- what it is\n- how often you use it\n- how it helps you\nand explain why it is so essential."},
    {"id": "ielts_p2_object_12", "category": "object", "cue_card": "Describe a toy or game you enjoyed as a child.\n\nYou should say:\n- what it was\n- who gave it to you\n- how you played with it\nand explain why you remember it so well."},
    {"id": "ielts_p2_object_13", "category": "object", "cue_card": "Describe something you made by hand.\n\nYou should say:\n- what you made\n- how you made it\n- how long it took\nand explain how you felt when it was finished."},
    {"id": "ielts_p2_object_14", "category": "object", "cue_card": "Describe a piece of art or decoration you like.\n\nYou should say:\n- what it is\n- where it is\n- what it looks like\nand explain why you like it."},
    {"id": "ielts_p2_object_15", "category": "object", "cue_card": "Describe a vehicle you have owned or used a lot.\n\nYou should say:\n- what it is\n- how long you have used it\n- what you use it for\nand explain what you like or dislike about it."},
    {"id": "ielts_p2_event_16", "category": "event", "cue_card": "Describe a time you helped a stranger.\n\nYou should say:\n- who the person was\n- what situation they were in\n- what you did to help\nand explain how you felt afterwards."},
    {"id": "ielts_p2_event_17", "category": "event", "cue_card": "Describe a time you changed an important plan.\n\nYou should say:\n- what the plan was\n- what made you change it\n- what you did instead\nand explain whether you regret the change."},
    {"id": "ielts_p2_event_18", "category": "event", "cue_card": "Describe a time you had to learn something very quickly.\n\nYou should say:\n- what you had to learn\n- why you were in a hurry\n- how you learned it\nand explain how well it went in the end."},
    {"id": "ielts_p2_event_19", "category": "event", "cue_card": "Describe a time you got lost.\n\nYou should say:\n- where you were\n- how you got lost\n- how you found your way\nand explain what you would do differently next time."},
    {"id": "ielts_p2_event_20", "category": "event", "cue_card": "Describe a time you received useful feedback.\n\nYou should say:\n- who gave you the feedback\n- what it was about\n- what you changed afterwards\nand explain why it was so useful."},
    {"id": "ielts_p2_event_21", "category": "event", "cue_card": "Describe a time technology made a task much easier for you.\n\nYou should say:\n- what the task was\n- what technology you used\n- how it helped\nand explain whether you would go back to the old way."},
    {"id": "ielts_p2_event_22", "category": "event", "cue_card": "Describe an important journey you made.\n\nYou should say:\n- where you went\n- why you made the journey\n- how you travelled\nand explain why the journey was important to you."},
    {"id": "ielts_p2_event_23", "category": "event", "cue_card": "Describe a time you had to apologise to someone.\n\nYou should say:\n- who you apologised to\n- what you apologised for\n- how they reacted\nand explain what you learned from the situation."},
    {"id": "ielts_p2_event_24", "category": "event", "cue_card": "Describe a time you really enjoyed an outdoor activity.\n\nYou should say:\n- what the activity was\n- where you did it\n- who you were with\nand explain why you enjoyed it so much."},
    {"id": "ielts_p2_event_25", "category": "event", "cue_card": "Describe a time you saved money for something special.\n\nYou should say:\n- what you were saving for\n- how long it took\n- how you managed to save\nand explain whether it was worth it."},
    {"id": "ielts_p2_event_26", "category": "event", "cue_card": "Describe a time you had to be very patient.\n\nYou should say:\n- when it was\n- what the situation was\n- how you stayed patient\nand explain how things turned out."},
    {"id": "ielts_p2_event_27", "category": "event", "cue_card": "Describe a time a friend surprised you.\n\nYou should say:\n- who the friend was\n- what the surprise was\n- how you reacted\nand explain why it meant a lot to you."},
    {"id": "ielts_p2_event_28", "category": "event", "cue_card": "Describe a time you achieved a personal goal.\n\nYou should say:\n- what the goal was\n- how long it took\n- what obstacles you faced\nand explain how it felt to achieve it."},
    {"id": "ielts_p2_people_16", "category": "people", "cue_card": "Describe a person who inspired you to learn a new skill.\n\nYou should say:\n- who the person is\n- what skill it was\n- how they inspired you\nand explain what effect it has had on you."},
    {"id": "ielts_p2_people_17", "category": "people", "cue_card": "Describe a colleague or classmate you enjoy working with.\n\nYou should say:\n- who the person is\n- what they do\n- how you work together\nand explain why you enjoy working with them."},
    {"id": "ielts_p2_people_18", "category": "people", "cue_card": "Describe someone you know who has an interesting job.\n\nYou should say:\n- who the person is\n- what their job is\n- what a typical day looks like for them\nand explain why you find the job interesting."},
    {"id": "ielts_p2_people_19", "category": "people", "cue_card": "Describe a person you know who is very well organised.\n\nYou should say:\n- who the person is\n- how you know them\n- how their organisation shows\nand explain whether you would like to be more like them."},
    {"id": "ielts_p2_people_20", "category": "people", "cue_card": "Describe a person you would like to work with in the future.\n\nYou should say:\n- who the person is\n- what they do\n- what you would like to work on together\nand explain why you would like to work with them."},
    {"id": "ielts_p2_people_21", "category": "people", "cue_card": "Describe an older person you respect.\n\nYou should say:\n- who the person is\n- how you know them\n- what they have done in their life\nand explain why you respect them."},
    {"id": "ielts_p2_people_22", "category": "people", "cue_card": "Describe a person who is good at making others laugh.\n\nYou should say:\n- who the person is\n- how they make people laugh\n- a time they did this\nand explain why a sense of humour matters."},
    {"id": "ielts_p2_people_23", "category": "people", "cue_card": "Describe someone you know who travels a lot.\n\nYou should say:\n- who the person is\n- where they usually go\n- why they travel so much\nand explain whether you would like that kind of lifestyle."},
    {"id": "ielts_p2_people_24", "category": "people", "cue_card": "Describe a person who gave you a memorable present.\n\nYou should say:\n- who the person is\n- what the present was\n- what the occasion was\nand explain why you still remember it."},
    {"id": "ielts_p2_people_25", "category": "people", "cue_card": "Describe a sportsperson you admire.\n\nYou should say:\n- who the person is\n- what sport they play\n- what they have achieved\nand explain why you admire them."},
    {"id": "ielts_p2_people_26", "category": "people", "cue_card": "Describe someone you know who made a big change in their life.\n\nYou should say:\n- who the person is\n- what the change was\n- why they made it\nand explain what you think of their decision."},
    {"id": "ielts_p2_people_27", "category": "people", "cue_card": "Describe a person you know who is a very good listener.\n\nYou should say:\n- who the person is\n- how you know them\n- a time they listened to you\nand explain why this quality matters."},
    {"id": "ielts_p2_people_28", "category": "people", "cue_card": "Describe a character from a book, film or TV series that you like.\n\nYou should say:\n- who the character is\n- what story they are in\n- what they are like\nand explain why you like this character."},
    {"id": "ielts_p2_place_16", "category": "place", "cue_card": "Describe a library you have used.\n\nYou should say:\n- where it is\n- what it is like inside\n- what you use it for\nand explain whether you think libraries still matter today."},
    {"id": "ielts_p2_place_17", "category": "place", "cue_card": "Describe a beautiful view you remember.\n\nYou should say:\n- where it was\n- what you could see\n- when you saw it\nand explain why it has stayed with you."},
    {"id": "ielts_p2_place_18", "category": "place", "cue_card": "Describe a place where you spent a lot of time as a child.\n\nYou should say:\n- where it is\n- what you did there\n- who you were with\nand explain how you feel about it now."},
    {"id": "ielts_p2_place_19", "category": "place", "cue_card": "Describe a hotel or other place you stayed on holiday.\n\nYou should say:\n- where it was\n- what it was like\n- how long you stayed\nand explain whether you would stay there again."},
    {"id": "ielts_p2_place_20", "category": "place", "cue_card": "Describe a quiet street or neighbourhood you like.\n\nYou should say:\n- where it is\n- what it is like\n- why you go there\nand explain what makes it so pleasant."},
    {"id": "ielts_p2_place_21", "category": "place", "cue_card": "Describe a workplace you have visited or worked in.\n\nYou should say:\n- where it was\n- what it looked like\n- what people did there\nand explain whether you would like to work there."},
    {"id": "ielts_p2_place_22", "category": "place", "cue_card": "Describe a place in your country that is popular with tourists.\n\nYou should say:\n- where it is\n- what visitors do there\n- when it is busiest\nand explain whether you think it deserves its popularity."},
    {"id": "ielts_p2_place_23", "category": "place", "cue_card": "Describe a place you go to when you want to be active.\n\nYou should say:\n- where it is\n- what you do there\n- how often you go\nand explain how it helps you."},
    {"id": "ielts_p2_place_24", "category": "place", "cue_card": "Describe a community centre or public space in your area.\n\nYou should say:\n- where it is\n- what happens there\n- who uses it\nand explain why it is important to the community."},
    {"id": "ielts_p2_place_25", "category": "place", "cue_card": "Describe a place with interesting architecture.\n\nYou should say:\n- where it is\n- what the buildings look like\n- when you saw them\nand explain why you find the architecture interesting."},
    {"id": "ielts_p2_place_26", "category": "place", "cue_card": "Describe a train station or airport you have passed through.\n\nYou should say:\n- which one it was\n- what it was like\n- why you were there\nand explain whether it worked well for travellers."},
    {"id": "ielts_p2_place_27", "category": "place", "cue_card": "Describe a place where you had a memorable meal.\n\nYou should say:\n- where it was\n- what you ate\n- who you were with\nand explain why the meal was so memorable."},
    {"id": "ielts_p2_place_28", "category": "place", "cue_card": "Describe a countryside area you have visited.\n\nYou should say:\n- where it is\n- what it looked like\n- what you did there\nand explain how it compared with life in a city."},
    {"id": "ielts_p2_object_16", "category": "object", "cue_card": "Describe a film or series you watched recently.\n\nYou should say:\n- what it was\n- where you watched it\n- who you watched it with\nand explain what you thought of it."},
    {"id": "ielts_p2_object_17", "category": "object", "cue_card": "Describe a piece of jewellery or an accessory you wear often.\n\nYou should say:\n- what it is\n- where it came from\n- how long you have had it\nand explain why you wear it so often."},
    {"id": "ielts_p2_object_18", "category": "object", "cue_card": "Describe something you would like to buy in the future.\n\nYou should say:\n- what it is\n- why you want it\n- how you plan to pay for it\nand explain why it matters to you."},
    {"id": "ielts_p2_object_19", "category": "object", "cue_card": "Describe a musical instrument you play or would like to play.\n\nYou should say:\n- which instrument it is\n- how long you have played or why you want to\n- how you are learning or would learn\nand explain why you like this instrument."},
    {"id": "ielts_p2_object_20", "category": "object", "cue_card": "Describe an item you always carry with you.\n\nYou should say:\n- what it is\n- how long you have carried it\n- how often you use it\nand explain why you always keep it with you."},
    {"id": "ielts_p2_object_21", "category": "object", "cue_card": "Describe a letter or message that was important to you.\n\nYou should say:\n- who it was from\n- what it said\n- when you received it\nand explain why it was so important."},
    {"id": "ielts_p2_object_22", "category": "object", "cue_card": "Describe a piece of sports or outdoor equipment you use.\n\nYou should say:\n- what it is\n- how often you use it\n- where you use it\nand explain how it helps you."},
    {"id": "ielts_p2_object_23", "category": "object", "cue_card": "Describe something old that you still use.\n\nYou should say:\n- what it is\n- how old it is\n- what you use it for\nand explain why you have not replaced it."},
    {"id": "ielts_p2_object_24", "category": "object", "cue_card": "Describe a song or piece of music that means a lot to you.\n\nYou should say:\n- what it is\n- when you first heard it\n- when you listen to it\nand explain why it is meaningful to you."},
    {"id": "ielts_p2_object_25", "category": "object", "cue_card": "Describe a map, guidebook or app that helped you on a trip.\n\nYou should say:\n- what it was\n- where the trip was\n- how it helped you\nand explain whether you would use it again."},
    {"id": "ielts_p2_object_26", "category": "object", "cue_card": "Describe a plant or garden you look after.\n\nYou should say:\n- what it is\n- where it is\n- how you look after it\nand explain why you enjoy caring for it."},
    {"id": "ielts_p2_object_27", "category": "object", "cue_card": "Describe a kitchen tool or gadget you use a lot.\n\nYou should say:\n- what it is\n- how often you use it\n- what you use it for\nand explain why it is so useful."},
    {"id": "ielts_p2_object_28", "category": "object", "cue_card": "Describe an advertisement you remember well.\n\nYou should say:\n- what it was for\n- where you saw it\n- what it showed\nand explain why it stuck in your memory."},
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
        "model_answer": {"type": "string", "minLength": 1},
    },
    "required": [
        "situation_present",
        "task_present",
        "action_present",
        "result_present",
        "overused_words",
        "model_answer",
    ],
    "additionalProperties": False,
}


def pick_ielts_question(qdrant: QdrantClient, collection: str) -> dict:
    """Random pick from the fixed bank, excluding questions already asked
    (tag:eng_ielts_topics, shared, no owner -- spec 2.3 Agent step 1). Once
    every question has been asked, the "already asked" exclusion resets
    (picks from the full bank again) rather than raising -- the bank cycling
    is preferable to the task silently failing to run."""
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


def _cue_card_html(cue_card: str) -> str:
    """The bank's cue cards use "- " bullets; show them as "• "."""
    lines = []
    for line in cue_card.splitlines():
        lines.append(f"• {esc(line[2:])}" if line.startswith("- ") else esc(line))
    return "\n".join(lines)


def build_wednesday_message(question: dict, week_number: int, part3_question: str | None = None) -> str:
    content_html = _cue_card_html(question["cue_card"])
    if part3_question:
        content_html += f"\n\n{bold('Part 3')}\n{esc(part3_question)}"
        card = TaskCard(
            day_code="wed",
            week_number=week_number,
            goal="Tell one STAR story, then argue one side (Part 2 + 3)",
            duration="~6 min",
            reply_mode="One voice message, ~2 min",
            content_html=content_html,
            steps=[
                "Think for 1 minute — no written draft",
                "Speak ~1 min on Part 2 (Situation, Task, Action, Result), then ~1 min on "
                "Part 3 (claim, counter-argument, conclusion)",
                DONE_STEP,
            ],
        )
    else:
        card = TaskCard(
            day_code="wed",
            week_number=week_number,
            goal="Tell one complete story using STAR",
            duration="~5 min",
            reply_mode="Voice, 1.5–2 min",
            content_html=content_html,
            steps=[
                "Think for 1 minute — no written draft",
                "Record 1.5–2 minutes covering Situation, Task, Action, Result",
                DONE_STEP,
            ],
        )
    return render_task_card(card)

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
        "model_answer": {"type": "string", "minLength": 1},
    },
    "required": ["claim_present", "concession_present", "conclusion_present", "model_answer"],
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
        "limitation), Conclusion (wraps up with a final judgment). Then "
        "write model_answer: an IELTS band 8 spoken answer to the same "
        "question (about 1 minute, 120-160 words) with a clear Claim, "
        "Concession and Conclusion, in natural spoken English -- a "
        "reference the speaker can compare their own answer against."
    )
    raw = llm.chat_json(
        prompt, ARGUMENT_EVAL_SCHEMA, schema_name="part3_argument_evaluation", max_tokens=800
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

    message = build_wednesday_message(question, week_number, part3_question=part3_question)
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
        "overused and suggest an IELTS band-7.5+ alternative for each. "
        "Finally, write model_answer: an IELTS band 8 spoken answer to the "
        "same cue card (about 2 minutes, 250-300 words) that covers every "
        "cue-card point and all four STAR elements, in natural spoken "
        "English rather than written prose. Build it on the speaker's own "
        "story and details where they gave any, so it reads as a better "
        "version of their answer, and use the suggested band-7.5+ "
        "alternatives where they fit."
    )
    raw = llm.chat_json(prompt, STAR_EVAL_SCHEMA, schema_name="star_evaluation", max_tokens=1200)
    data = json.loads(raw)

    star_elements = {
        "Situation": data["situation_present"],
        "Task": data["task_present"],
        "Action": data["action_present"],
        "Result": data["result_present"],
    }
    star_line = "　".join(f"{'✅' if present else '❌'} {name}" for name, present in star_elements.items())
    missing = [name for name, present in star_elements.items() if not present]
    result_lines = [f"Part 2: {star_line}" if part3_question else star_line]
    tips = [f"Add the missing STAR part: {', '.join(missing)}"] if missing else []
    tips += [f'"{item["word"]}" → "{item["replacement"]}" (band 7.5+)' for item in data["overused_words"]]
    example = (data.get("model_answer") or "").strip()

    if part3_question:
        argument = evaluate_part3_argument(llm, part3_question, transcribed_reply)
        argument_elements = {
            "Claim": argument["claim_present"],
            "Concession": argument["concession_present"],
            "Conclusion": argument["conclusion_present"],
        }
        result_lines.append(
            "Part 3: "
            + "　".join(f"{'✅' if present else '❌'} {name}" for name, present in argument_elements.items())
        )
        argument_missing = [name for name, present in argument_elements.items() if not present]
        if argument_missing:
            tips.append(f"Part 3 needs: {', '.join(argument_missing)}")
        part3_example = (argument.get("model_answer") or "").strip()
        example = f"Part 2:\n{example}\n\nPart 3 — {part3_question}\n{part3_example}"

    mark_task_completed(qdrant, collection, week_number, "wed", owner)
    return render_feedback_card(
        FeedbackCard(
            day_code="wed",
            result_lines=result_lines,
            tip_lines=tips,
            example=example,
            answer=transcribed_reply,
        )
    )

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
        "model_answer": {"type": "string", "minLength": 1},
    },
    "required": [
        "anchor_present",
        "bounce_present",
        "vocabulary_original",
        "vocabulary_replacement",
        "banter_reply",
        "model_answer",
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


def build_thursday_message(category: str, opener: str, week_number: int) -> str:
    content_html = f"Topic: {esc(category.replace('_', ' '))}\n\n{italic(f'“{opener}”')}"
    return render_task_card(
        TaskCard(
            day_code="thu",
            week_number=week_number,
            goal="Keep the chat going with Anchor & Bounce",
            duration="~3 min",
            reply_mode="Voice",
            content_html=content_html,
            steps=[
                "Respond to what they said (Anchor)",
                "Share your own situation, then ask something back (Bounce)",
                DONE_STEP,
            ],
        )
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
    message = build_thursday_message(category, opener, week_number)
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
        "if you were the colleague responding to what they just said. "
        "Then write model_answer: a strong example reply to the opener "
        "(3-5 sentences, natural spoken British English) with a clear "
        "Anchor and Bounce, built on the speaker's own situation where "
        "they gave one -- a reference the speaker can compare their reply "
        "against."
    )
    raw = llm.chat_json(prompt, THURSDAY_EVAL_SCHEMA, schema_name="thursday_evaluation", max_tokens=800)
    data = json.loads(raw)

    result_lines = [
        "✅ Anchor (responded and shared your situation)"
        if data["anchor_present"]
        else "❌ Anchor (respond to them and share your own situation)",
        "✅ Bounce (asked something back)" if data["bounce_present"] else "❌ Bounce (no question back)",
    ]
    tips = []
    if data.get("vocabulary_original"):
        tips.append(f'"{data["vocabulary_original"]}" → "{data["vocabulary_replacement"]}"')
    if not data["bounce_present"]:
        tips.append("End with an open question to keep the chat going")
    model_reply = (data.get("model_answer") or "").strip()
    example = f"Your colleague might say:\n{data['banter_reply']}"
    if model_reply:
        example = f"Model reply:\n{model_reply}\n\n{example}"
    mark_task_completed(qdrant, collection, week_number, "thu", owner)
    return render_feedback_card(
        FeedbackCard(
            day_code="thu",
            result_lines=result_lines,
            tip_lines=tips,
            example=example,
            answer=transcribed_reply,
        )
    )

# ---------------------------------------------------------------------------
# Shared per-chunk-per-owner progress (spec 2.5/2.6/Section 4). Friday and
# Saturday write/read the SAME record per (week_number, chunk, owner): a
# deterministic tag + chunk-name lookup (create on first write, update via
# set_payload after), not a fresh point every time. needs_review follows the
# latest attempt of each evaluation (Monday reply, Friday speaking, Saturday
# cloze) and is True while any of them is wrong -- see record_chunk_usage.
# (Originally sticky for the whole week; changed once daily tasks became
# repeatable, so re-practising a day can clear that day's own miss.)
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
    evaluation: str,
) -> None:
    """Create or update this user's per-chunk progress record for the week.
    Only overwrites user_sentence/user_sentence_source when a new non-empty
    sentence is given (Saturday's cloze-answer check has no new sentence to
    record, only a correctness verdict).

    needs_review is decided per evaluation ("mon", "fri", "sat"): each keeps
    only its latest result in review_by_source, and the chunk needs review
    when any evaluation's latest attempt got it wrong. So re-practising
    Friday and getting it right clears Friday's miss, but a wrong Saturday
    answer still flags the chunk however Friday went. A record written
    before this rule has only needs_review; a True there is kept as a
    "legacy" entry so an existing flag isn't silently dropped."""
    existing = _read_chunk_progress_point(qdrant, collection, owner, week_number, phrase)
    if existing:
        payload = existing["payload"] or {}
        by_source = dict(payload.get("review_by_source") or ({"legacy": True} if payload.get("needs_review") else {}))
        by_source[evaluation] = not used_correctly
        fields: dict = {"review_by_source": by_source, "needs_review": any(by_source.values())}
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
        "needs_review": not used_correctly,
        "review_by_source": {evaluation: not used_correctly},
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
        },
        "model_answer": {"type": "string", "minLength": 1},
    },
    "required": ["chunk_results", "model_answer"],
    "additionalProperties": False,
}


def build_friday_message(chunks: list[dict], week_number: int) -> str:
    chunk_lines = []
    for chunk in chunks:
        line = f"• {bold(chunk['phrase'])} — {esc(chunk['definition'])}"
        # Only an English example that really contains the phrase helps here.
        example = chunk.get("context_sentence") or ""
        if generate_cloze(chunk["phrase"], example):
            line += f"\n  {italic(example)}"
        chunk_lines.append(line)
    return render_task_card(
        TaskCard(
            day_code="fri",
            week_number=week_number,
            goal="Use at least 2 chunks in free speech",
            duration="~3 min",
            reply_mode="Voice, 1 min",
            content_html="This week's chunks:\n" + "\n".join(chunk_lines),
            steps=[
                "Talk about anything for 1 minute",
                "Work in at least 2 chunks (any tense is fine)",
                DONE_STEP,
            ],
        )
    )


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
    message = build_friday_message(chunks, week_number)
    vector = embeddings.embed(message)
    for owner in owners:
        send_message(owner, message)
        mark_task_pushed(qdrant, collection, week_number, "fri", owner, vector)
    return chunks


FRIDAY_MODEL_ANSWER_INSTRUCTION = (
    "a natural 1-minute spoken ramble (120-160 words) on the same topic the "
    "speaker chose that works in ALL of this week's chunks naturally, reusing "
    "the speaker's own ideas where they gave any"
)
MONDAY_MODEL_ANSWER_INSTRUCTION = (
    "one natural real-life example sentence for each chunk, one per line, "
    "reusing the speaker's own situation where they gave one"
)


def evaluate_chunk_usage(
    llm: LlmClient,
    chunks: list[dict],
    transcribed_reply: str,
    *,
    model_answer_instruction: str = FRIDAY_MODEL_ANSWER_INSTRUCTION,
) -> dict:
    """LLM judges chunk usage via semantic understanding, not string
    matching -- spoken chunks morph, e.g. "spread oneself too thin" becomes
    "I was spreading myself too thin" (spec 2.5 point 3). Also extracts the
    user's actual sentence for each chunk they used. Monday and Friday share
    this judgment and differ only in what the model answer should be."""
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
        "string if the chunk was not used. Then write model_answer: "
        f"{model_answer_instruction} -- a reference the speaker can compare "
        "their reply against."
    )
    raw = llm.chat_json(prompt, FRIDAY_EVAL_SCHEMA, schema_name="friday_chunk_usage", max_tokens=1000)
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
    result_lines: list[str] = []
    unused: list[str] = []
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
            evaluation="fri",
        )
        if used:
            result_lines.append(f"✅ {phrase}")
        else:
            result_lines.append(f"❌ {phrase} (not used, or not used naturally)")
            unused.append(phrase)
    tips = [f"Next time, try working in: {', '.join(unused)}"] if unused else []
    mark_task_completed(qdrant, collection, week_number, "fri", owner)
    return render_feedback_card(
        FeedbackCard(
            day_code="fri",
            result_lines=result_lines,
            tip_lines=tips,
            example=chunk_usage.get("model_answer") or "",
            answer=transcribed_reply,
        )
    )


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


# Past forms of verbs that open common idioms but don't keep their stem
# (take -> took), so a stem + suffix match can't find them.
_IRREGULAR_FORMS = {
    "be": ["was", "were", "been", "being", "is", "are", "am"],
    "break": ["broke", "broken"],
    "bring": ["brought"],
    "build": ["built"],
    "buy": ["bought"],
    "catch": ["caught"],
    "come": ["came"],
    "do": ["did", "done", "does"],
    "draw": ["drew", "drawn"],
    "drive": ["drove", "driven"],
    "eat": ["ate", "eaten"],
    "fall": ["fell", "fallen"],
    "feel": ["felt"],
    "find": ["found"],
    "get": ["got", "gotten"],
    "give": ["gave", "given"],
    "go": ["went", "gone", "goes"],
    "have": ["had", "has"],
    "hold": ["held"],
    "keep": ["kept"],
    "know": ["knew", "known"],
    "lay": ["laid"],
    "lead": ["led"],
    "leave": ["left"],
    "lose": ["lost"],
    "make": ["made"],
    "meet": ["met"],
    "pay": ["paid"],
    "run": ["ran"],
    "say": ["said"],
    "see": ["saw", "seen"],
    "sell": ["sold"],
    "send": ["sent"],
    "shake": ["shook", "shaken"],
    "sit": ["sat"],
    "speak": ["spoke", "spoken"],
    "spend": ["spent"],
    "stand": ["stood"],
    "stick": ["stuck"],
    "take": ["took", "taken"],
    "teach": ["taught"],
    "tell": ["told"],
    "think": ["thought"],
    "throw": ["threw", "thrown"],
    "wear": ["wore", "worn"],
    "win": ["won"],
    "write": ["wrote", "written"],
}
# Dictionary-style placeholders an idiom is written with, and what they stand
# for in a real sentence.
_POSSESSIVES = r"(?:my|your|his|her|its|our|their|one's|someone's|[\w-]+'s)"
_SOMEONE = r"(?:[\w'-]+(?:\s+[\w'-]+){0,2})"


def _word_pattern(word: str) -> str:
    lower = word.lower()
    if lower == "oneself":
        return "(?:" + "|".join(_REFLEXIVE_VARIANTS) + ")"
    if lower in ("one's", "someone's"):
        return _POSSESSIVES
    if lower in ("someone", "somebody", "something", "sb", "sth"):
        return _SOMEONE
    if not word.isalpha():
        return re.escape(word)
    forms = [re.escape(word) + r"\w*"]
    forms += [re.escape(form) for form in _IRREGULAR_FORMS.get(lower, [])]
    if lower.endswith("e") and len(lower) > 2:
        forms.append(re.escape(word[:-1]) + "ing")  # take -> taking
    return "(?:" + "|".join(forms) + ")"


def _build_phrase_pattern(phrase: str) -> re.Pattern:
    """Match a chunk phrase inside a sentence, tolerating how idioms are
    actually used: verb endings (spread -> spreading, take -> taking) and
    common irregular forms (take -> took/taken), dictionary placeholders
    (someone -> a name or pronoun, one's -> her/their/..., oneself -> any
    reflexive), and an optional part in brackets ("take a weight off (one's
    shoulders)" also matches just "took a weight off")."""
    optional = ""
    match = re.search(r"\(([^)]*)\)", phrase)
    if match:
        optional = match.group(1)
        phrase = (phrase[: match.start()] + phrase[match.end() :]).strip()
    body = r"\s+".join(_word_pattern(word) for word in phrase.split())
    if optional.strip():
        body += r"(?:\s+" + r"\s+".join(_word_pattern(word) for word in optional.split()) + ")?"
    return re.compile(r"(?<![\w'])" + body + r"(?![\w'])", re.IGNORECASE)


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
        prompt_text=f'Use "{phrase}" correctly in a sentence of your own.',
        source="phrase_only",
    )


def build_saturday_message(questions: list[ClozeQuestion], week_number: int) -> str:
    question_lines = [
        f"{bold(f'{index}.')} {esc(question.prompt_text)}" for index, question in enumerate(questions, start=1)
    ]
    return render_task_card(
        TaskCard(
            day_code="sat",
            week_number=week_number,
            goal="Review this week's chunks",
            duration="~5 min",
            reply_mode="Text or voice",
            content_html="\n".join(question_lines),
            steps=[
                f"Answer all {len(questions)} in one message, by number",
                "Text or voice both work",
                DONE_STEP,
            ],
        )
    )


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
        message = build_saturday_message(questions, week_number)
        vector = embeddings.embed(message)
        send_message(owner, message)
        mark_task_pushed(qdrant, collection, week_number, "sat", owner, vector)
        set_pending_answer(
            owner,
            {
                "kind": "eng_saturday_quiz",
                "week_number": week_number,
                "questions": [{"phrase": q.phrase, "prompt_text": q.prompt_text} for q in questions],
            },
        )
        result[owner] = questions
    return result


SATURDAY_CLOZE_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "correct": {"type": "boolean"},
                    "explanation_zh": {"type": "string", "minLength": 1},
                    "example_sentence": {"type": "string", "minLength": 1},
                    "model_answer": {"type": "string", "minLength": 1},
                },
                "required": ["correct", "explanation_zh", "example_sentence", "model_answer"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}


def judge_cloze_answers_with_llm(llm: LlmClient, questions: list[dict], transcribed_answer: str) -> list[dict]:
    """LLM judges each numbered question's answer by which part of the
    learner's reply corresponds to which blank -- fixes a real bug where
    the old deterministic judge_cloze_answer() checked whether a phrase
    appeared ANYWHERE in the whole multi-blank reply, so answers swapped
    to the wrong blank (or any correct phrase mentioned anywhere) were
    wrongly credited regardless of position. Also generates a short
    Chinese explanation and a correct-usage example sentence for every
    question (not just wrong ones) -- reinforces learning instead of a
    bare correct/needs-review verdict, and covers phrase_only questions
    that had no example sentence on file at all. Returns one result dict
    per question, in the same order as `questions`."""
    numbered = "\n".join(f"{i + 1}. {q['prompt_text']}" for i, q in enumerate(questions))
    phrase_list = ", ".join(f'{i + 1}. "{q["phrase"]}"' for i, q in enumerate(questions))
    prompt = (
        "This is a fill-in-the-blank (cloze) quiz with numbered questions, "
        "each blank expecting a specific target phrase.\n\n"
        f"Questions:\n{numbered}\n\n"
        f"Target phrase for each question, in order:\n{phrase_list}\n\n"
        f'The learner\'s reply (may or may not be numbered/ordered):\n"{transcribed_answer}"\n\n'
        "For EACH numbered question: (1) judge whether the part of the "
        "reply that answers THAT SPECIFIC question correctly uses its "
        "target phrase -- match reply segments to questions by number if "
        "the reply is numbered, otherwise by order; a phrase used for the "
        "wrong question is NOT correct for that question, even if it "
        "appears somewhere in the reply, position matters, not just "
        "whether the phrase is present anywhere; (2) in Traditional "
        "Chinese (繁體中文), briefly explain why it's right or wrong, to "
        "help the learner understand the mistake, not just see a verdict; "
        "(3) give one natural English example sentence correctly using "
        "the target phrase, so the learner has a model to learn from; "
        "(4) model_answer: the question's own sentence with the blank "
        "correctly filled in, the target phrase in the right tense/form "
        "for that sentence (if the question has no blank, write one "
        "sentence correctly using the phrase). "
        "IMPORTANT: your response is parsed as JSON -- if you need to "
        "quote a phrase inside explanation_zh, use Chinese corner "
        "brackets 「」, never a literal double-quote character (\"), "
        "which would break the JSON string."
    )
    raw = llm.chat_json(prompt, SATURDAY_CLOZE_JUDGE_SCHEMA, schema_name="saturday_cloze_judge", max_tokens=1300)
    return json.loads(raw)["results"]


def evaluate_saturday_answers(
    llm: LlmClient,
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    owner: str,
    week_number: int,
    questions: list[dict],
    transcribed_answer: str,
) -> str:
    results = judge_cloze_answers_with_llm(llm, questions, transcribed_answer)
    result_lines: list[str] = []
    tips: list[str] = []
    for index, (question, result) in enumerate(zip(questions, results), start=1):
        phrase = question["phrase"]
        correct = result["correct"]
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
            evaluation="sat",
        )
        result_lines.append(f"{'✅' if correct else '❌'} {index}. {phrase}")
        result_lines.append(f"    {result['explanation_zh']}")
        tips.append(f"{phrase}: {result['example_sentence']}")
    answer_key = "\n".join(
        f"{index}. {(result.get('model_answer') or '').strip()}"
        for index, result in enumerate(results, start=1)
        if (result.get("model_answer") or "").strip()
    )
    mark_task_completed(qdrant, collection, week_number, "sat", owner)
    return render_feedback_card(
        FeedbackCard(
            day_code="sat",
            result_lines=result_lines,
            tip_lines=tips,
            example=answer_key,
            answer=transcribed_answer,
        )
    )


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
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 400},
        "summary_zh": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    "required": ["summary", "summary_zh"],
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


def summarize_sunday_article(llm: LlmClient, article_text: str) -> tuple[str, str]:
    """LLM reads the real fetched article text and writes a ~50-word
    cultural-background summary -- explicitly grounded in the actual
    content, not guessed from the title/URL (spec 2.7 point 4). Returns
    (English summary, its Traditional Chinese translation)."""
    prompt = (
        "This is the full text of a British lifestyle/culture newspaper "
        "column. Write a roughly 50-word summary in English that gives a "
        "Traditional-Chinese-speaking reader enough cultural background to "
        "understand and enjoy the piece -- don't retell the whole plot, "
        "just orient them. Also give summary_zh: a faithful Traditional "
        "Chinese (繁體中文) translation of that English summary, so the "
        "reader can check their understanding of it.\n\n"
        f"Article text:\n{article_text}"
    )
    raw = llm.chat_json(prompt, SUNDAY_SUMMARY_SCHEMA, schema_name="sunday_summary", max_tokens=500)
    data = json.loads(raw)
    return data["summary"], (data.get("summary_zh") or "").strip()


def build_sunday_message(
    article_title: str, article_url: str, summary: str, week_number: int, summary_zh: str = ""
) -> str:
    content_html = f"{bold(article_title)}\n{esc(article_url)}\n\n{esc(summary)}"
    if summary_zh:
        content_html += f"\n\nChinese translation (tap to expand)\n{expandable(esc(summary_zh))}"
    return render_task_card(
        TaskCard(
            day_code="sun",
            week_number=week_number,
            goal="Read for the gist — no dictionary, no notes",
            duration="~10 min",
            reply_mode="No reply needed",
            content_html=content_html,
            steps=[
                "Read the summary, then open the article",
                "Aim to understand 70–80% of it",
                "No reply needed — reading it counts as done",
            ],
        )
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

    summary, summary_zh = summarize_sunday_article(llm, article_text)
    message = build_sunday_message(article.title, article.link, summary, week_number, summary_zh)
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
