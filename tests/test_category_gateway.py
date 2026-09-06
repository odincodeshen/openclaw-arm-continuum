import tempfile
import unittest
from pathlib import Path

import openclaw_telegram_gateway as gateway
from openclaw_runtime import categories

from tests.support import build_settings


class CategoryGatewayTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.inbox = root / "inbox"
        self.inbox.mkdir()
        self.settings = build_settings(
            inbox_path=self.inbox,
            category_registry_path=self.inbox / ".openclaw" / "categories.json",
            category_collection_prefix="oc_cat_",
            category_pending_ttl_seconds=100,
        )
        self._orig_settings = gateway.settings
        gateway.settings = self.settings
        self.addCleanup(setattr, gateway, "settings", self._orig_settings)

        self.sent: list[tuple[int, str]] = []
        self._orig_send = gateway.send_message
        gateway.send_message = lambda chat_id, text: self.sent.append((chat_id, text))
        self.addCleanup(setattr, gateway, "send_message", self._orig_send)

        with gateway.PENDING_CATEGORY_LOCK:
            gateway.PENDING_CATEGORY.clear()
        self.addCleanup(gateway.PENDING_CATEGORY.clear)


class PendingStateMachineTest(CategoryGatewayTestBase):
    def test_set_and_pop_pending(self) -> None:
        gateway.set_pending_category(42, {"path": "/x/a.pdf", "kind": "document", "note": ""})
        self.assertTrue(gateway.has_pending_category(42))
        popped = gateway.pop_pending_category(42)
        self.assertEqual(len(popped["items"]), 1)
        self.assertFalse(gateway.has_pending_category(42))

    def test_resolve_pending_routes_items_to_category(self) -> None:
        captured = {}

        def fake_ingest(chat_id, items, entry):
            captured["chat_id"] = chat_id
            captured["items"] = items
            captured["entry"] = entry

        self._orig_run = gateway._run_category_ingest
        gateway._run_category_ingest = fake_ingest
        self.addCleanup(setattr, gateway, "_run_category_ingest", self._orig_run)

        gateway.set_pending_category(7, {"path": "/x/spec.pdf", "kind": "document", "note": ""})
        handled = gateway.resolve_pending_with_category(7, "工作筆記")

        # give the worker thread a moment
        for _ in range(50):
            if "entry" in captured:
                break
            import time

            time.sleep(0.01)

        self.assertTrue(handled)
        self.assertEqual(captured["entry"]["display"], "工作筆記")
        self.assertEqual(captured["items"][0]["path"], "/x/spec.pdf")
        self.assertFalse(gateway.has_pending_category(7))

    def test_resolve_pending_rejects_bad_name_and_keeps_items(self) -> None:
        gateway.set_pending_category(9, {"path": "/x/spec.pdf", "kind": "document", "note": ""})
        handled = gateway.resolve_pending_with_category(9, "/mem")
        self.assertTrue(handled)
        self.assertTrue(gateway.has_pending_category(9))
        self.assertTrue(any("will not work" in text for _, text in self.sent))

    def test_no_pending_returns_false(self) -> None:
        self.assertFalse(gateway.resolve_pending_with_category(1, "任何類別"))

    def test_expired_pending_document_falls_back_to_knowledge(self) -> None:
        staged_dir = self.inbox / ".staging" / "telegram"
        staged_dir.mkdir(parents=True)
        staged = staged_dir / "old.pdf"
        staged.write_bytes(b"%PDF-1.4")

        gateway.set_pending_category(5, {"path": str(staged), "kind": "document", "note": ""})
        with gateway.PENDING_CATEGORY_LOCK:
            gateway.PENDING_CATEGORY[5]["updated_at"] = 0  # force-expire

        gateway.sweep_expired_pending()

        self.assertFalse(gateway.has_pending_category(5))
        self.assertFalse(staged.exists())
        moved = list((self.inbox / "knowledge" / "telegram").glob("*.pdf"))
        self.assertEqual(len(moved), 1)


class CategoryIngestTest(CategoryGatewayTestBase):
    def test_ingest_document_moves_file_and_writes_sidecar(self) -> None:
        src_dir = self.inbox / ".staging" / "telegram"
        src_dir.mkdir(parents=True)
        src = src_dir / "20260101-report.md"
        src.write_text("quarterly report body", encoding="utf-8")

        entry = categories.upsert_registry_entry(self.settings, "報告")
        target = gateway.ingest_document_into_category(src, entry, note="Q1", original_name="第一季報告.pdf")

        self.assertTrue(target.exists())
        self.assertFalse(src.exists())
        self.assertEqual(
            target.parent, self.inbox / "categories" / entry["slug"]
        )
        import json as _json

        sidecar = _json.loads(target.with_name(target.name + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["category"], "報告")
        self.assertEqual(sidecar["original_file_name"], "第一季報告.pdf")

    def test_write_upload_meta_records_original_name(self) -> None:
        d = self.inbox / "knowledge" / "telegram"
        d.mkdir(parents=True)
        stored = d / "20260906-100000-pdf.pdf"
        stored.write_bytes(b"%PDF-1.4")
        gateway.write_upload_meta(stored, "我的中文檔名.pdf")
        import json as _json

        data = _json.loads(stored.with_name(stored.name + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(data["original_file_name"], "我的中文檔名.pdf")

    def test_ingest_image_uses_vision_client_and_writes_markdown(self) -> None:
        media = self.inbox / "media" / "telegram"
        media.mkdir(parents=True)
        img = media / "rack.jpg"
        img.write_bytes(b"\xff\xd8\xff\xd9")

        class FakeVision:
            def describe_image(self, path, instruction=None, *, max_tokens=None):
                return "A server rack with three switches. Labels: SW1, SW2, SW3."

        self._orig_vision = gateway.vision
        gateway.vision = FakeVision()
        self.addCleanup(setattr, gateway, "vision", self._orig_vision)

        entry = categories.upsert_registry_entry(self.settings, "機房")
        doc = gateway.ingest_image_into_category(1, img, entry, note="機櫃照")

        self.assertTrue(doc.name.endswith(".md"))
        body = doc.read_text(encoding="utf-8")
        self.assertIn("A server rack", body)
        sidecar_data = doc.with_name(doc.name + ".meta.json").read_text(encoding="utf-8")
        self.assertIn("image_path", sidecar_data)
        self.assertTrue((self.inbox / "categories" / entry["slug"] / "media" / "rack.jpg").exists())


class ProcessImageMessageTest(CategoryGatewayTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._set_vision_enabled(True)
        self.img = self.inbox / "pic.jpg"
        self.img.write_bytes(b"\xff\xd8\xff\xd9")

    def _set_vision_enabled(self, value: bool) -> None:
        import dataclasses

        self.settings = dataclasses.replace(self.settings, vision_enabled=value)
        gateway.settings = self.settings

    def _install_vision(self, fake) -> None:
        orig = gateway.vision
        gateway.vision = fake
        self.addCleanup(setattr, gateway, "vision", orig)

    def test_routes_through_vision_client_with_caption(self) -> None:
        calls = []

        class FakeVision:
            endpoint_id = "vision"

            def describe_image(self, path, instruction=None, *, max_tokens=None):
                calls.append((path, instruction, max_tokens))
                return "A network switch, model X."

        self._install_vision(FakeVision())
        gateway.process_image_message(5, self.img, "what model is this?")

        self.assertEqual(calls[0][1], "what model is this?")
        self.assertEqual(calls[0][2], self.settings.vision_max_tokens)
        self.assertTrue(any("model X" in text for _, text in self.sent))

    def test_disabled_vision_skips_call(self) -> None:
        self._set_vision_enabled(False)
        self._install_vision(object())  # any attribute access would raise
        gateway.process_image_message(5, self.img, "")
        self.assertTrue(any("OPENCLAW_VISION_ENABLED=false" in t for _, t in self.sent))

    def test_vision_error_points_at_setup_docs(self) -> None:
        class FakeVision:
            endpoint_id = "vision"

            def describe_image(self, path, instruction=None, *, max_tokens=None):
                raise gateway.VisionError("endpoint down")

        self._install_vision(FakeVision())
        gateway.process_image_message(5, self.img, "")
        self.assertTrue(any("vision_smoke.py" in t for _, t in self.sent))


class CategoryCommandTest(CategoryGatewayTestBase):
    def test_cat_list_empty(self) -> None:
        self.assertTrue(gateway.handle_category_command(100, "/cat list"))
        self.assertTrue(any("No categories yet" in t for _, t in self.sent))

    def test_cat_list_after_upsert(self) -> None:
        categories.upsert_registry_entry(self.settings, "工作筆記")
        gateway.handle_category_command(100, "/cat list")
        self.assertTrue(any("工作筆記" in t for _, t in self.sent))

    def test_non_cat_text_ignored(self) -> None:
        self.assertFalse(gateway.handle_category_command(100, "hello"))


if __name__ == "__main__":
    unittest.main()
