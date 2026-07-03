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

    while RUNNING:
        try:
            results = ingestor.scan_once()
            for result in results:
                if result.skipped and result.reason != "unchanged":
                    log(f"[watcher] skipped path={result.path} reason={result.reason}")
                elif not result.skipped:
                    log(
                        f"[watcher] ingested path={result.path} "
                        f"collection={result.collection} chunks={result.chunks}"
                    )
        except Exception:
            log(traceback.format_exc())
        time.sleep(settings.watcher_poll_seconds)

    log("[watcher] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
