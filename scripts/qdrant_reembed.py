#!/usr/bin/env python3
"""Re-embed a bot's Qdrant collections with another embedding model.

Every vector in a collection must come from the same embedding model, so
switching models (docs/EMBEDDINGS.md) means re-embedding everything the bot
has stored. Runs inside the bot's Telegram container:

    docker exec -i openclaw-telegram-<bot> python3 - --model qwen3-embedding:0.6b --dims 1024 < scripts/qdrant_reembed.py
    docker exec -i openclaw-telegram-<bot> python3 - --model qwen3-embedding:0.6b --dims 1024 --apply < scripts/qdrant_reembed.py

Without --apply it only shows the plan. By default it covers the bot's /rag
collections: tracker memory (which also holds the English bot's word lists
and progress), knowledge base and every category. --collections a,b picks
others. For each collection whose vectors are not already --dims wide (or
every one with --force):

1. takes a Qdrant snapshot (the way back; see docs/EMBEDDINGS.md);
2. reads every point;
3. embeds each point's stored text with --model, into <name>__reembed_tmp,
   and checks the count. If the model fails on any text, the collection is
   left as it was;
4. checks nothing was written to the collection meanwhile (else skips it);
5. recreates <name> at the new size, keeping ids, payloads, payload indexes
   and keyword vectors, writes the points back and checks the count;
6. deletes <name>__reembed_tmp.

Then set OPENCLAW_EMBEDDING_MODEL / OPENCLAW_EMBEDDING_VECTOR_SIZE in the
bot's .env and recreate it (bin/openclawctl --profile <bot> start), so new
writes and questions use the same model.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

for candidate in (Path("/app"), Path(__file__).resolve().parent.parent / "app" if "__file__" in globals() else None):
    if candidate and candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from openclaw_runtime.categories import registry_entries  # noqa: E402
from openclaw_runtime.config import load_settings  # noqa: E402
from openclaw_runtime.http_client import get_json, request_json  # noqa: E402
from openclaw_runtime.keywords import VECTOR_NAME  # noqa: E402

TMP_SUFFIX = "__reembed_tmp"
BATCH = 32


class Reembed:
    def __init__(self, settings, model: str, dims: int) -> None:
        self.base = settings.qdrant_base_url.rstrip("/")
        self.ollama = settings.ollama_base_url.rstrip("/")
        self.timeout = max(300, settings.request_timeout)
        self.model, self.dims = model, dims

    # -- Qdrant -----------------------------------------------------------
    def info(self, collection: str) -> dict | None:
        try:
            return get_json(f"{self.base}/collections/{collection}", timeout=30).get("result") or {}
        except Exception:  # noqa: BLE001 - missing
            return None

    def count(self, collection: str) -> int:
        return int(request_json("POST", f"{self.base}/collections/{collection}/points/count",
                                {"exact": True}, timeout=self.timeout)["result"]["count"])

    def points(self, collection: str) -> list[dict]:
        out, offset = [], None
        while True:
            body = {"limit": 256, "with_payload": True, "with_vector": True}
            if offset is not None:
                body["offset"] = offset
            result = request_json("POST", f"{self.base}/collections/{collection}/points/scroll", body,
                                  timeout=self.timeout)["result"]
            out += result.get("points") or []
            offset = result.get("next_page_offset")
            if offset is None:
                return out

    def create(self, collection: str, info: dict) -> None:
        params = info["config"]["params"]
        dense = dict(params["vectors"])
        dense["size"] = self.dims
        body = {"vectors": dense}
        if params.get("sparse_vectors"):
            body["sparse_vectors"] = params["sparse_vectors"]
        request_json("PUT", f"{self.base}/collections/{collection}", body, timeout=self.timeout)
        for field, schema in (info.get("payload_schema") or {}).items():
            request_json("PUT", f"{self.base}/collections/{collection}/index?wait=true",
                         {"field_name": field, "field_schema": schema.get("data_type", "keyword")}, timeout=self.timeout)

    def write(self, collection: str, points: list[dict]) -> None:
        for start in range(0, len(points), BATCH):
            request_json("PUT", f"{self.base}/collections/{collection}/points?wait=true",
                         {"points": points[start:start + BATCH]}, timeout=self.timeout)

    def delete(self, collection: str) -> None:
        request_json("DELETE", f"{self.base}/collections/{collection}", None, timeout=self.timeout)

    def snapshot(self, collection: str) -> str:
        result = request_json("POST", f"{self.base}/collections/{collection}/snapshots?wait=true", {},
                              timeout=self.timeout)["result"]
        return str(result.get("name"))

    # -- embeddings --------------------------------------------------------
    def embed(self, text: str) -> list[float]:
        request = urllib.request.Request(f"{self.ollama}/api/embed",
                                         json.dumps({"model": self.model, "input": text}).encode(),
                                         {"Content-Type": "application/json"})
        vector = json.loads(urllib.request.urlopen(request, timeout=self.timeout).read())["embeddings"][0]
        if len(vector) != self.dims:
            raise ValueError(f"{self.model} returned {len(vector)} dimensions, not {self.dims}")
        return vector

    def reembedded(self, points: list[dict]) -> list[dict]:
        """The points with new dense vectors; keyword vectors and payloads kept."""
        out = []
        for point in points:
            payload = point.get("payload") or {}
            vector = point.get("vector")
            keywords = vector.get(VECTOR_NAME) if isinstance(vector, dict) else None
            dense = self.embed(str(payload.get("text") or payload.get("kind") or "(empty)"))
            new_vector = {"": dense, VECTOR_NAME: keywords} if keywords else (
                {"": dense} if isinstance(vector, dict) else dense)
            out.append({"id": point["id"], "vector": new_vector, "payload": payload})
        return out

    # -- one collection ----------------------------------------------------
    def dense_size(self, info: dict) -> int | None:
        vectors = info["config"]["params"].get("vectors")
        return int(vectors["size"]) if isinstance(vectors, dict) and "size" in vectors else None

    def migrate(self, collection: str, force: bool) -> str:
        info = self.info(collection)
        if info is None:
            return "skipped: no such collection"
        size = self.dense_size(info)
        if size is None:
            return "skipped: has named dense vectors (not an OpenClaw collection layout)"
        if size == self.dims and not force:
            return f"already {self.dims} dimensions (use --force to re-embed anyway)"
        tmp = collection + TMP_SUFFIX
        if self.info(tmp) is not None:
            return f"skipped: {tmp} exists from an earlier run; check it, delete it, run again"
        snapshot = self.snapshot(collection)
        before = self.count(collection)
        started = time.time()
        try:
            points = self.reembedded(self.points(collection))
        except Exception as exc:  # noqa: BLE001 - e.g. the model fails on some text
            return f"skipped, nothing changed: re-embedding failed ({exc})"
        if len(points) != before:
            return f"skipped: read {len(points)} of {before} points"
        self.create(tmp, info)
        self.write(tmp, points)
        if self.count(tmp) != before:
            self.delete(tmp)
            return "skipped: the copy came out short; nothing changed"
        if self.count(collection) != before:
            self.delete(tmp)
            return "skipped: something was saved meanwhile; run again at a quiet time"
        self.delete(collection)
        try:
            self.create(collection, info)
            self.write(collection, points)
            after = self.count(collection)
        except Exception as exc:
            return (f"FAILED after deleting the original ({exc}). The re-embedded data is in {tmp}, the original "
                    f"in snapshot {snapshot}; restore one before using the bot (docs/EMBEDDINGS.md).")
        if after != before:
            return f"FAILED: {after} of {before} points written back; the full copy is in {tmp}"
        self.delete(tmp)
        return f"done: {after} points re-embedded in {time.time() - started:.0f}s, snapshot {snapshot}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="the new Ollama embedding model, e.g. qwen3-embedding:0.6b")
    parser.add_argument("--dims", type=int, required=True, help="its vector size, e.g. 1024")
    parser.add_argument("--apply", action="store_true", help="migrate (default: only show the plan)")
    parser.add_argument("--force", action="store_true", help="re-embed collections already --dims wide")
    parser.add_argument("--collections", default="", help="comma-separated names (default: the bot's /rag ones)")
    args = parser.parse_args(argv)
    settings = load_settings()
    collections = [c.strip() for c in args.collections.split(",") if c.strip()] or (
        [settings.tracker_collection, settings.knowledge_collection]
        + ([e["collection"] for e in registry_entries(settings)] if settings.category_rag_enabled else []))
    job = Reembed(settings, args.model, args.dims)
    try:
        job.embed("check")
    except Exception as exc:  # noqa: BLE001
        print(f"the embedding model {args.model} is not usable: {exc}")
        return 1
    problems = 0
    for collection in collections:
        if not args.apply:
            info = job.info(collection)
            if info is None:
                state = "missing"
            else:
                size = job.dense_size(info)
                state = (f"already {args.dims} dimensions" if size == args.dims and not args.force else
                         f"re-embed {job.count(collection)} points ({size} -> {args.dims} dimensions)")
            print(f"{collection}: {state}")
            continue
        result = job.migrate(collection, args.force)
        problems += result.startswith(("FAILED", "skipped, nothing changed"))
        print(f"{collection}: {result}", flush=True)
    if not args.apply:
        print(f"\nPlan only. Run again with --apply, then set OPENCLAW_EMBEDDING_MODEL={args.model} and "
              f"OPENCLAW_EMBEDDING_VECTOR_SIZE={args.dims} in the bot's .env and recreate it.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
