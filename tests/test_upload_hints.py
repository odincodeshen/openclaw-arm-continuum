import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openclaw_telegram_gateway as gateway
from openclaw_runtime import categories
from openclaw_runtime.upload_hints import find_duplicate, looks_like_text, pdf_preview_text, preview_text, suggest_category

FIXTURE = Path(__file__).resolve().parent / "openclaw-rag-fixture.pdf"


class FakeLlm:
    def __init__(self, choice):
        self.choice = choice
        self.prompts = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.prompts.append((prompt, schema))
        return json.dumps({"category": self.choice})


class PreviewTest(unittest.TestCase):
    def test_pdf_text_and_garbage(self) -> None:
        self.assertIn("OpenClaw RAG stabilisation", pdf_preview_text(FIXTURE))
        self.assertFalse(looks_like_text("!#$%& ! ! ! ! ! #$%& ( )*+ .-& ! /0-% 1#,2 3 4- 3% 5"))
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "a.md"
            note.write_text("# Title\nbody", encoding="utf-8")
            self.assertEqual(preview_text(note), "# Title\nbody")
            self.assertEqual(preview_text(Path(tmp) / "x.docx"), "")


class SuggestTest(unittest.TestCase):
    def test_choice_limited_to_existing_names(self) -> None:
        cats = {"aitool": ["Claude-guide.pdf"], "mindset": []}
        llm = FakeLlm("aitool")
        self.assertEqual(suggest_category(llm, "cursor-tips.pdf", "Cursor tips", cats), "aitool")
        prompt, schema = llm.prompts[0]
        self.assertIn("- aitool: Claude-guide.pdf", prompt)
        self.assertEqual(schema["properties"]["category"]["enum"], ["aitool", "mindset", "none"])
        self.assertIsNone(suggest_category(FakeLlm("none"), "x", "", cats))
        self.assertIsNone(suggest_category(FakeLlm("aitool"), "x", "", {"aitool": []}))  # one category: no hint


class DuplicateTest(unittest.TestCase):
    def test_same_bytes_found_anywhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cat").mkdir()
            (root / "cat" / "old.pdf").write_bytes(b"%PDF same")
            (root / "cat" / "old.pdf.meta.json").write_bytes(b"%PDF same")  # sidecars are ignored
            new = root / "new.pdf"
            new.write_bytes(b"%PDF same")
            self.assertEqual(find_duplicate(new, {"#aitool": root / "cat", "kb": root / "missing"}), ("#aitool", "old.pdf"))
            new.write_bytes(b"%PDF diff!")
            self.assertIsNone(find_duplicate(new, {"#aitool": root / "cat"}))


class GatewayUploadHintsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        inbox = Path(self.tmp.name) / "inbox"
        inbox.mkdir()
        self.settings = dataclasses.replace(
            gateway.settings, inbox_path=inbox, category_rag_enabled=True, pending_state_path=None,
            category_registry_path=inbox / ".openclaw" / "categories.json", telegram_allowed_chat_ids=set())
        for target, name, value in [(gateway, "settings", self.settings)]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.calls = []
        p = patch.object(gateway, "telegram", lambda m, pl=None, timeout=60: self.calls.append((m, pl or {})) or
                         {"result": {"message_id": 40}})
        p.start()
        self.addCleanup(p.stop)
        gateway.PENDING_CATEGORY.clear()
        self.addCleanup(gateway.PENDING_CATEGORY.clear)
        self.aitool = categories.upsert_registry_entry(self.settings, "aitool")
        categories.upsert_registry_entry(self.settings, "mindset")
        folder = gateway.category_dir(self.aitool["slug"])
        folder.mkdir(parents=True)
        (folder / "20260928-Life.pdf").write_bytes(b"%PDF same")

    def _stage(self, data: bytes) -> Path:
        staged = gateway.category_staging_dir() / "Life.pdf"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(data)
        gateway.set_pending_category(5, {"path": str(staged), "kind": "document", "note": "", "original_name": "Life.pdf"})
        return staged

    def test_duplicate_upload_offers_skip_and_skip_removes_the_copy(self) -> None:
        staged = self._stage(b"%PDF same")
        dup = find_duplicate(staged, gateway.duplicate_places())
        self.assertEqual(dup, ("#aitool", "20260928-Life.pdf"))
        markup = gateway.category_picker_markup(duplicate=True)
        self.assertIn({"text": "Skip — keep the saved copy", "callback_data": "cat_dupskip"},
                      [b for row in markup["inline_keyboard"] for b in row])
        gateway.handle_callback_query({"id": "q", "data": "cat_dupskip",
                                       "message": {"message_id": 40, "chat": {"id": 5}}})
        self.assertFalse(staged.exists())
        self.assertNotIn(5, gateway.PENDING_CATEGORY)

    def test_suggestion_moves_the_category_to_the_top(self) -> None:
        staged = self._stage(b"%PDF other")
        gateway.PENDING_CATEGORY[5]["prompt_ids"] = [40]
        with patch.object(gateway, "llm", FakeLlm("mindset")):
            gateway._suggest_for_picker(5, 40, staged, "Life.pdf", False)
        method, payload = self.calls[-1]
        self.assertEqual(method, "editMessageReplyMarkup")
        first = payload["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(first["text"], "⭐ #mindset (suggested)")
        gateway.PENDING_CATEGORY[5]["prompt_ids"] = []  # answered meanwhile
        count = len(self.calls)
        with patch.object(gateway, "llm", FakeLlm("mindset")):
            gateway._suggest_for_picker(5, 40, staged, "Life.pdf", False)
        self.assertEqual(len(self.calls), count)


if __name__ == "__main__":
    unittest.main()
