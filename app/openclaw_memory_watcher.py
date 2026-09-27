#!/usr/bin/env python3
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

from openclaw_runtime.config import load_settings
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.file_ingest import InboxIngestor
from openclaw_runtime.qdrant_client import QdrantClient


RUNNING = True


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    print(f"{stamp} {message}", flush=True)


def stop(_signum: int, _frame: object) -> None:
    global RUNNING
    RUNNING = False


def report_results(results, logged_skips: set[tuple[str, str]]) -> None:
    """Log each ingest, but each skipped file only the first time it's seen
    with that reason -- the inbox is rescanned every few seconds, and
    re-logging every photo, voice note and .meta.json sidecar on every scan
    buried real problems under ~100k identical lines a day."""
    for result in results:
        if result.skipped:
            if result.reason == "unchanged":
                continue
            key = (str(result.path), str(result.reason))
            if key in logged_skips:
                continue
            logged_skips.add(key)
            log(f"[watcher] skipped path={result.path} reason={result.reason}")
        else:
            log(f"[watcher] ingested path={result.path} collection={result.collection} chunks={result.chunks}")


def main() -> int:
    settings = load_settings()
    if not settings.memory_enabled:
        log("[watcher] memory disabled")
        return 0

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    embeddings = EmbeddingClient(settings)
    qdrant = QdrantClient(settings)
    qdrant.ensure_collections()
    ingestor = InboxIngestor(settings, embeddings, qdrant)
    log(f"[watcher] inbox={settings.inbox_path} poll={settings.watcher_poll_seconds}s")
    logged_skips: set[tuple[str, str]] = set()

    while RUNNING:
        try:
            report_results(ingestor.scan_once(), logged_skips)
        except Exception:
            log(traceback.format_exc())
        time.sleep(settings.watcher_poll_seconds)

    log("[watcher] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
