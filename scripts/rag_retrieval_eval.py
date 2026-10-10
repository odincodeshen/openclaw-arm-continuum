#!/usr/bin/env python3
"""Does /rag find the right passage? A retrieval check on a bot's own documents.

Runs inside a bot's Telegram container and only reads. It samples passages
from the bot's knowledge base and categories, and has the model write one
question per passage. For each question it then checks:

- whether plain /rag's retrieval (what the answer is written from) contains
  the source passage, and how many hits came with it;
- the source passage's rank in its own collection when up to 50 are
  fetched, to see whether fetching more and re-ranking could reach it;
- how many hits a relevance cut-off would keep: hits scoring within a
  margin of the best hit.

    docker exec -i openclaw-telegram-<bot> python3 - --questions 30 < scripts/rag_retrieval_eval.py
    docker exec -i openclaw-telegram-<bot> python3 - --query-prefix "search_query: " < scripts/rag_retrieval_eval.py
    docker exec -i openclaw-telegram-<bot> python3 - --query-prefix "search_query: " \
        --reembed-prefix "search_document: " < scripts/rag_retrieval_eval.py

--reembed-prefix tries a document-side prefix without touching the bot's
data: it copies the bot's collections into throwaway ones, embedding each
passage again with the prefix, runs the check against the copies, and
deletes them at the end.

--why-missed adds, for the questions whose passage isn't sent, the step that
dropped it and counts of what those passages and questions have in common.

No model call writes an answer, so it is quick once the questions exist.
Only counts are printed, unless --show.
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import re
import statistics
import sys
import uuid
from collections import Counter
from pathlib import Path

for candidate in (Path("/app"), Path(__file__).resolve().parent.parent / "app" if "__file__" in globals() else None):
    if candidate and candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from openclaw_runtime.config import load_settings  # noqa: E402
from openclaw_runtime.embedding_client import EmbeddingClient  # noqa: E402
from openclaw_runtime.http_client import request_json  # noqa: E402
from openclaw_runtime.llm_client import LlmClient, estimate_tokens  # noqa: E402
from openclaw_runtime.qdrant_client import QdrantClient  # noqa: E402
from openclaw_runtime.rag_budget import drop_weak_hits, fit_passages, keywords_first, terms  # noqa: E402
from openclaw_runtime.rag_eval import bot_collections, make_questions  # noqa: E402
from openclaw_runtime.skills.memory import RagRetrieveSkill  # noqa: E402

_CJK_CHAR = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
MARGINS = (0.05, 0.10, 0.15, 0.20)
FAILED_EMBEDS: list[str] = []  # texts the embedding model refused (reported, then skipped)
DEPTHS = (5, 10, 20, 50)


class PrefixedEmbeddings:
    """Adds a prefix to every text it embeds (for the query side only)."""

    def __init__(self, inner, prefix: str) -> None:
        self.inner, self.prefix = inner, prefix

    def embed(self, text: str) -> list[float]:
        return self.inner.embed(self.prefix + text)


class RedirectedQdrant:
    """The bot's Qdrant client, reading from copies: every call that names a
    collection in ``mapping`` goes to its copy instead."""

    def __init__(self, inner, mapping: dict[str, str]) -> None:
        self.inner, self.mapping = inner, mapping

    def __getattr__(self, name):
        method = getattr(self.inner, name)

        def call(collection, *args, **kwargs):
            return method(self.mapping.get(collection, collection), *args, **kwargs)

        return call


def copy_with_prefix(settings, qdrant, embeddings, collections: list[str], prefix: str,
                     vector_size: int | None = None) -> dict[str, str]:
    """Throwaway copies of collections, every passage embedded again (with
    prefix, by embeddings). Returns {original: copy}."""
    base = settings.qdrant_base_url.rstrip("/")
    tag = f"evalcopy_{uuid.uuid4().hex[:8]}_"
    mapping = {}
    for index, collection in enumerate(collections):
        try:
            points = qdrant.scroll_by_filters(collection, {}, limit=512)
        except Exception:  # noqa: BLE001 - nothing to copy
            continue
        copy = f"{tag}{index}"
        request_json("PUT", f"{base}/collections/{copy}",
                     {"vectors": {"size": vector_size or settings.embedding_vector_size, "distance": "Cosine"}})
        mapping[collection] = copy
        batch = []
        for point in points:
            payload = point.get("payload") or {}
            text = str(payload.get("text") or "")
            if not text:
                continue
            try:
                vector = embeddings.embed(prefix + text)
            except Exception:  # noqa: BLE001 - e.g. a model that returns NaN for some input
                FAILED_EMBEDS.append(f"passage {point['id']}")
                continue
            batch.append({"id": point["id"], "vector": vector, "payload": payload})
            if len(batch) == 64:
                request_json("PUT", f"{base}/collections/{copy}/points?wait=true", {"points": batch})
                batch = []
        if batch:
            request_json("PUT", f"{base}/collections/{copy}/points?wait=true", {"points": batch})
    return mapping


class Bm25:
    """Keyword scores over every passage of the given collections (fine for
    a check; a few thousand passages)."""

    def __init__(self, qdrant, collections: list[str], k1: float = 1.2, b: float = 0.75) -> None:
        self.docs = []  # (collection, id, term counts, length)
        for collection in collections:
            try:
                points = qdrant.scroll_by_filters(collection, {}, limit=512)
            except Exception:  # noqa: BLE001
                continue
            for point in points:
                words = self.tokens(str((point.get("payload") or {}).get("text") or ""))
                if words:
                    counts = {}
                    for w in words:
                        counts[w] = counts.get(w, 0) + 1
                    self.docs.append((collection, str(point["id"]), counts, len(words)))
        self.avg = sum(d[3] for d in self.docs) / (len(self.docs) or 1)
        self.collection_of = {pid: coll for coll, pid, _, _ in self.docs}
        df = {}
        for _, _, counts, _ in self.docs:
            for w in counts:
                df[w] = df.get(w, 0) + 1
        n = len(self.docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.k1, self.b = k1, b

    @staticmethod
    def tokens(text: str) -> list[str]:
        out = []
        for word in text.lower().split():
            out += sorted(terms(word))
        return out

    def rank(self, query: str) -> list[tuple[str, float]]:
        q = set(self.tokens(query))
        scored = []
        for _, pid, counts, length in self.docs:
            s = 0.0
            for w in q:
                tf = counts.get(w)
                if tf:
                    s += self.idf[w] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / self.avg))
            if s > 0:
                scored.append((pid, s))
        return sorted(scored, key=lambda x: -x[1])


def rrf(*rankings: list[str], k: int = 60, weights: tuple[float, ...] | None = None) -> list[str]:
    scores = {}
    for i, ranking in enumerate(rankings):
        w = weights[i] if weights else 1.0
        for position, pid in enumerate(ranking, start=1):
            scores[pid] = scores.get(pid, 0.0) + w / (k + position)
    return [pid for pid, _ in sorted(scores.items(), key=lambda x: -x[1])]


PIPELINE_WEIGHTS = ((1, 1), (3, 1), (1, 0))  # (keywords, vector)
PIPELINE_KEEP = (4, 6, 8)
KEYWORD_FIRST = ((3, 1), (4, 2), (3, 3), (5, 2))  # (keyword hits, vector hits added after them)


def drop_copies(settings, mapping: dict[str, str]) -> None:
    base = settings.qdrant_base_url.rstrip("/")
    for copy in mapping.values():
        try:
            request_json("DELETE", f"{base}/collections/{copy}")
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--query-prefix", default="", help='e.g. "search_query: " for nomic-embed-text')
    parser.add_argument("--reembed-prefix", default=None,
                        help='check against throwaway copies embedded with this document prefix')
    parser.add_argument("--reembed-model", default="",
                        help="with --reembed-prefix (may be empty): embed the copies and questions with this "
                             "Ollama model instead, e.g. bge-m3")
    parser.add_argument("--question-language", default="",
                        help='write the questions in this language, e.g. "Traditional Chinese"')
    parser.add_argument("--hybrid", action="store_true",
                        help="also rank by keywords (BM25) and by both fused, over all the bot's documents")
    parser.add_argument("--why-missed", action="store_true",
                        help="for questions whose passage /rag doesn't send: the step that dropped it, and "
                             "counts of what those passages and questions have in common")
    parser.add_argument("--show", action="store_true", help="also print each question and where its passage ranked")
    args = parser.parse_args(argv)

    settings = load_settings()
    qdrant, llm = QdrantClient(settings), LlmClient(settings)
    base_embeddings = EmbeddingClient(settings)
    vector_size = None
    if args.reembed_model:
        if args.reembed_prefix is None:
            args.reembed_prefix = ""
        base_embeddings = EmbeddingClient(dataclasses.replace(settings, embedding_model=args.reembed_model))
        vector_size = len(base_embeddings.embed("size check"))
    embeddings = PrefixedEmbeddings(base_embeddings, args.query_prefix) if args.query_prefix else base_embeddings
    samples = make_questions(settings, qdrant, llm, args.questions, args.seed, args.question_language)
    print(f"{len(samples)} questions from the bot's own documents", file=sys.stderr)
    mapping = {}
    if args.reembed_prefix is not None:
        collections = bot_collections(settings) + [settings.tracker_collection]
        mapping = copy_with_prefix(settings, qdrant, base_embeddings, collections, args.reembed_prefix, vector_size)
        print(f"copied {len(mapping)} collections with document prefix {args.reembed_prefix!r}", file=sys.stderr)
        qdrant = RedirectedQdrant(qdrant, mapping)
    skill = RagRetrieveSkill(settings, {}, embeddings, qdrant, llm)
    try:
        report(args, settings, skill, qdrant, embeddings, samples)
        if args.why_missed:
            why_missed(settings, qdrant, embeddings, skill.llm, samples)
    finally:
        drop_copies(settings, mapping)
    return 0


def sent_to_model(settings, sections: list, question: str) -> list[dict]:
    """The passages /rag would put in the prompt (same steps as _answer_from)."""
    sections = drop_weak_hits(sections, settings.rag_relevance_margin)
    sections = keywords_first(sections, keyword_hits=settings.rag_keyword_hits, vector_hits=settings.rag_vector_hits)
    sections = fit_passages(sections, question, context_tokens=settings.rag_context_tokens,
                            passage_tokens=settings.rag_passage_tokens)
    return [h for _, hits in sections for h in hits]


def final_context(settings, qdrant, embeddings, llm, samples) -> dict[str, tuple[int, list[int], list[int]]]:
    """For keyword search off and on: (source passage sent, passages sent,
    est. tokens sent) with this bot's settings."""
    out = {}
    for label, on in (("vector only", False), ("keywords first", True)):
        s_on = dataclasses.replace(settings, rag_keyword_search=on)
        skill = RagRetrieveSkill(s_on, {}, embeddings, qdrant, llm)
        found, counts, tokens = 0, [], []
        for s in samples:
            sent = sent_to_model(s_on, skill.default_sections(s.question), s.question)
            found += any(str(h.get("id")) == s.point_id for h in sent)
            counts.append(len(sent))
            tokens.append(sum(estimate_tokens(str((h.get("payload") or {}).get("text") or "")) for h in sent))
        out[label] = (found, counts, tokens)
    return out


def report(args, settings, skill, qdrant, embeddings, samples) -> None:
    usable = []
    for s in samples:
        try:
            embeddings.embed(s.question)
            usable.append(s)
        except Exception:  # noqa: BLE001
            FAILED_EMBEDS.append("a question")
    samples[:] = usable

    in_default, hit_counts, context_tokens = 0, [], []
    depth_found = {d: 0 for d in DEPTHS}
    margin_kept = {m: 0 for m in MARGINS}
    margin_hits = {m: [] for m in MARGINS}
    gaps = []
    for s in samples:
        sections = skill.default_sections(s.question)
        hits = [h for _, section in sections for h in section if (h.get("payload") or {}).get("text")]
        hit_counts.append(len(hits))
        context_tokens.append(sum(estimate_tokens(str(h["payload"]["text"])) for h in hits))
        found = next((h for h in hits if str(h.get("id")) == s.point_id), None)
        in_default += found is not None
        best = max((float(h.get("score") or 0) for h in hits), default=0.0)
        if found is not None:
            gaps.append(best - float(found.get("score") or 0))
        for m in MARGINS:
            kept = [h for h in hits if float(h.get("score") or 0) >= best - m]
            margin_hits[m].append(len(kept))
            margin_kept[m] += found is not None and found in kept
        deep = qdrant.search(s.collection, embeddings.embed(s.question), limit=max(DEPTHS))
        rank = next((i for i, h in enumerate(deep, start=1) if str(h.get("id")) == s.point_id), None)
        for d in DEPTHS:
            depth_found[d] += rank is not None and rank <= d
        if args.show:
            print(f"{'IN ' if found else 'OUT'} rank {rank or '>50'}  {s.question[:90]}", file=sys.stderr)

    if args.hybrid:
        hybrid = hybrid_ranks(settings, qdrant, embeddings, samples)
    final = final_context(settings, qdrant, embeddings, skill.llm, samples)
    n = len(samples) or 1
    print(f"# /rag retrieval check -- {settings.runtime_label}"
          + (f" -- query prefix {args.query_prefix!r}" if args.query_prefix else "")
          + (f" -- document prefix {args.reembed_prefix!r}" if args.reembed_prefix else "")
          + (f" -- embeddings {args.reembed_model}" if args.reembed_model else "")
          + (f" -- questions in {args.question_language}" if args.question_language else "") + "\n")
    print(f"{len(samples)} questions · retrieval limit {settings.retrieval_limit}"
          + (f" · the embedding model failed on {len(FAILED_EMBEDS)} texts (skipped)" if FAILED_EMBEDS else "")
          + "\n")
    print("| Plain /rag retrieval | Value |")
    print("| --- | --- |")
    print(f"| Source passage retrieved | {in_default}/{len(samples)} ({100 * in_default / n:.0f}%) |")
    print(f"| Hits per question (median) | {statistics.median(hit_counts) if hit_counts else 0:.0f} |")
    print(f"| Retrieved text (median est. tokens) | {statistics.median(context_tokens) if context_tokens else 0:.0f} |")
    if gaps:
        print(f"| Best score minus source score, when retrieved (median / max) | "
              f"{statistics.median(gaps):.3f} / {max(gaps):.3f} |")
    print("\n| What /rag sends the model (this bot's settings) | Source passage sent | Passages (median) "
          "| Est. tokens (median) |")
    print("| --- | --- | --- | --- |")
    for label, (found_n, counts, tokens) in final.items():
        print(f"| {label} | {found_n}/{len(samples)} ({100 * found_n / n:.0f}%) | "
              f"{statistics.median(counts) if counts else 0:.0f} | {statistics.median(tokens) if tokens else 0:.0f} |")
    print("\n| Source passage in its own collection, top N | Found |")
    print("| --- | --- |")
    for d in DEPTHS:
        print(f"| {d} | {depth_found[d]}/{len(samples)} ({100 * depth_found[d] / n:.0f}%) |")
    print("\n| Keep hits within this margin of the best | Source kept | Hits kept (median) |")
    print("| --- | --- | --- |")
    for m in MARGINS:
        print(f"| {m:.2f} | {margin_kept[m]}/{len(samples)} | {statistics.median(margin_hits[m]) if margin_hits[m] else 0:.0f} |")
    if args.hybrid:
        print("\n| Over all the bot's documents, top N | Vector | Keywords (BM25) | Both (RRF) |")
        print("| --- | --- | --- | --- |")
        for d in DEPTHS:
            cells = [sum(1 for r in hybrid[kind] if r is not None and r <= d) for kind in ("vector", "bm25", "rrf")]
            print(f"| {d} | " + " | ".join(f"{c}/{len(samples)} ({100 * c / n:.0f}%)" for c in cells) + " |")
        print("\n| /rag pipeline: weights keywords:vector | " + " | ".join(f"keep {k}" for k in PIPELINE_KEEP) + " |")
        print("| --- |" + " --- |" * len(PIPELINE_KEEP))
        for wk, wv in PIPELINE_WEIGHTS:
            cells = [sum(1 for r in hybrid[f"pipe {wk}:{wv} keep {k}"] if r) for k in PIPELINE_KEEP]
            print(f"| {wk}:{wv} | " + " | ".join(f"{c}/{len(samples)} ({100 * c / n:.0f}%)" for c in cells) + " |")
        print("\n| Keyword hits first, then vector hits | Source kept |")
        print("| --- | --- |")
        for k, v in KEYWORD_FIRST:
            c = sum(1 for r in hybrid[f"first {k}+{v}"] if r)
            print(f"| {k} + {v} | {c}/{len(samples)} ({100 * c / n:.0f}%) |")


def _ids(sections: list) -> set[str]:
    return {str(h.get("id")) for _, hits in sections for h in hits}


def _bucket(value: int | None, edges: tuple[int, ...], unit: str = "") -> str:
    if value is None:
        return f">{edges[-1]}{unit}"
    low = 1
    for edge in edges:
        if value <= edge:
            return f"{low}-{edge}{unit}" if low != edge else f"{edge}{unit}"
        low = edge + 1
    return f">{edges[-1]}{unit}"


def why_missed(settings, qdrant, embeddings, llm, samples) -> None:
    """Why the source passage didn't reach the model, with keyword search on.
    Counts only: the step that dropped it, then traits of the missed
    passages and questions."""
    s_on = dataclasses.replace(settings, rag_keyword_search=True)
    skill = RagRetrieveSkill(s_on, {}, embeddings, qdrant, llm)
    by_id = {}
    for collection in bot_collections(settings) + [settings.tracker_collection]:
        try:
            for point in qdrant.scroll_by_filters(collection, {}, limit=512):
                by_id[str(point["id"])] = point.get("payload") or {}
        except Exception:  # noqa: BLE001
            continue
    steps, traits, missed, rows = Counter(), Counter(), 0, []
    for s in samples:
        sections = skill.default_sections(s.question)
        margin = drop_weak_hits(sections, s_on.rag_relevance_margin)
        picked = keywords_first(margin, keyword_hits=s_on.rag_keyword_hits, vector_hits=s_on.rag_vector_hits)
        sent = fit_passages(picked, s.question, context_tokens=s_on.rag_context_tokens,
                            passage_tokens=s_on.rag_passage_tokens)
        if s.point_id in _ids(sent):
            continue
        missed += 1
        step = ("not retrieved at all" if s.point_id not in _ids(sections)
                else "dropped by the relevance margin" if s.point_id not in _ids(margin)
                else "dropped by the keyword/vector split" if s.point_id not in _ids(picked)
                else "dropped by the token budget")
        steps[step] += 1
        flat = [h for _, hits in sections for h in hits]
        pooled = {via: [str(h.get("id")) for h in sorted((h for h in flat if (h.get("via") == "keywords") == (via == "kw")),
                                                         key=lambda h: -float(h.get("score") or 0))]
                  for via in ("kw", "vec")}
        found = [h for _, hits in sections for h in hits if str(h.get("id")) == s.point_id]
        if found:
            traits["retrieved via: " + "+".join(sorted({h.get("via") or "vector" for h in found}))] += 1
        payload = by_id.get(s.point_id, {})
        cjk = len(_CJK_CHAR.findall(s.text)) / max(1, len(s.text))
        traits["passage language: " + ("Chinese" if cjk > 0.3 else "mixed" if cjk > 0.05 else "Latin")] += 1
        traits["passage length: " + _bucket(len(s.text), (600, 1200, 1799), " chars").replace(">1799", "1800")] += 1
        shared = terms(s.question) & terms(s.text)
        traits["question terms in passage: " + _bucket(len(shared) or None, (2, 5)).replace(">5", "6+")
               if shared else "question terms in passage: 0"] += 1
        latin = [t for t in terms(s.question) if t.isascii()]
        traits["question has Latin words or digits: " + ("yes" if latin else "no")] += 1
        deep = qdrant.search(s.collection, embeddings.embed(s.question), limit=50)
        rank = next((i for i, h in enumerate(deep, start=1) if str(h.get("id")) == s.point_id), None)
        traits["vector rank in its collection: " + _bucket(rank, (2, 5, 20, 50))] += 1
        sent_hits = [h for _, hits in sent for h in hits]
        sha = payload.get("file_sha256")
        if sha and any((by_id.get(str(h.get("id"))) or {}).get("file_sha256") == sha for h in sent_hits):
            traits["another passage of the same file was sent"] += 1
        fact = s.fact.strip().lower()
        if fact and fact in s.text.lower():
            traits["fact appears word for word in the passage"] += 1
        fact_sent = bool(fact) and any(fact in str((h.get("payload") or {}).get("text") or "").lower()
                                       for h in sent_hits)
        if fact_sent:
            traits["fact appears word for word in a passage that was sent"] += 1
        sibling = [int((by_id.get(str(h.get("id"))) or {}).get("chunk_index", -99)) for h in sent_hits
                   if sha and (by_id.get(str(h.get("id"))) or {}).get("file_sha256") == sha]
        own = int(payload.get("chunk_index", -1))
        rows.append(" | ".join(str(x) for x in (
            step.split(" by the ")[-1].replace("not retrieved at all", "-"),
            *(pooled[k].index(s.point_id) + 1 if s.point_id in pooled[k] else "-" for k in ("kw", "vec")),
            rank or ">50", len(shared), f"{own + 1}/{payload.get('chunk_count', '?')}",
            ",".join(f"{c - own:+d}" for c in sorted(sibling)) or "-", "yes" if fact_sent else "no")))
    print(f"\n| Why the source passage wasn't sent ({missed} of {len(samples)} questions) | Questions |")
    print("| --- | --- |")
    for step, count in steps.most_common():
        print(f"| {step} | {count} |")
    print("\n| Dropped at | Keyword rank (all sent sections) | Vector rank (pooled) | Vector rank (own collection) "
          "| Shared terms | Chunk | Same-file chunks sent (offset) | Fact in a sent passage |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        print(f"| {row} |")
    print("\n| Traits of the missed ones | Questions |")
    print("| --- | --- |")
    for trait, count in sorted(traits.items()):
        print(f"| {trait} | {count} |")


def hybrid_ranks(settings, qdrant, embeddings, samples) -> dict[str, list[int | None]]:
    collections = bot_collections(settings)
    bm25 = Bm25(qdrant, collections)
    out = {"vector": [], "bm25": [], "rrf": []}
    out.update({f"pipe {wk}:{wv} keep {keep}": [] for wk, wv in PIPELINE_WEIGHTS for keep in PIPELINE_KEEP})
    out.update({f"first {k}+{v}": [] for k, v in KEYWORD_FIRST})
    for s in samples:
        vector = embeddings.embed(s.question)
        hits = []
        for collection in collections:
            try:
                hits += qdrant.search(collection, vector, limit=max(DEPTHS))
            except Exception:  # noqa: BLE001
                continue
        by_vector = [str(h["id"]) for h in sorted(hits, key=lambda h: -float(h.get("score") or 0))]
        by_bm25 = [pid for pid, _ in bm25.rank(s.question)]
        for kind, ranking in (("vector", by_vector), ("bm25", by_bm25), ("rrf", rrf(by_vector, by_bm25))):
            out[kind].append(ranking.index(s.point_id) + 1 if s.point_id in ranking else None)
        # The /rag pipeline: per collection the top 5 by vector and by keywords
        # (as plain /rag fetches), pooled, fused with weights, then the best kept.
        dense = []
        for collection in collections:
            try:
                dense += qdrant.search(collection, vector, limit=settings.retrieval_limit)
            except Exception:  # noqa: BLE001
                continue
        dense_ids = [str(h["id"]) for h in sorted(dense, key=lambda h: -float(h.get("score") or 0))]
        per_collection = {}
        for pid, score in bm25.rank(s.question):
            coll = bm25.collection_of[pid]
            if len(per_collection.setdefault(coll, [])) < settings.retrieval_limit:
                per_collection[coll].append((pid, score))
        sparse_ids = [pid for pid, _ in sorted((x for v in per_collection.values() for x in v), key=lambda x: -x[1])]
        for wk, wv in PIPELINE_WEIGHTS:
            fused = rrf(sparse_ids, dense_ids, weights=(wk, wv))
            for keep in PIPELINE_KEEP:
                out[f"pipe {wk}:{wv} keep {keep}"].append(1 if s.point_id in fused[:keep] else None)
        for k, v in KEYWORD_FIRST:
            chosen = sparse_ids[:k] + [pid for pid in dense_ids if pid not in sparse_ids[:k]][:v]
            out[f"first {k}+{v}"].append(1 if s.point_id in chosen else None)
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
