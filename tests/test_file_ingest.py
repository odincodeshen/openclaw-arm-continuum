import os
import tempfile
import time
import unittest
from pathlib import Path

from openclaw_runtime.file_ingest import InboxIngestor

from tests.support import build_settings


class FakeEmbeddingClient:
    def embed(self, text: str) -> list[float]:
        return [0.0]


class FakeQdrantClient:
    def __init__(self) -> None:
        self.upserts: list[tuple] = []
        self.ensured: list[str] = []

    def ensure_collection(self, collection) -> None:
        self.ensured.append(collection)

    def upsert_text(self, collection, text, vector, metadata) -> str:
        self.upserts.append((collection, text, metadata))
        return "fake-point-id"


class InboxIngestorFingerprintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.inbox = root / "inbox"
        (self.inbox / "knowledge").mkdir(parents=True)
        (self.inbox / "tracker").mkdir(parents=True)
        self.state_path = root / "watcher_state.json"
        self.settings = build_settings(
            web_enabled=False,
            inbox_path=self.inbox,
            watcher_state_path=self.state_path,
            category_registry_path=root / ".openclaw" / "categories.json",
        )

    def new_ingestor(self) -> InboxIngestor:
        return InboxIngestor(self.settings, FakeEmbeddingClient(), FakeQdrantClient())

    def test_new_file_is_ingested(self) -> None:
        note = self.inbox / "knowledge" / "note.md"
        note.write_text("hello world, this is a test note.", encoding="utf-8")

        ingestor = self.new_ingestor()
        result = ingestor.ingest_file(note)

        self.assertFalse(result.skipped)
        self.assertEqual(result.collection, "knowledge_coll")
        self.assertGreater(result.chunks, 0)

    def test_unchanged_file_skips_without_hashing(self) -> None:
        note = self.inbox / "knowledge" / "note.md"
        note.write_text("hello world, this is a test note.", encoding="utf-8")

        first = self.new_ingestor()
        first.ingest_file(note)
        first._save_state()

        second = self.new_ingestor()

        def boom(path):
            raise AssertionError("full SHA256 hash must not run for an unchanged file")

        second._fingerprint = boom

        result = second.ingest_file(note)
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "unchanged")

    def test_content_change_triggers_reingest(self) -> None:
        note = self.inbox / "knowledge" / "note.md"
        note.write_text("hello world, this is a test note.", encoding="utf-8")

        first = self.new_ingestor()
        first.ingest_file(note)
        first._save_state()

        time.sleep(1.1)
        note.write_text("hello world, this is a test note, now with more content appended.", encoding="utf-8")

        second = self.new_ingestor()
        result = second.ingest_file(note)
        self.assertFalse(result.skipped)
        self.assertGreater(result.chunks, 0)

    def test_mtime_touch_without_content_change_still_skips(self) -> None:
        # Cheap stat check misses on mtime, but the fallback full-hash
        # comparison must still recognize identical content and skip.
        note = self.inbox / "knowledge" / "note.md"
        content = "hello world, this is a test note."
        note.write_text(content, encoding="utf-8")

        first = self.new_ingestor()
        first.ingest_file(note)
        first._save_state()

        time.sleep(1.1)
        note.write_text(content, encoding="utf-8")  # same content, new mtime
        os.utime(note, None)

        second = self.new_ingestor()
        result = second.ingest_file(note)
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "unchanged")

    def test_unsupported_suffix_is_skipped(self) -> None:
        photo = self.inbox / "knowledge" / "photo.jpg"
        photo.write_bytes(b"\xff\xd8\xff")

        ingestor = self.new_ingestor()
        result = ingestor.ingest_file(photo)
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "unsupported_suffix")


class InboxIngestorCategoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "inbox"
        (self.inbox / "categories").mkdir(parents=True)
        self.settings = build_settings(
            web_enabled=False,
            inbox_path=self.inbox,
            watcher_state_path=self.root / "watcher_state.json",
            category_registry_path=self.root / ".openclaw" / "categories.json",
            category_collection_prefix="oc_cat_",
        )

    def new_ingestor(self) -> tuple[InboxIngestor, "FakeQdrantClient"]:
        qdrant = FakeQdrantClient()
        return InboxIngestor(self.settings, FakeEmbeddingClient(), qdrant), qdrant

    def test_file_under_category_dir_routes_to_category_collection(self) -> None:
        category_dir = self.inbox / "categories" / "work-notes_deadbeef"
        category_dir.mkdir(parents=True)
        note = category_dir / "spec.md"
        note.write_text("category isolation spec, keep this separate.", encoding="utf-8")

        ingestor, qdrant = self.new_ingestor()
        result = ingestor.ingest_file(note)

        self.assertFalse(result.skipped)
        self.assertEqual(result.collection, "oc_cat_work-notes_deadbeef")
        self.assertIn("oc_cat_work-notes_deadbeef", qdrant.ensured)
        _, _, metadata = qdrant.upserts[0]
        self.assertEqual(metadata["category_slug"], "work-notes_deadbeef")

    def test_meta_sidecar_supplies_display_name_and_is_not_ingested(self) -> None:
        category_dir = self.inbox / "categories" / "x_12ab34cd"
        category_dir.mkdir(parents=True)
        (category_dir / "photo.jpg.md").write_text("A photo of a server rack.", encoding="utf-8")
        (category_dir / "photo.jpg.md.meta.json").write_text(
            '{"category": "機櫃照片", "image_path": "/workspace/inbox/categories/x_12ab34cd/photo.jpg"}',
            encoding="utf-8",
        )

        ingestor, qdrant = self.new_ingestor()
        results = ingestor.scan_once()

        ingested = [r for r in results if not r.skipped]
        self.assertEqual(len(ingested), 1)
        _, _, metadata = qdrant.upserts[0]
        self.assertEqual(metadata["category"], "機櫃照片")
        self.assertEqual(metadata["image_path"], "/workspace/inbox/categories/x_12ab34cd/photo.jpg")
        skipped_reasons = {r.reason for r in results if r.skipped}
        self.assertIn("sidecar_meta", skipped_reasons)

    def test_bare_image_in_category_dir_is_skipped_without_error(self) -> None:
        category_dir = self.inbox / "categories" / "x_12ab34cd"
        category_dir.mkdir(parents=True)
        (category_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff")

        ingestor, _ = self.new_ingestor()
        result = ingestor.ingest_file(category_dir / "photo.jpg")
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "unsupported_suffix")


class InboxIngestorAttributionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "inbox"
        (self.inbox / "knowledge").mkdir(parents=True)
        self.settings = build_settings(
            web_enabled=False,
            inbox_path=self.inbox,
            watcher_state_path=self.root / "watcher_state.json",
            category_registry_path=self.root / ".openclaw" / "categories.json",
        )

    def _ingest(self, path: Path):
        qdrant = FakeQdrantClient()
        InboxIngestor(self.settings, FakeEmbeddingClient(), qdrant).ingest_file(path)
        return qdrant

    def test_meta_sidecar_original_name_lands_in_payload(self) -> None:
        doc = self.inbox / "knowledge" / "20260906-report.md"
        doc.write_text("some report body text here", encoding="utf-8")
        (self.inbox / "knowledge" / "20260906-report.md.meta.json").write_text(
            '{"original_file_name": "第一季報告.pdf"}', encoding="utf-8"
        )
        qdrant = self._ingest(doc)
        _, _, metadata = qdrant.upserts[0]
        self.assertEqual(metadata["original_file_name"], "第一季報告.pdf")

    def test_markdown_h1_becomes_doc_title(self) -> None:
        doc = self.inbox / "knowledge" / "notes.md"
        doc.write_text("# Arm Neoverse V3 Notes\n\nbody", encoding="utf-8")
        qdrant = self._ingest(doc)
        _, _, metadata = qdrant.upserts[0]
        self.assertEqual(metadata["doc_title"], "Arm Neoverse V3 Notes")

    def test_plain_text_has_no_title(self) -> None:
        doc = self.inbox / "knowledge" / "plain.txt"
        doc.write_text("just a line, no heading", encoding="utf-8")
        qdrant = self._ingest(doc)
        _, _, metadata = qdrant.upserts[0]
        self.assertNotIn("doc_title", metadata)


class ChunkTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.inbox = root / "inbox"
        (self.inbox / "knowledge").mkdir(parents=True)
        (self.inbox / "tracker").mkdir(parents=True)

    def new_ingestor(self, chunk_chars: int = 250, chunk_overlap: int = 20) -> InboxIngestor:
        settings = build_settings(
            web_enabled=False,
            inbox_path=self.inbox,
            watcher_state_path=Path(self.tmp.name) / "watcher_state.json",
            category_registry_path=Path(self.tmp.name) / ".openclaw" / "categories.json",
            ingest_chunk_chars=chunk_chars,
            ingest_chunk_overlap=chunk_overlap,
        )
        return InboxIngestor(settings, FakeEmbeddingClient(), FakeQdrantClient())

    def test_short_sections_are_packed_into_one_chunk(self) -> None:
        text = "## Intro\nShort intro body.\n\n## Details\nShort details body."
        ingestor = self.new_ingestor(chunk_chars=250)
        chunks = ingestor._chunk_text(text)
        self.assertEqual(len(chunks), 1)
        self.assertIn("## Intro", chunks[0])
        self.assertIn("## Details", chunks[0])

    def test_sections_that_together_exceed_chunk_size_split_at_the_boundary(self) -> None:
        section_a = "## Section A\n" + ("a" * 150)
        section_b = "## Section B\n" + ("b" * 150)
        text = f"{section_a}\n\n{section_b}"
        ingestor = self.new_ingestor(chunk_chars=250)
        chunks = ingestor._chunk_text(text)

        self.assertEqual(len(chunks), 2)
        self.assertIn("## Section A", chunks[0])
        self.assertNotIn("## Section B", chunks[0])
        self.assertIn("## Section B", chunks[1])
        self.assertNotIn("## Section A", chunks[1])

    def test_oversized_section_falls_back_to_character_slicing(self) -> None:
        short_section = "## Short\nbrief body."
        huge_section = "## Huge\n" + ("x" * 400)
        text = f"{short_section}\n\n{huge_section}"
        ingestor = self.new_ingestor(chunk_chars=250, chunk_overlap=20)
        chunks = ingestor._chunk_text(text)

        self.assertEqual(chunks[0], short_section)
        self.assertGreater(len(chunks), 2)
        for chunk in chunks[1:]:
            self.assertLessEqual(len(chunk), 250)

    def test_plain_text_with_no_headers_uses_character_slicing_like_before(self) -> None:
        text = "no headers here, " * 30
        ingestor = self.new_ingestor(chunk_chars=250, chunk_overlap=20)
        chunks = ingestor._chunk_text(text)

        self.assertGreater(len(chunks), 1)
        reconstructed_start = chunks[0][:230]
        self.assertTrue(text.startswith(reconstructed_start))

    def test_single_short_document_yields_one_chunk(self) -> None:
        text = "# Title\n\nJust one small paragraph of content."
        ingestor = self.new_ingestor(chunk_chars=250)
        chunks = ingestor._chunk_text(text)
        self.assertEqual(len(chunks), 1)
        self.assertIn("Just one small paragraph", chunks[0])


if __name__ == "__main__":
    unittest.main()
