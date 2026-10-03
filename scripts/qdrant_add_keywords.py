#!/usr/bin/env python3
"""Give a bot's existing Qdrant collections the keyword vector.

Keyword search (OPENCLAW_RAG_KEYWORD_SEARCH, app/openclaw_runtime/keywords.py)
needs a sparse vector named "kw" in each collection. Collections created
since then have it. Older ones need this one-off migration, because Qdrant
can't add a vector to an existing collection. Runs inside the bot's Telegram
container:

    docker exec -i openclaw-telegram-<bot> python3 - < scripts/qdrant_add_keywords.py           # plan only
    docker exec -i openclaw-telegram-<bot> python3 - --apply < scripts/qdrant_add_keywords.py   # migrate

By default it covers the bot's /rag collections: tracker memory, knowledge
base and every category. --collections a,b picks others. For each
collection without the keyword vector, it:

1. takes a Qdrant snapshot (the way back: restore it from the Qdrant
   dashboard or API);
2. reads every point with its vector and payload;
3. writes them, with keyword vectors, to <name>__kw_tmp and checks the count;
4. checks nothing was written to the collection meanwhile (else skips it);
5. recreates <name> with the keyword vector, writes the points back with
   the same ids, payload indexes and vectors, and checks the count;
6. deletes <name>__kw_tmp.

Nothing is re-embedded, so it is quick.

--refresh recomputes the keyword vectors of collections that already have
them, in place, after a change to how terms are made (keywords.py,
rag_budget.term_list). Search keeps working meanwhile. Run it at a quiet time: a document
or /mem note saved during the few seconds a collection is being recreated
can fail and need saving again. Restart the bot afterwards, so it sees the
keyword vectors.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for candidate in (Path("/app"), Path(__file__).resolve().parent.parent / "app" if "__file__" in globals() else None):
    if candidate and candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from openclaw_runtime.config import load_settings  # noqa: E402
from openclaw_runtime.http_client import get_json, request_json  # noqa: E402
from openclaw_runtime.keywords import SPARSE_CONFIG, VECTOR_NAME, document_vector  # noqa: E402
from openclaw_runtime.qdrant_client import QdrantClient  # noqa: E402
from openclaw_runtime.rag_eval import bot_collections  # noqa: E402

TMP_SUFFIX = "__kw_tmp"
BATCH = 64


class Migration:
    def __init__(self, settings) -> None:
        self.base = settings.qdrant_base_url.rstrip("/")
        self.timeout = max(120, settings.request_timeout)

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
        request_json("PUT", f"{self.base}/collections/{collection}",
                     {"vectors": params["vectors"], "sparse_vectors": SPARSE_CONFIG}, timeout=self.timeout)
        for field, schema in (info.get("payload_schema") or {}).items():
            request_json("PUT", f"{self.base}/collections/{collection}/index?wait=true",
                         {"field_name": field, "field_schema": schema.get("data_type", "keyword")},
                         timeout=self.timeout)

    def write(self, collection: str, points: list[dict]) -> None:
        for start in range(0, len(points), BATCH):
            batch = []
            for point in points[start:start + BATCH]:
                dense = point.get("vector")
                if isinstance(dense, dict):  # already named; keep the unnamed dense part
                    dense = dense.get("")
                vector = {"": dense}
                keywords = document_vector(str((point.get("payload") or {}).get("text") or ""))
                if keywords["indices"]:
                    vector[VECTOR_NAME] = keywords
                batch.append({"id": point["id"], "vector": vector, "payload": point.get("payload") or {}})
            request_json("PUT", f"{self.base}/collections/{collection}/points?wait=true", {"points": batch},
                         timeout=self.timeout)

    def delete(self, collection: str) -> None:
        request_json("DELETE", f"{self.base}/collections/{collection}", None, timeout=self.timeout)

    def refresh(self, collection: str) -> str:
        """Recompute the keyword vectors in place (vectors endpoint; the dense
        vectors and payloads are untouched)."""
        info = self.info(collection)
        if info is None:
            return "skipped: no such collection"
        if VECTOR_NAME not in (info["config"]["params"].get("sparse_vectors") or {}):
            return "skipped: no keyword vector yet (run without --refresh first)"
        updated, offset = 0, None
        while True:
            body = {"limit": 256, "with_payload": ["text"], "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            result = request_json("POST", f"{self.base}/collections/{collection}/points/scroll", body,
                                  timeout=self.timeout)["result"]
            batch = []
            for point in result.get("points") or []:
                keywords = document_vector(str((point.get("payload") or {}).get("text") or ""))
                if keywords["indices"]:
                    batch.append({"id": point["id"], "vector": {VECTOR_NAME: keywords}})
            if batch:
                request_json("PUT", f"{self.base}/collections/{collection}/points/vectors?wait=true",
                             {"points": batch}, timeout=self.timeout)
                updated += len(batch)
            offset = result.get("next_page_offset")
            if offset is None:
                return f"refreshed {updated} keyword vectors"

    def snapshot(self, collection: str) -> str:
        result = request_json("POST", f"{self.base}/collections/{collection}/snapshots?wait=true", {},
                              timeout=self.timeout)["result"]
        return str(result.get("name"))

    def migrate(self, collection: str) -> str:
        info = self.info(collection)
        if info is None:
            return "skipped: no such collection"
        sparse = info["config"]["params"].get("sparse_vectors") or {}
        if VECTOR_NAME in sparse:
            return "already has keywords"
        if not isinstance(info["config"]["params"].get("vectors"), dict) or \
                "size" not in info["config"]["params"]["vectors"]:
            return "skipped: has named vectors (not an OpenClaw collection layout)"
        tmp = collection + TMP_SUFFIX
        if self.info(tmp) is not None:
            return f"skipped: {tmp} exists from an earlier run; check it, delete it, run again"
        snapshot = self.snapshot(collection)
        before = self.count(collection)
        points = self.points(collection)
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
            return (f"FAILED after deleting the original ({exc}). The data is in {tmp} and in snapshot "
                    f"{snapshot}; restore one of them before using the bot.")
        if after != before:
            return f"FAILED: {after} of {before} points written back; the full copy is in {tmp}"
        self.delete(tmp)
        return f"done: {after} points, snapshot {snapshot}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="migrate (default: only show the plan)")
    parser.add_argument("--refresh", action="store_true",
                        help="recompute keyword vectors in collections that already have them")
    parser.add_argument("--collections", default="", help="comma-separated names (default: the bot's /rag ones)")
    args = parser.parse_args(argv)
    settings = load_settings()
    collections = [c.strip() for c in args.collections.split(",") if c.strip()] or \
        [settings.tracker_collection] + bot_collections(settings)
    migration = Migration(settings)
    client = QdrantClient(settings)
    problems = 0
    for collection in collections:
        if not args.apply:
            info = migration.info(collection)
            state = ("missing" if info is None else "has keywords" if client.has_keywords(collection)
                     else f"needs keywords ({migration.count(collection)} points)")
            print(f"{collection}: {state}")
            continue
        result = migration.refresh(collection) if args.refresh else migration.migrate(collection)
        problems += result.startswith("FAILED")
        print(f"{collection}: {result}", flush=True)
    if not args.apply:
        print("\nPlan only. Run again with --apply to migrate, then restart the bot.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
