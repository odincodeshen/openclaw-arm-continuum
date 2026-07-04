import hashlib
import json
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from openclaw_runtime.config import Settings
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.qdrant_client import QdrantClient


SUPPORTED_SUFFIXES = {".md", ".txt", ".log", ".json", ".csv", ".tsv", ".pdf"}


@dataclass(frozen=True)
class IngestResult:
    path: Path
    collection: str
    chunks: int
    skipped: bool = False
    reason: str = ""


class InboxIngestor:
    def __init__(self, settings: Settings, embeddings: EmbeddingClient, qdrant: QdrantClient) -> None:
        self.settings = settings
        self.embeddings = embeddings
        self.qdrant = qdrant
        self.state = self._load_state()

    def scan_once(self) -> list[IngestResult]:
        self.settings.inbox_path.mkdir(parents=True, exist_ok=True)
        (self.settings.inbox_path / "knowledge").mkdir(parents=True, exist_ok=True)
        (self.settings.inbox_path / "tracker").mkdir(parents=True, exist_ok=True)
        results = []
        for path in sorted(self.settings.inbox_path.rglob("*")):
            if path.is_file():
                try:
                    results.append(self.ingest_file(path))
                except Exception as exc:
                    fingerprint = self._safe_fingerprint(path)
                    self.state[str(path)] = {
                        "fingerprint": fingerprint,
                        "stat_signature": self._safe_stat_signature(path),
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(limit=8),
                        "updated_at": int(time.time()),
                    }
                    results.append(
                        IngestResult(
                            path,
                            self._collection_for(path),
                            0,
                            skipped=True,
                            reason=f"failed:{type(exc).__name__}: {exc}",
                        )
                    )
        if results:
            self._save_state()
        return results

    def ingest_file(self, path: Path) -> IngestResult:
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            return IngestResult(path, "", 0, skipped=True, reason="unsupported_suffix")

        state_key = str(path)
        stat_signature = self._stat_signature(path)
        cached = self.state.get(state_key, {})
        if cached.get("fingerprint") and cached.get("stat_signature") == stat_signature:
            return IngestResult(path, self._collection_for(path), 0, skipped=True, reason="unchanged")

        fingerprint = self._fingerprint(path)
        if cached.get("fingerprint") == fingerprint:
            self.state[state_key] = {**cached, "stat_signature": stat_signature, "updated_at": int(time.time())}
            return IngestResult(path, self._collection_for(path), 0, skipped=True, reason="unchanged")

        text = self._read_text(path).strip()
        if not text:
            self.state[state_key] = {
                "fingerprint": fingerprint,
                "stat_signature": stat_signature,
                "chunks": 0,
                "updated_at": int(time.time()),
            }
            return IngestResult(path, self._collection_for(path), 0, skipped=True, reason="empty")

        collection = self._collection_for(path)
        chunks = self._chunk_text(text)
        for index, chunk in enumerate(chunks):
            vector = self.embeddings.embed(chunk)
            self.qdrant.upsert_text(
                collection,
                chunk,
                vector,
                {
                    "source": "inbox",
                    "kind": "file_chunk",
                    "file_path": str(path),
                    "file_name": path.name,
                    "file_sha256": fingerprint,
                    "chunk_index": index,
                    "chunk_count": len(chunks),
                },
            )

        self.state[state_key] = {
            "fingerprint": fingerprint,
            "stat_signature": stat_signature,
            "collection": collection,
            "chunks": len(chunks),
            "updated_at": int(time.time()),
        }
        return IngestResult(path, collection, len(chunks))

    def _collection_for(self, path: Path) -> str:
        relative_parts = path.relative_to(self.settings.inbox_path).parts
        if relative_parts and relative_parts[0] == "tracker":
            return self.settings.tracker_collection
        return self.settings.knowledge_collection

    def _chunk_text(self, text: str) -> list[str]:
        chunk_size = max(200, self.settings.ingest_chunk_chars)
        overlap = max(0, min(self.settings.ingest_chunk_overlap, chunk_size // 2))
        chunks = []
        start = 0
        while start < len(text):
            end = min(len(text), start + chunk_size)
            chunks.append(text[start:end].strip())
            if end >= len(text):
                break
            start = end - overlap
        return [chunk for chunk in chunks if chunk]

    def _read_text(self, path: Path) -> str:
        if path.suffix.lower() == ".pdf":
            return self._read_pdf(path)
        return path.read_text(encoding="utf-8", errors="replace")

    def _read_pdf(self, path: Path) -> str:
        try:
            from pypdf import PdfReader
        except Exception as exc:
            raise RuntimeError("PDF ingestion requires pypdf in the memory watcher image") from exc

        reader = PdfReader(str(path))
        lines = [f"# {path.name}", "", f"Source PDF: {path}", ""]
        for index, page in enumerate(reader.pages, 1):
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:
                page_text = f"[PDF page extraction failed: {exc}]"
            lines.append(f"## Page {index}")
            lines.append(page_text.strip())
            lines.append("")
        return "\n".join(lines)

    def _stat_signature(self, path: Path) -> list[int]:
        stat = path.stat()
        return [stat.st_mtime_ns, stat.st_size]

    def _fingerprint(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _safe_fingerprint(self, path: Path) -> str:
        try:
            return self._fingerprint(path)
        except Exception:
            return ""

    def _safe_stat_signature(self, path: Path) -> list[int] | None:
        try:
            return self._stat_signature(path)
        except Exception:
            return None

    def _load_state(self) -> dict:
        if not self.settings.watcher_state_path.exists():
            return {}
        try:
            return json.loads(self.settings.watcher_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save_state(self) -> None:
        self.settings.watcher_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.watcher_state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
