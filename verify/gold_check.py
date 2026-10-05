#!/usr/bin/env python3
"""Answer quality on the fixed gold set (bin/verify gold).

Runs inside the verify sandbox, with the same isolation as verify/runner.py:
made-up documents, throwaway collections, no profile, no Telegram. It:

1. files verify/gold/corpus/ as a bot would (knowledge base and categories)
   and indexes it with this machine's embedding model;
2. for each question in verify/gold/gold.yaml, checks whether /rag sent the
   right document to the model (retrieval). It uses the bot's real /rag
   settings: relevance margin, keyword search, budget;
3. asks it through /rag and checks the answer for an expected string
   (answers; no model judges);
4. reads each image in verify/gold/images/ with the vision model and scores
   the characters against its .txt (ocr);
5. times each answer and image.

Prints one JSON line with the scores, per-question results and timings.

    python /src/verify/gold_check.py --prefix verify_<run>_
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import statistics
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
GOLD = REPO / "verify" / "gold"
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "verify"))
os.environ.setdefault("OPENCLAW_MODEL_CATALOG", str(REPO / "app" / "models.json"))


def normalise(text: str) -> str:
    """For comparing image text: no whitespace, full-width punctuation as half-width."""
    table = str.maketrans({"：": ":", "，": ",", "（": "(", "）": ")", "｜": "|", "￥": "¥", "。": "."})
    return re.sub(r"\s+", "", text.translate(table))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--only", choices=["rag", "ocr"], default=None)
    args = parser.parse_args(argv)
    import runner  # the sandbox isolation (made-up chat ID, throwaway collections, no real token)

    runner.prepare_environment({}, args.prefix)
    from openclaw_runtime import categories
    from openclaw_runtime.categories import category_slug
    from openclaw_runtime.config import load_settings
    from openclaw_runtime.embedding_client import EmbeddingClient
    from openclaw_runtime.file_ingest import InboxIngestor
    from openclaw_runtime.llm_client import estimate_tokens
    from openclaw_runtime.model_catalog import load_model_registry
    from openclaw_runtime.model_client_factory import ModelClientFactory
    from openclaw_runtime.qdrant_client import QdrantClient
    from openclaw_runtime.rag_budget import drop_weak_hits, fit_passages, keywords_first
    from openclaw_runtime.skills.memory import RagRetrieveSkill
    from openclaw_runtime.vision_client import VisionClient

    gold = yaml.safe_load((GOLD / "gold.yaml").read_text(encoding="utf-8"))
    settings = load_settings()
    factory = ModelClientFactory(settings, load_model_registry(settings))
    llm = factory.get("local_default")
    embeddings, qdrant = EmbeddingClient(settings), QdrantClient(settings)
    result: dict = {"model": llm.model, "questions": [], "images": [],
                    "minimums": gold["minimums"], "tolerances": gold["tolerances"], "settings": {
        "keyword_search": settings.rag_keyword_search, "relevance_margin": settings.rag_relevance_margin,
        "context_tokens": settings.rag_context_tokens}}
    try:
        if args.only in (None, "rag"):
            started = time.time()
            qdrant.ensure_collections()
            for name, place in gold["documents"].items():
                source = GOLD / "corpus" / name
                if place == "knowledge":
                    target = settings.inbox_path / "knowledge" / name
                else:
                    slug = category_slug(place)
                    target = settings.inbox_path / settings.category_inbox_dirname / slug / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    (target.parent / (name + ".meta.json")).write_text(json.dumps(
                        {"category": place, "category_slug": slug, "original_file_name": name}), encoding="utf-8")
                    categories.upsert_registry_entry(settings, place)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            InboxIngestor(settings, embeddings, qdrant).scan_once()
            result["index_seconds"] = round(time.time() - started, 1)
            skill = RagRetrieveSkill(settings, {}, embeddings, qdrant, llm)
            for item in gold["questions"]:
                sections = drop_weak_hits(skill.default_sections(item["q"]), settings.rag_relevance_margin)
                sections = keywords_first(sections, keyword_hits=settings.rag_keyword_hits,
                                          vector_hits=settings.rag_vector_hits)
                sections = fit_passages(sections, item["q"], context_tokens=settings.rag_context_tokens,
                                        passage_tokens=settings.rag_passage_tokens)
                sent = [h for _, hits in sections for h in hits]
                retrieved = any((h.get("payload") or {}).get("file_name") == item["source"] for h in sent)
                started = time.time()
                try:
                    answer = skill.run(f"/rag {item['q']}").answer
                except Exception as exc:  # noqa: BLE001 - a failed answer is a wrong answer
                    answer = f"(error: {exc})"
                seconds = round(time.time() - started, 1)
                right = any(str(a).lower() in answer.lower() for a in item["a"])
                result["questions"].append({
                    "id": item["id"], "group": item["group"], "retrieved": retrieved, "right": right,
                    "seconds": seconds, "sent_tokens": sum(estimate_tokens(str((h.get("payload") or {}).get("text")
                                                                            or "")) for h in sent),
                    "answer": answer[:300] if not right else ""})
                print(f"{'R' if retrieved else '-'}{'A' if right else '-'} {seconds:6.1f}s {item['id']}",
                      file=sys.stderr, flush=True)
        if args.only in (None, "ocr") and settings.vision_enabled:
            vision = VisionClient(factory.get_or_default("vision"))
            for name in gold["images"]:
                truth = (GOLD / "images" / f"{name}.txt").read_text(encoding="utf-8")
                started = time.time()
                try:
                    text = vision.transcribe_image(GOLD / "images" / f"{name}.png", max_tokens=600)
                except Exception as exc:  # noqa: BLE001
                    text = f"(error: {exc})"
                seconds = round(time.time() - started, 1)
                score = difflib.SequenceMatcher(None, normalise(truth), normalise(text)).ratio()
                result["images"].append({"name": name, "accuracy": round(score, 3), "seconds": seconds,
                                         "text": text[:300] if score < 0.95 else ""})
                print(f"{score:.3f} {seconds:6.1f}s {name}", file=sys.stderr, flush=True)
    finally:
        runner.drop_collections(settings.qdrant_base_url, args.prefix)
    questions, images = result["questions"], result["images"]
    groups = sorted({q["group"] for q in questions})
    result["scores"] = {
        "retrieval": round(sum(q["retrieved"] for q in questions) / len(questions), 3) if questions else None,
        "answers": round(sum(q["right"] for q in questions) / len(questions), 3) if questions else None,
        "ocr": round(sum(i["accuracy"] for i in images) / len(images), 3) if images else None,
        **{f"answers_{g}": round(sum(q["right"] for q in questions if q["group"] == g)
                                 / sum(1 for q in questions if q["group"] == g), 3) for g in groups},
        **{f"retrieval_{g}": round(sum(q["retrieved"] for q in questions if q["group"] == g)
                                   / sum(1 for q in questions if q["group"] == g), 3) for g in groups},
    }
    result["timing"] = {
        "answer_seconds_median": statistics.median([q["seconds"] for q in questions]) if questions else None,
        "ocr_seconds_median": statistics.median([i["seconds"] for i in images]) if images else None,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
