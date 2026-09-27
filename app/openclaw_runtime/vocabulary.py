"""Word lookup (/w) and the personal word list (/vocab) for the English-
learning bot.

Meanings come from the offline dictionary (openclaw_runtime.dictionary); the
LLM only adds what a dictionary can't: which sense a word has in the
sentence the learner saw it in, and one example sentence. A word the
dictionary doesn't have (a phrase, slang) falls back to an LLM explanation,
clearly labelled as such.

Every successful lookup is saved to the learner's own word list in Qdrant --
owner-scoped like the rest of the English bot's per-user records. Looking
the same word up again bumps its lookup count instead of adding a duplicate:
a word looked up repeatedly is one that hasn't stuck yet.

Saved words come back for spaced-repetition review (/vocab review), Leitner
style: every correct answer moves a word up a box and pushes its next review
further out (see REVIEW_INTERVAL_DAYS), a wrong answer sends it back to box 0
for tomorrow.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from openclaw_runtime.dictionary import LocalDictionary, normalize_query
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.message_cards import (
    RULE,
    FeedbackCard,
    TaskCard,
    bold,
    esc,
    italic,
    render_feedback_card,
    render_task_card,
)
from openclaw_runtime.owned_records import read_owned_points, write_owned_point
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.skills.english_bot import generate_cloze, judge_cloze_answers_with_llm

VOCAB_TAG = "eng_vocab"
VOCAB_KIND = "vocab"
MAX_MEANING_LINES = 4
LIST_LIMIT = 30

LOOKUP_USAGE = (
    "Usage: /w <word> -- or just send the word on its own.\n"
    "Add the sentence you saw it in after a | to get the meaning in context:\n"
    "/w resilient | She's remarkably resilient."
)

SENSE_SCHEMA = {
    "type": "object",
    "properties": {
        "sense_zh": {"type": "string"},
        "example": {"type": "string", "minLength": 1},
    },
    "required": ["sense_zh", "example"],
    "additionalProperties": False,
}

AI_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_english": {"type": "boolean"},
        "phonetic": {"type": "string"},
        "meaning_zh": {"type": "string"},
        "sense_zh": {"type": "string"},
        "example": {"type": "string"},
    },
    "required": ["is_english", "phonetic", "meaning_zh", "sense_zh", "example"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class LookupResult:
    word: str
    phonetic: str
    meaning_lines: list[str]
    tags: list[str]
    base_form: str
    sense_zh: str
    example: str
    source: str  # "dictionary" | "ai"


def parse_lookup_command(text: str) -> tuple[str, str]:
    """"/w resilient | She's resilient." -> ("resilient", "She's resilient.")."""
    body = text.strip()[2:].strip()
    word, _, sentence = body.partition("|")
    return normalize_query(word), sentence.strip()


# A bare message counts as a lookup (no /w needed) when it's 1-4 English
# words and nothing else -- letters, apostrophes, hyphens, spaces. Anything
# with digits or sentence punctuation is left to the other handlers (a daily
# task answer, chat), and so are the everyday replies below, which would
# otherwise fill the word list with "thanks" and "ok".
_BARE_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*(?: [A-Za-z][A-Za-z'’-]*){0,3}")
CHAT_REPLIES = {
    "hi", "hello", "hey", "thanks", "thank you", "thx", "ok", "okay", "yes", "no",
    "yeah", "yep", "nope", "bye", "good morning", "good night", "cool", "great",
    "nice", "sure", "lol", "done", "help",
}


def parse_bare_lookup(text: str) -> tuple[str, str] | None:
    """"resilient" or "resilient | She's resilient." sent without /w ->
    (word, sentence); None when the message doesn't look like a lookup."""
    word_part, _, sentence = (text or "").partition("|")
    word = " ".join(word_part.split())
    if not _BARE_WORD.fullmatch(word) or word.lower() in CHAT_REPLIES:
        return None
    return word, sentence.strip()


def _sentence_instruction(sentence: str) -> str:
    if sentence:
        return (
            f'The learner saw it in this sentence: "{sentence}". In sense_zh, say '
            "briefly in Traditional Chinese (繁體中文) which meaning it has in "
            "that sentence."
        )
    return "No sentence was given, so leave sense_zh as an empty string."


def _dictionary_sense(llm: LlmClient, word: str, meaning_lines: list[str], sentence: str) -> tuple[str, str]:
    meanings = "\n".join(meaning_lines) or "(no Chinese meaning on file)"
    prompt = (
        f'An English learner looked up "{word}". The dictionary lists these '
        f"meanings:\n{meanings}\n\n"
        f"{_sentence_instruction(sentence)} Then give one short, natural English "
        "example sentence using the word, as a native speaker would say it "
        "today (example)."
    )
    data = json.loads(llm.chat_json(prompt, SENSE_SCHEMA, schema_name="word_sense", max_tokens=300))
    return data["sense_zh"].strip(), data["example"].strip()


def _ai_entry(llm: LlmClient, word: str, sentence: str) -> dict:
    prompt = (
        f'An English learner looked up "{word}", which isn\'t in the offline '
        "dictionary (it may be a phrase, an idiom or slang). If it is not "
        "English at all or not a real word or phrase, set is_english to false "
        "and leave the other fields empty. Otherwise give its IPA phonetic "
        "(empty for a multi-word phrase), a short meaning in Traditional "
        "Chinese (繁體中文) with its part of speech (meaning_zh), and one short "
        f"natural English example sentence (example). {_sentence_instruction(sentence)}"
    )
    return json.loads(llm.chat_json(prompt, AI_ENTRY_SCHEMA, schema_name="word_ai_entry", max_tokens=400))


def lookup_word(dictionary: LocalDictionary, llm: LlmClient, word: str, sentence: str = "") -> LookupResult | None:
    entry = dictionary.lookup(word)
    if entry is not None:
        meaning_lines = (entry.translation_lines or entry.definition_lines)[:MAX_MEANING_LINES]
        try:
            sense_zh, example = _dictionary_sense(llm, entry.word, meaning_lines, sentence)
        except Exception:  # noqa: BLE001 - the dictionary meaning alone is still a useful answer
            sense_zh, example = "", ""
        return LookupResult(
            word=entry.word,
            phonetic=entry.phonetic,
            meaning_lines=meaning_lines,
            tags=entry.tags,
            base_form=entry.base_form,
            sense_zh=sense_zh,
            example=example,
            source="dictionary",
        )

    data = _ai_entry(llm, word, sentence)
    if not data.get("is_english") or not data.get("meaning_zh", "").strip():
        return None
    return LookupResult(
        word=word,
        phonetic=data.get("phonetic", "").strip().strip("/"),
        meaning_lines=[data["meaning_zh"].strip()],
        tags=[],
        base_form="",
        sense_zh=data.get("sense_zh", "").strip(),
        example=data.get("example", "").strip(),
        source="ai",
    )


def _word_key(word: str) -> str:
    return word.strip().lower()


def _find_saved(qdrant: QdrantClient, collection: str, owner: str, word: str) -> dict | None:
    points = read_owned_points(
        qdrant, collection, owner, {"tag": VOCAB_TAG, "kind": VOCAB_KIND, "word": _word_key(word)}, limit=1
    )
    return points[0] if points else None


def save_to_word_list(
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    owner: str,
    result: LookupResult,
    sentence: str = "",
    *,
    now: datetime | None = None,
) -> int:
    """Add the word to this learner's list, or bump its lookup count if it's
    already there. Returns the lookup count after this lookup. Review fields
    (review_box / next_review) are written now so spaced-repetition review
    can be added later without migrating old records."""
    now = now or datetime.now(timezone.utc)
    existing = _find_saved(qdrant, collection, owner, result.word)
    if existing:
        payload = existing.get("payload") or {}
        count = int(payload.get("lookup_count") or 0) + 1
        fields = {"lookup_count": count, "last_lookup_at": now.isoformat()}
        if sentence:
            fields["context_sentence"] = sentence
        qdrant.set_payload(collection, existing["id"], fields)
        return count

    meaning = result.meaning_lines[0] if result.meaning_lines else ""
    text = f"{result.word}: {meaning}"
    write_owned_point(
        qdrant,
        collection,
        owner,
        text,
        embeddings.embed(text),
        {
            "tag": VOCAB_TAG,
            "kind": VOCAB_KIND,
            "word": _word_key(result.word),
            "display_word": result.word,
            "phonetic": result.phonetic,
            "meaning": meaning,
            "context_sentence": sentence,
            "source": result.source,
            "lookup_count": 1,
            "added_at": now.isoformat(),
            "last_lookup_at": now.isoformat(),
            "review_box": 0,
            "next_review": (now.date() + timedelta(days=1)).isoformat(),
        },
    )
    return 1


def add_for_review(
    qdrant: QdrantClient,
    embeddings: EmbeddingClient,
    collection: str,
    owner: str,
    word: str,
    meaning: str,
    sentence: str = "",
    *,
    source: str,
    now: datetime | None = None,
) -> None:
    """Put a word in this learner's review queue for tomorrow, without it
    counting as a lookup -- used for the English bot's weekly chunks that
    haven't stuck. Already on the list: sent back to box 0, due tomorrow
    (it was evidently not learned). Not on the list: added as a new word."""
    now = now or datetime.now(timezone.utc)
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    existing = _find_saved(qdrant, collection, owner, word)
    if existing:
        fields = {"review_box": 0, "next_review": tomorrow}
        if sentence:
            fields["context_sentence"] = sentence
        qdrant.set_payload(collection, existing["id"], fields)
        return
    text = f"{word}: {meaning}"
    write_owned_point(
        qdrant,
        collection,
        owner,
        text,
        embeddings.embed(text),
        {
            "tag": VOCAB_TAG,
            "kind": VOCAB_KIND,
            "word": _word_key(word),
            "display_word": word,
            "phonetic": "",
            "meaning": meaning,
            "context_sentence": sentence,
            "source": source,
            "lookup_count": 0,
            "added_at": now.isoformat(),
            "last_lookup_at": "",
            "review_box": 0,
            "next_review": tomorrow,
        },
    )


def list_word_list(qdrant: QdrantClient, collection: str, owner: str) -> list[dict]:
    """This learner's saved words, newest first."""
    points = read_owned_points(qdrant, collection, owner, {"tag": VOCAB_TAG, "kind": VOCAB_KIND}, limit=1024)
    payloads = [point.get("payload") or {} for point in points]
    return sorted(payloads, key=lambda payload: payload.get("added_at") or "", reverse=True)


def remove_from_word_list(qdrant: QdrantClient, collection: str, owner: str, word: str) -> bool:
    existing = _find_saved(qdrant, collection, owner, normalize_query(word))
    if not existing:
        return False
    qdrant.delete_points(collection, [existing["id"]])
    return True


def render_lookup_card(result: LookupResult, sentence: str, lookup_count: int) -> str:
    header = bold(result.word)
    if result.phonetic:
        header += f"  /{esc(result.phonetic)}/"
    lines = [f"<b>查字｜{esc(result.word)}</b>", RULE, header]
    lines += [esc(line) for line in result.meaning_lines]
    if result.base_form:
        lines.append(f"Base form: {esc(result.base_form)}")
    if result.source == "ai":
        lines.append(italic("AI explanation — not in the local dictionary"))
    if sentence:
        lines += ["", bold("In your sentence"), italic(sentence)]
        if result.sense_zh:
            lines.append(f"→ {esc(result.sense_zh)}")
    if result.example:
        lines += ["", bold("Example"), italic(result.example)]
    if result.tags:
        lines += ["", f"{bold('Tags')}　{esc(' · '.join(result.tags))}"]
    lines.append("")
    if lookup_count <= 1:
        lines.append(
            f"Added to your word list (looked up 1 time). Send /vocab rm {esc(result.word)} to undo."
        )
    else:
        lines.append(f"Already in your word list — looked up {lookup_count} times.")
    return "\n".join(lines)


def render_word_list(entries: list[dict], due_count: int = 0) -> str:
    if not entries:
        return "Your word list is empty. Look a word up with /w <word> and it's added automatically."
    lines = [f"<b>生字本</b> · {len(entries)} words", RULE]
    if due_count:
        lines += [f"{due_count} due for review today — send /vocab review", ""]
    for index, entry in enumerate(entries[:LIST_LIMIT], start=1):
        meaning = entry.get("meaning") or ""
        if len(meaning) > 30:
            meaning = meaning[:30] + "…"
        count = int(entry.get("lookup_count") or 1)
        suffix = f" (looked up {count}×)" if count > 1 else ""
        lines.append(f"{index}. {bold(entry.get('display_word') or entry.get('word', ''))} — {esc(meaning)}{suffix}")
    if len(entries) > LIST_LIMIT:
        lines.append(f"…and {len(entries) - LIST_LIMIT} more. Newest first.")
    lines += ["", "/w &lt;word&gt; to look up · /vocab review to review · /vocab rm &lt;word&gt; to remove"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spaced-repetition review
# ---------------------------------------------------------------------------

# Days until the next review once a word reaches each box (box 0 = new or
# just missed, reviewed the next day). Box 5 is as far as it goes.
REVIEW_INTERVAL_DAYS = {1: 3, 2: 7, 3: 14, 4: 30, 5: 60}
MAX_REVIEW_BOX = max(REVIEW_INTERVAL_DAYS)
REVIEW_SESSION_SIZE = 5
REVIEW_FEEDBACK_FOOTER = "Next review dates updated. /vocab shows your list."


def next_review_after(box: int, correct: bool, today: date) -> tuple[int, date]:
    """(new box, next review date) after one review answer."""
    if not correct:
        return 0, today + timedelta(days=1)
    new_box = min(box + 1, MAX_REVIEW_BOX)
    return new_box, today + timedelta(days=REVIEW_INTERVAL_DAYS[new_box])


def _due_points(qdrant: QdrantClient, collection: str, owner: str, today: date) -> list[dict]:
    points = read_owned_points(qdrant, collection, owner, {"tag": VOCAB_TAG, "kind": VOCAB_KIND}, limit=1024)
    today_text = today.isoformat()

    def next_review(point: dict) -> str:
        return (point.get("payload") or {}).get("next_review") or today_text

    def lookups(point: dict) -> int:
        return int((point.get("payload") or {}).get("lookup_count") or 1)

    # Most overdue first; among equals, the words looked up most often.
    due = [point for point in points if next_review(point) <= today_text]
    return sorted(due, key=lambda point: (next_review(point), -lookups(point)))


def count_due_words(qdrant: QdrantClient, collection: str, owner: str, today: date) -> int:
    return len(_due_points(qdrant, collection, owner, today))


def _review_question(payload: dict) -> str:
    """Prefer blanking the word out of the learner's own sentence (with the
    meaning as a hint); fall back to recalling the word from its meaning."""
    word = payload.get("display_word") or payload.get("word", "")
    meaning = payload.get("meaning") or ""
    cloze = generate_cloze(word, payload.get("context_sentence") or "")
    if cloze:
        return f"{cloze} (hint: {meaning})" if meaning else cloze
    return f"Which word or phrase means: {meaning}"


def start_review(qdrant: QdrantClient, collection: str, owner: str, today: date) -> list[dict]:
    """Pick up to REVIEW_SESSION_SIZE due words and build one question each.
    Each question carries what's needed to grade it and update the record
    later: the word (as the judge's target phrase), its point id and box."""
    questions = []
    for point in _due_points(qdrant, collection, owner, today)[:REVIEW_SESSION_SIZE]:
        payload = point.get("payload") or {}
        questions.append(
            {
                "phrase": payload.get("display_word") or payload.get("word", ""),
                "prompt_text": _review_question(payload),
                "point_id": point["id"],
                "box": int(payload.get("review_box") or 0),
                "correct_count": int(payload.get("correct_count") or 0),
                "wrong_count": int(payload.get("wrong_count") or 0),
            }
        )
    return questions


def render_review_quiz(questions: list[dict]) -> str:
    lines = [f"{bold(f'{index}.')} {esc(q['prompt_text'])}" for index, q in enumerate(questions, start=1)]
    return render_task_card(
        TaskCard(
            day_code="",
            week_number=0,
            title="生字複習",
            goal="Recall words you looked up",
            duration="~3 min",
            reply_mode="Typed reply",
            content_html="\n".join(lines),
            steps=[
                f"Answer all {len(questions)} in one typed message, by number",
                "Until you answer, typed replies go to this review; voice replies still go to today's task",
                "One answer finishes the review — no /Done needed",
            ],
        )
    )


def render_review_reminder(due_count: int) -> str:
    """The Word review block added to the end of a daily task card -- empty
    when nothing is due, so the card is unchanged on those days."""
    if not due_count:
        return ""
    noun = "word is" if due_count == 1 else "words are"
    return f"\n\n{bold('Word review')}\n{due_count} saved {noun} due today — send /vocab review"


def grade_review(
    llm: LlmClient,
    qdrant: QdrantClient,
    collection: str,
    questions: list[dict],
    answer: str,
    today: date,
    *,
    now: datetime | None = None,
) -> str:
    """Judge the answers (same position-aware LLM judge as Saturday's cloze
    quiz), move each word to its next box/date, and return the feedback card."""
    now = now or datetime.now(timezone.utc)
    results = judge_cloze_answers_with_llm(llm, questions, answer)
    result_lines: list[str] = []
    tips: list[str] = []
    answer_key: list[str] = []
    for index, (question, result) in enumerate(zip(questions, results), start=1):
        correct = bool(result["correct"])
        box, next_date = next_review_after(question["box"], correct, today)
        fields = {
            "review_box": box,
            "next_review": next_date.isoformat(),
            "last_review_at": now.isoformat(),
            "correct_count": question.get("correct_count", 0) + (1 if correct else 0),
            "wrong_count": question.get("wrong_count", 0) + (0 if correct else 1),
        }
        qdrant.set_payload(collection, question["point_id"], fields)
        days = (next_date - today).days
        when = "tomorrow" if days == 1 else f"in {days} days"
        result_lines.append(f"{'✅' if correct else '❌'} {index}. {question['phrase']} — next review {when}")
        result_lines.append(f"    {result['explanation_zh']}")
        tips.append(f"{question['phrase']}: {result['example_sentence']}")
        model_answer = (result.get("model_answer") or "").strip()
        if model_answer:
            answer_key.append(f"{index}. {model_answer}")
    return render_feedback_card(
        FeedbackCard(
            day_code="",
            title="生字複習｜回饋",
            result_lines=result_lines,
            tip_lines=tips,
            example="\n".join(answer_key),
            answer=answer,
            footer=REVIEW_FEEDBACK_FOOTER,
        )
    )
