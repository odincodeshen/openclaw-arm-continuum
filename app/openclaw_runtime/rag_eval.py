"""Shared pieces for the /rag evaluation scripts (scripts/rag_*_eval.py).

Questions are written by the bot's own model from passages sampled out of
the bot's own collections, so a bot can be checked on its real documents
without anyone writing a test set. Nothing here writes to Qdrant.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

from openclaw_runtime.categories import registry_entries

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "usable": {"type": "boolean"},
        "question": {"type": "string"},
        "fact": {"type": "string"},
    },
    "required": ["usable", "question", "fact"],
}
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"contains_fact": {"type": "boolean"}},
    "required": ["contains_fact"],
}


@dataclass
class Sample:
    collection: str
    point_id: str
    text: str
    question: str = ""
    fact: str = ""


def bot_collections(settings) -> list[str]:
    """The collections plain /rag reads documents from: the knowledge base
    and, with categories on, every category."""
    collections = [settings.knowledge_collection]
    if settings.category_rag_enabled:
        collections += [entry["collection"] for entry in registry_entries(settings)]
    return collections


def sample_passages(settings, qdrant, count: int, seed: int, min_chars: int = 400) -> list[Sample]:
    """Up to count passages (shuffled with seed) of at least min_chars."""
    pool = []
    for collection in bot_collections(settings):
        try:
            points = qdrant.scroll_by_filters(collection, {}, limit=512)
        except Exception:  # noqa: BLE001 - a missing collection just has nothing to sample
            continue
        for point in points:
            text = str((point.get("payload") or {}).get("text") or "").strip()
            if len(text) >= min_chars:
                pool.append(Sample(collection, str(point.get("id")), text))
    random.Random(seed).shuffle(pool)
    return pool[:count]


def write_question(llm, passage: str, language: str = "") -> dict | None:
    """{"question", "fact"} that the passage answers, or None when it has no
    clear, specific fact. language: write the question in it (e.g. to check
    a Chinese question against English notes); default the passage's own."""
    in_language = f"in {language}" if language else "in the passage's own language"
    prompt = (
        "Here is a passage from someone's notes. Write one question a person might ask that this passage "
        "answers with a specific fact (a number, name, date, place or short phrase), and give that fact. "
        f"Ask it the way they would, without quoting the passage, {in_language}. If the "
        "passage has no clear, specific fact, set usable to false.\n\n"
        f"Passage:\n{passage[:3000]}"
    )
    try:
        data = json.loads(llm.chat_json(prompt, QUESTION_SCHEMA, schema_name="rag_eval_question",
                                        max_tokens=200, persona=False))
    except Exception:  # noqa: BLE001
        return None
    if not data.get("usable") or not data.get("question") or not data.get("fact"):
        return None
    return data


def make_questions(settings, qdrant, llm, count: int, seed: int, language: str = "") -> list[Sample]:
    """count samples with a question each (sampling spares for passages the
    model can't use)."""
    out = []
    for sample in sample_passages(settings, qdrant, count * 2, seed):
        if len(out) >= count:
            break
        made = write_question(llm, sample.text, language)
        if made:
            sample.question, sample.fact = made["question"], made["fact"]
            out.append(sample)
    return out


def judge(llm, question: str, fact: str, answer: str) -> bool:
    prompt = (
        "Does the answer below state this fact (in any language or wording)? Reply with contains_fact.\n\n"
        f"Question: {question}\nFact: {fact}\n\nAnswer:\n{answer[:2000]}"
    )
    try:
        data = json.loads(llm.chat_json(prompt, JUDGE_SCHEMA, schema_name="rag_eval_judge",
                                        max_tokens=40, persona=False))
    except Exception:  # noqa: BLE001
        return False
    return bool(data.get("contains_fact"))
