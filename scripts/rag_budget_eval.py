#!/usr/bin/env python3
"""Does a /rag budget lose answers? A check on a bot's own documents.

Runs inside a bot's Telegram container and only reads: it samples passages
from the bot's knowledge base and categories, has the model write one
question per passage (with the short fact that answers it), then asks each
question through /rag with no budget and with each budget given, and has the
model judge whether the answer contains the fact.

    docker exec -i openclaw-telegram-<bot> python3 - --budgets 1200/300,1500/400 < scripts/rag_budget_eval.py

Budgets are "context/passage" in estimated tokens (OPENCLAW_RAG_CONTEXT_TOKENS
/ OPENCLAW_RAG_PASSAGE_TOKENS). The report shows, for each, how many answers
kept the fact and the average size of the /rag prompt. Questions and answers
stay in the container; only counts are printed, unless --show.

The model judges its own answers, so treat small differences as noise: a
budget is fine when it scores about the same as "no budget".
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import sys
from pathlib import Path

for candidate in (Path("/app"), Path(__file__).resolve().parent.parent / "app" if "__file__" in globals() else None):
    if candidate and candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from openclaw_runtime import llm_client  # noqa: E402
from openclaw_runtime.categories import registry_entries  # noqa: E402
from openclaw_runtime.config import load_settings  # noqa: E402
from openclaw_runtime.embedding_client import EmbeddingClient  # noqa: E402
from openclaw_runtime.llm_client import LlmClient  # noqa: E402
from openclaw_runtime.qdrant_client import QdrantClient  # noqa: E402
from openclaw_runtime.skills.memory import RagRetrieveSkill  # noqa: E402

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


def sample_passages(settings, qdrant, count: int, seed: int) -> list[dict]:
    collections = [settings.knowledge_collection]
    if settings.category_rag_enabled:
        collections += [entry["collection"] for entry in registry_entries(settings)]
    pool = []
    for collection in collections:
        try:
            points = qdrant.scroll_by_filters(collection, {}, limit=512)
        except Exception:  # noqa: BLE001 - a missing collection just has nothing to sample
            continue
        pool += [str((p.get("payload") or {}).get("text") or "").strip() for p in points]
    pool = [text for text in pool if len(text) >= 400]
    random.Random(seed).shuffle(pool)
    return [{"text": text} for text in pool[: count * 2]]  # spares for passages the model can't use


def write_question(llm, passage: str) -> dict | None:
    prompt = (
        "Here is a passage from someone's notes. Write one question a person might ask that this passage "
        "answers with a specific fact (a number, name, date, place or short phrase), and give that fact. "
        "Ask it the way they would, without quoting the passage, in the passage's own language. If the "
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


class PromptSizes:
    """Records the estimated size of each /rag prompt."""

    def __init__(self) -> None:
        self.sizes: list[int] = []
        original = llm_client.LlmClient._chat_completion

        def wrapped(client, payload):
            self.sizes.append(sum(llm_client.estimate_tokens(str(m.get("content") or ""))
                                  for m in payload.get("messages") or []))
            return original(client, payload)

        llm_client.LlmClient._chat_completion = wrapped

    def take(self) -> int:
        size = self.sizes[-1] if self.sizes else 0
        self.sizes = []
        return size


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=int, default=20)
    parser.add_argument("--budgets", default="1200/300,1500/400",
                        help='comma-separated "context/passage" token budgets')
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--show", action="store_true", help="also print each question and fact")
    args = parser.parse_args(argv)

    base = load_settings()
    configs = [("no budget", 0, 0)]
    for item in filter(None, args.budgets.split(",")):
        context, _, passage = item.strip().partition("/")
        configs.append((item.strip(), int(context), int(passage or 0)))

    embeddings, qdrant, llm = EmbeddingClient(base), QdrantClient(base), LlmClient(base)
    sizes = PromptSizes()
    questions = []
    for passage in sample_passages(base, qdrant, args.questions, args.seed):
        if len(questions) >= args.questions:
            break
        made = write_question(llm, passage["text"])
        if made:
            questions.append(made)
    print(f"{len(questions)} questions from the bot's own documents", file=sys.stderr)

    rows = []
    for name, context, passage in configs:
        settings = dataclasses.replace(base, rag_context_tokens=context, rag_passage_tokens=passage)
        skill = RagRetrieveSkill(settings, {}, embeddings, qdrant, llm)
        kept, prompt_sizes = 0, []
        for q in questions:
            sizes.take()
            answer = skill.run(f"/rag {q['question']}").answer
            prompt_sizes.append(sizes.take())
            ok = judge(llm, q["question"], q["fact"], answer)
            sizes.take()
            kept += ok
            if args.show:
                print(f"[{name}] {'OK ' if ok else 'MISS'} {q['question']} -> {q['fact']}", file=sys.stderr)
        average = sum(prompt_sizes) / len(prompt_sizes) if prompt_sizes else 0
        rows.append((name, kept, len(questions), average))
        print(f"{name}: {kept}/{len(questions)}", file=sys.stderr)

    print(f"# /rag budget check -- {base.runtime_label}\n")
    print("| Budget (context/passage) | Answers with the fact | Average /rag prompt (est. tokens) |")
    print("| --- | --- | --- |")
    for name, kept, total, average in rows:
        print(f"| {name} | {kept}/{total} | {average:.0f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
