import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openclaw_telegram_gateway as gateway
from openclaw_runtime.vision_client import NO_TEXT, READER_SYSTEM, VisionClient, describe_instruction


class FakeLlm:
    endpoint_id = "x"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def read_image(self, path, prompt, *, system, max_tokens):
        self.calls.append((prompt, system, max_tokens))
        return self.replies.pop(0)


class TranscribeTest(unittest.TestCase):
    def test_verbatim_prompt_without_the_persona(self) -> None:
        llm = FakeLlm([("車票 G123 | 上海虹桥 → 北京南", "stop")])
        text = VisionClient(llm).transcribe_image(Path("x.jpg"), max_tokens=4096)
        self.assertEqual(text, "車票 G123 | 上海虹桥 → 北京南")
        prompt, system, max_tokens = llm.calls[0]
        self.assertEqual(system, READER_SYSTEM)
        self.assertIn("do not convert between Traditional and Simplified", prompt)
        self.assertEqual(max_tokens, 4096)

    def test_cut_off_text_gets_one_continuation(self) -> None:
        llm = FakeLlm([("line 1\nline 2", "length"), ("line 3", "stop")])
        self.assertEqual(VisionClient(llm).transcribe_image(Path("x.jpg")), "line 1\nline 2\nline 3")
        self.assertIn("continue from exactly where it stops:\nline 1\nline 2", llm.calls[1][0])
        llm = FakeLlm([("a", "length"), ("b", "length")])
        self.assertTrue(VisionClient(llm).transcribe_image(Path("x.jpg")).endswith("[transcription cut off at the length limit]"))

    def test_no_text(self) -> None:
        self.assertEqual(VisionClient(FakeLlm([(NO_TEXT, "stop")])).transcribe_image(Path("x.jpg")), "")

    def test_description_language_rule(self) -> None:
        prompt = describe_instruction("Traditional Chinese (繁體中文)", note="receipt")
        self.assertIn("same language and script as the main text", prompt)
        self.assertIn("if it has no text, write it in Traditional Chinese (繁體中文)", prompt)
        self.assertIn("note, use it as context: receipt", prompt)


class UncategorisedPhotoTest(unittest.TestCase):
    def test_photo_goes_to_the_knowledge_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            photo = Path(tmp) / "p.jpg"
            photo.write_bytes(b"\xff\xd8\xff\xd9")

            class FakeVision:
                def transcribe_image(self, path, *, max_tokens=4096):
                    return "Booking ref ABC123"

                def describe_for_index(self, path, *, fallback_language="", note="", max_tokens=400):
                    return "A hotel booking screenshot."

            settings = dataclasses.replace(gateway.settings, inbox_path=inbox, vision_enabled=True,
                                           index_chat_photos=True)
            with patch.object(gateway, "settings", settings), patch.object(gateway, "vision", FakeVision()):
                doc = gateway.index_image_to_knowledge(photo, original_name="p.jpg")
                self.assertEqual(doc.parent, inbox / "knowledge" / "telegram")
                self.assertIn("Booking ref ABC123", doc.read_text(encoding="utf-8"))
                self.assertTrue((inbox / "knowledge" / "telegram" / "media" / "p.jpg").exists())
            with patch.object(gateway, "settings", dataclasses.replace(settings, index_chat_photos=False)):
                self.assertIsNone(gateway.index_image_to_knowledge(photo))

    def test_cancelled_image_is_indexed_not_dropped(self) -> None:
        indexed = []

        class InlineThread:
            def __init__(inner, target=None, args=(), daemon=None):
                inner.target, inner.args = target, args

            def start(inner):
                inner.target(*inner.args)

        with patch.object(gateway, "index_image_to_knowledge", lambda path, note="", name="", chat_id=None: indexed.append((path, chat_id))), \
                patch.object(gateway.threading, "Thread", InlineThread):
            gateway.sweep_pending_items_to_default({"items": [{"path": "/x/a.jpg", "kind": "image", "note": ""}]}, 5)
        self.assertEqual(indexed, [(Path("/x/a.jpg"), 5)])


class ImageTextCardTest(unittest.TestCase):
    def test_card_layout_and_long_text_split(self) -> None:
        from tests.card_checks import assert_valid_telegram_html

        html = gateway.render_image_text_card("slide.jpg", "#life", "10 questions <to> find out\n1. Who?", "A slide.")
        assert_valid_telegram_html(self, html)
        self.assertTrue(html.startswith("<b>【圖片文字】</b>· slide.jpg → #life"))
        self.assertIn("<blockquote expandable>10 questions &lt;to&gt; find out\n1. Who?</blockquote>", html)
        self.assertTrue(html.endswith("<i>A slide.</i>"))
        long = "\n".join(f"line {n} " + "x" * 90 for n in range(100))
        html = gateway.render_image_text_card("doc.jpg", "knowledge base", long, "")
        self.assertGreaterEqual(html.count("<blockquote expandable>"), 4)
        for part in gateway.split_html_message(html, 3500):
            self.assertLessEqual(len(part), 3500)
        self.assertIn("No text found", gateway.render_image_text_card("p.jpg", "#x", "", "A cat."))

    def test_category_ingest_sends_the_card(self) -> None:
        sent = []

        class FakeVision:
            def transcribe_image(self, path, *, max_tokens=4096):
                return "Booking ref ABC123"

            def describe_for_index(self, path, *, fallback_language="", note="", max_tokens=400):
                return "A booking screenshot."

        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            photo = Path(tmp) / "p.jpg"
            photo.write_bytes(b"x")
            settings = dataclasses.replace(gateway.settings, inbox_path=inbox,
                                           category_registry_path=inbox / ".openclaw" / "c.json")
            with patch.object(gateway, "settings", settings), patch.object(gateway, "vision", FakeVision()), \
                    patch.object(gateway, "send_html", lambda chat_id, html: sent.append((chat_id, html))):
                from openclaw_runtime import categories

                entry = categories.upsert_registry_entry(settings, "trip")
                gateway.ingest_image_into_category(5, photo, entry, original_name="ticket.jpg")
        self.assertEqual(sent[0][0], 5)
        self.assertIn("ticket.jpg → #trip", sent[0][1])
        self.assertIn("Booking ref ABC123", sent[0][1])


class ScannedPdfTest(unittest.TestCase):
    def test_blank_pages_are_read_from_the_image_and_cached(self) -> None:
        from pypdf import PdfWriter

        from openclaw_runtime import file_ingest
        from tests.support import build_settings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "scan.pdf"
            writer = PdfWriter()
            for _ in range(3):
                writer.add_blank_page(width=200, height=200)
            with pdf.open("wb") as handle:
                writer.write(handle)
            settings = build_settings(watcher_state_path=root / "state" / "watcher.json", pdf_ocr_max_pages=2)
            reads = []

            def reader(image):
                reads.append(image.name)
                return f"page text {len(reads)}"

            def fake_render(path, index, target, scale=2.0):
                target.write_bytes(b"png")
                return target

            with patch.object(file_ingest, "render_pdf_page", fake_render):
                ingestor = file_ingest.InboxIngestor(settings, None, None, page_reader=reader)
                text = ingestor._read_pdf(pdf)
                self.assertIn("## Page 1 (read from the page image)\npage text 1", text)
                self.assertIn("## Page 2 (read from the page image)\npage text 2", text)
                self.assertIn("## Page 3\n", text)  # over the page limit: left as it was
                again = ingestor._read_pdf(pdf)
            self.assertEqual(len(reads), 2)  # the second pass came from the cache
            self.assertEqual(text, again)
            plain = file_ingest.InboxIngestor(settings, None, None)._read_pdf(pdf)
            self.assertNotIn("read from the page image", plain)


class NoVisionModelTest(unittest.TestCase):
    def test_photos_are_not_queued_for_categories_without_vision(self) -> None:
        sent, pickers = [], []
        settings = dataclasses.replace(gateway.settings, vision_enabled=False, category_rag_enabled=True)
        with patch.object(gateway, "settings", settings), \
                patch.object(gateway, "send_message", lambda c, t: sent.append(t)), \
                patch.object(gateway, "send_category_picker", lambda *a, **k: pickers.append(a)), \
                patch.object(gateway, "index_image_to_knowledge", lambda *a: pickers.append("kb")):
            gateway._route_image_to_category(5, Path("/x/a.jpg"), None, "")
            gateway._route_image_to_category(5, Path("/x/a.jpg"), "trip", "")
        self.assertEqual(pickers, [])
        self.assertNotIn(5, gateway.PENDING_CATEGORY)
        self.assertIn("no vision model", sent[-1])


if __name__ == "__main__":
    unittest.main()
