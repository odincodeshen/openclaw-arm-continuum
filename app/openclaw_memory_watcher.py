#!/usr/bin/env python3
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

from openclaw_runtime.alerts import Alerter
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


def check_ingest_health(results, alerter: Alerter) -> None:
    """Alert when files can't be indexed (e.g. the embedding service is
    down, so nothing new reaches /rag); a scan with no failures resolves it."""
    failed = [r for r in results if r.skipped and str(r.reason).startswith("failed:")]
    if failed:
        alerter.alert(
            "watcher-ingest",
            f"The memory watcher can't index {len(failed)} file(s); new material isn't reaching /rag.",
            f"{failed[0].path}: {failed[0].reason}",
        )
    else:
        alerter.resolve("watcher-ingest", "The memory watcher is indexing files again.")


def scanned_page_reader(settings):
    """The vision model reading a rendered PDF page, for scanned PDFs -- or
    None when vision is off or OPENCLAW_PDF_OCR_MAX_PAGES is 0."""
    if not settings.vision_enabled or settings.pdf_ocr_max_pages <= 0:
        return None
    try:
        import pypdfium2  # noqa: F401 - rendering is needed before anything is read

        from openclaw_runtime.model_catalog import load_model_registry
        from openclaw_runtime.model_client_factory import ModelClientFactory
        from openclaw_runtime.vision_client import VisionClient

        vision = VisionClient(ModelClientFactory(settings, load_model_registry(settings)).get_or_default("vision"))
    except Exception as exc:  # noqa: BLE001
        log(f"[watcher] scanned-PDF reading off: {exc}")
        return None
    return lambda image: vision.transcribe_image(image, max_tokens=settings.image_ocr_max_tokens)


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
    ingestor = InboxIngestor(settings, embeddings, qdrant, page_reader=scanned_page_reader(settings))
    log(f"[watcher] inbox={settings.inbox_path} poll={settings.watcher_poll_seconds}s")
    logged_skips: set[tuple[str, str]] = set()
    alerter = Alerter(settings, log=log)

    while RUNNING:
        try:
            results = ingestor.scan_once()
            report_results(results, logged_skips)
            check_ingest_health(results, alerter)
            alerter.resolve("watcher-scan", "The memory watcher is scanning the inbox again.")
        except Exception:
            log(traceback.format_exc())
            alerter.alert("watcher-scan", "The memory watcher can't scan its inbox.", traceback.format_exc())
        time.sleep(settings.watcher_poll_seconds)

    log("[watcher] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
