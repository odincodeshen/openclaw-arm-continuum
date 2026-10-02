import hashlib
import json
import tempfile
import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from openclaw_runtime.categories import (
    category_collection_name,
    ensure_registry_entry_for_slug,
    is_category_collection,
)
from openclaw_runtime.config import Settings
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.qdrant_client import QdrantClient


SUPPORTED_SUFFIXES = {".md", ".txt", ".log", ".json", ".csv", ".tsv", ".pdf"}
META_SIDECAR_SUFFIX = ".meta.json"

_HEADER_RE = re.compile(r"^#{1,6}\s+\S")


@dataclass(frozen=True)
class IngestResult:
    path: Path
    collection: str
    chunks: int
    skipped: bool = False
    reason: str = ""


# A PDF page with fewer characters than this in its text layer is treated as
# a scanned image and, when a page reader is set, read from its rendering.
SCANNED_PAGE_MIN_CHARS = 20


def render_pdf_page(path: Path, index: int, target: Path, scale: float = 2.0) -> Path:
    """Render one PDF page (0-based) to a PNG -- about 144 dpi at scale 2."""
    import pypdfium2

    pdf = pypdfium2.PdfDocument(str(path))
    try:
        page = pdf[index]
        page.render(scale=scale).to_pil().save(target, format="PNG")
    finally:
        pdf.close()
    return target


class InboxIngestor:
    def __init__(
        self,
        settings: Settings,
        embeddings: EmbeddingClient,
        qdrant: QdrantClient,
        page_reader: Callable[[Path], str] | None = None,
    ) -> None:
        """page_reader(png) -> text reads a scanned PDF page (the vision
        model's verbatim transcription); None leaves such pages empty."""
        self.settings = settings
        self.embeddings = embeddings
        self.qdrant = qdrant
        self.page_reader = page_reader
        self.state = self._load_state()
        self._ensured_collections: set[str] = set()

    def scan_once(self) -> list[IngestResult]:
        self.settings.inbox_path.mkdir(parents=True, exist_ok=True)
        (self.settings.inbox_path / "knowledge").mkdir(parents=True, exist_ok=True)
        (self.settings.inbox_path / "tracker").mkdir(parents=True, exist_ok=True)
        (self.settings.inbox_path / self.settings.category_inbox_dirname).mkdir(parents=True, exist_ok=True)
        results = []
        for path in sorted(self.settings.inbox_path.rglob("*")):
            if path.is_file() and not self._is_hidden(path):
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
        if path.name.endswith(META_SIDECAR_SUFFIX):
            return IngestResult(path, "", 0, skipped=True, reason="sidecar_meta")
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
        self._ensure_collection(collection, path)
        extra_metadata = self._source_metadata(path, collection, text)
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
                    **extra_metadata,
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

    def _is_hidden(self, path: Path) -> bool:
        # Skip dotfiles and dot-directories under the inbox: the category
        # registry (inbox/.openclaw/) and the two-step upload staging area
        # (inbox/.staging/) live there and must never be ingested.
        try:
            parts = path.relative_to(self.settings.inbox_path).parts
        except ValueError:
            return False
        return any(part.startswith(".") for part in parts)

    def _collection_for(self, path: Path) -> str:
        relative_parts = path.relative_to(self.settings.inbox_path).parts
        if relative_parts:
            if (
                self.settings.category_rag_enabled
                and relative_parts[0] == self.settings.category_inbox_dirname
                and len(relative_parts) >= 3
            ):
                return category_collection_name(self.settings, relative_parts[1])
            if relative_parts[0] == "tracker":
                return self.settings.tracker_collection
        return self.settings.knowledge_collection

    def _category_slug_for(self, path: Path) -> str | None:
        relative_parts = path.relative_to(self.settings.inbox_path).parts
        if (
            self.settings.category_rag_enabled
            and len(relative_parts) >= 3
            and relative_parts[0] == self.settings.category_inbox_dirname
        ):
            return relative_parts[1]
        return None

    def _ensure_collection(self, collection: str, path: Path) -> None:
        if collection in self._ensured_collections:
            return
        if is_category_collection(self.settings, collection):
            self.qdrant.ensure_collection(collection)
            slug = self._category_slug_for(path)
            if slug:
                try:
                    ensure_registry_entry_for_slug(self.settings, slug)
                except OSError:
                    pass
        self._ensured_collections.add(collection)

    def _read_sidecar(self, path: Path) -> dict:
        sidecar = path.with_name(path.name + META_SIDECAR_SUFFIX)
        if not sidecar.exists():
            return {}
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _source_metadata(self, path: Path, collection: str, text: str) -> dict:
        """Attribution + category metadata merged into every chunk's payload."""
        sidecar = self._read_sidecar(path)
        metadata: dict = {}
        for key in ("original_file_name", "origin", "source_url", "caption_note"):
            value = sidecar.get(key)
            if value:
                metadata[key] = value

        title = self._extract_title(text)
        if title:
            metadata.setdefault("doc_title", title)

        slug = self._category_slug_for(path)
        if slug:
            metadata["category_slug"] = slug
            metadata["category"] = sidecar.get("category") or slug
            if sidecar.get("image_path"):
                metadata["image_path"] = sidecar["image_path"]

        return metadata

    @staticmethod
    def _extract_title(text: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()[:200]
            return ""
        return ""

    def _chunk_text(self, text: str) -> list[str]:
        chunk_size = max(200, self.settings.ingest_chunk_chars)
        overlap = max(0, min(self.settings.ingest_chunk_overlap, chunk_size // 2))
        chunks: list[str] = []
        buffer = ""
        for section in self._split_into_sections(text):
            if len(section) > chunk_size:
                if buffer:
                    chunks.append(buffer.strip())
                    buffer = ""
                chunks.extend(self._slice_by_chars(section, chunk_size, overlap))
                continue
            candidate = f"{buffer}\n\n{section}" if buffer else section
            if len(candidate) <= chunk_size:
                buffer = candidate
            else:
                if buffer:
                    chunks.append(buffer.strip())
                buffer = section
        if buffer:
            chunks.append(buffer.strip())
        return [chunk for chunk in chunks if chunk]

    @staticmethod
    def _split_into_sections(text: str) -> list[str]:
        """Split on markdown header lines, each section keeping its header
        attached to its body -- lets _chunk_text pack whole sections
        together instead of slicing mid-topic on a fixed character count."""
        lines = text.splitlines()
        sections: list[str] = []
        current: list[str] = []
        for line in lines:
            if _HEADER_RE.match(line) and current:
                sections.append("\n".join(current).strip())
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append("\n".join(current).strip())
        return [section for section in sections if section]

    @staticmethod
    def _slice_by_chars(text: str, chunk_size: int, overlap: int) -> list[str]:
        chunks = []
        start = 0
        while start < len(text):
            end = min(len(text), start + chunk_size)
            chunks.append(text[start:end].strip())
            if end >= len(text):
                break
            start = end - overlap
        return chunks

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
        read_pages = 0
        for index, page in enumerate(reader.pages, 1):
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:
                page_text = f"[PDF page extraction failed: {exc}]"
            heading = f"## Page {index}"
            if (
                len(page_text.strip()) < SCANNED_PAGE_MIN_CHARS
                and self.page_reader is not None
                and read_pages < self.settings.pdf_ocr_max_pages
            ):
                read_pages += 1
                scanned = self._read_scanned_page(path, index - 1)
                if scanned:
                    page_text, heading = scanned, f"## Page {index} (read from the page image)"
            lines.append(heading)
            lines.append(page_text.strip())
            lines.append("")
        return "\n".join(lines)

    def _read_scanned_page(self, path: Path, index: int) -> str:
        """Text of a scanned page, cached by file content and page so that
        re-indexing the same file never reads it again."""
        cache = self.settings.watcher_state_path.parent / "pdf_ocr" / f"{self._safe_fingerprint(path)[:24]}-{index}.txt"
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                image = render_pdf_page(path, index, Path(tmp) / "page.png")
                text = (self.page_reader(image) or "").strip()
        except Exception as exc:  # noqa: BLE001 - one unreadable page shouldn't stop the file
            print(f"[watcher] scanned page {index + 1} of {path.name} not read: {exc}", flush=True)
            return ""
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        return text

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
