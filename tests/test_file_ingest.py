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


if __name__ == "__main__":
    unittest.main()
