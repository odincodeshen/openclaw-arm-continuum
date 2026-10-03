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
import sys
from pathlib import Path

for candidate in (Path("/app"), Path(__file__).resolve().parent.parent / "app" if "__file__" in globals() else None):
    if candidate and candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from openclaw_runtime import llm_client  # noqa: E402
from openclaw_runtime.config import load_settings  # noqa: E402
from openclaw_runtime.embedding_client import EmbeddingClient  # noqa: E402
from openclaw_runtime.llm_client import LlmClient  # noqa: E402
from openclaw_runtime.qdrant_client import QdrantClient  # noqa: E402
from openclaw_runtime.rag_eval import judge, make_questions  # noqa: E402
from openclaw_runtime.skills.memory import RagRetrieveSkill  # noqa: E402


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
    parser.add_argument("--question-language", default="",
                        help='write the questions in this language, e.g. "Traditional Chinese"')
    parser.add_argument("--show", action="store_true", help="also print each question and fact")
    args = parser.parse_args(argv)

    base = load_settings()
    configs = [("no budget", 0, 0)]
    for item in filter(None, args.budgets.split(",")):
        context, _, passage = item.strip().partition("/")
        configs.append((item.strip(), int(context), int(passage or 0)))

    embeddings, qdrant, llm = EmbeddingClient(base), QdrantClient(base), LlmClient(base)
    sizes = PromptSizes()
    questions = make_questions(base, qdrant, llm, args.questions, args.seed, args.question_language)
    print(f"{len(questions)} questions from the bot's own documents", file=sys.stderr)

    rows = []
    for name, context, passage in configs:
        settings = dataclasses.replace(base, rag_context_tokens=context, rag_passage_tokens=passage)
        skill = RagRetrieveSkill(settings, {}, embeddings, qdrant, llm)
        kept, prompt_sizes = 0, []
        for q in questions:
            sizes.take()
            answer = skill.run(f"/rag {q.question}").answer
            prompt_sizes.append(sizes.take())
            ok = judge(llm, q.question, q.fact, answer)
            sizes.take()
            kept += ok
            if args.show:
                print(f"[{name}] {'OK ' if ok else 'MISS'} {q.question} -> {q.fact}", file=sys.stderr)
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
