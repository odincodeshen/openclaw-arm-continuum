import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openclaw_telegram_gateway as gateway
from openclaw_runtime.skills import memory


class RagButtonsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        inbox = Path(self.tmp.name) / "inbox"
        inbox.mkdir()
        p = patch.object(gateway, "settings", dataclasses.replace(
            gateway.settings, category_rag_enabled=True, inbox_path=inbox, pending_state_path=None,
            category_registry_path=inbox / ".openclaw" / "categories.json", telegram_allowed_chat_ids=set()))
        p.start()
        self.addCleanup(p.stop)
        self.calls, self.html, self.ran = [], [], []

        class InlineThread:
            def __init__(inner, target=None, args=(), daemon=None):
                inner.target, inner.args = target, args

            def start(inner):
                inner.target(*inner.args)

        for name, value in [("telegram", lambda m, pl=None, timeout=60: self.calls.append((m, pl or {})) or
                              {"result": {"message_id": 3}}),
                            ("send_html", lambda c, h: self.html.append(h))]:
            q = patch.object(gateway, name, value)
            q.start()
            self.addCleanup(q.stop)
        q = patch.object(gateway.threading, "Thread", InlineThread)
        q.start()
        self.addCleanup(q.stop)
        self.addCleanup(gateway.RAG_FOLLOWUP.clear)
        self.addCleanup(gateway.PENDING_CATEGORY.clear)
        self.answer = "Phosphate cells last long.\n\nSources: cells.md"
        memory.RECENT_RAG_SOURCES[memory.answer_key(self.answer)] = [{"source": "cells.md", "text": "Lithium iron phosphate..."}]

    def _tap(self, data):
        gateway.handle_callback_query({"id": "q", "data": data, "message": {"message_id": 3, "chat": {"id": 5}}})

    def test_rows_only_for_questions(self) -> None:
        rows = gateway.rag_answer_rows(5, "/rag #batteries what chemistry?", self.answer)
        self.assertEqual([b["text"] for b in rows[0]], ["📄 Show sources", "↪ Follow-up", "💾 Save answer"])
        self.assertEqual(gateway.rag_answer_rows(5, "/rag digest week", self.answer), [])

    def test_sources_followup_and_save(self) -> None:
        rows = gateway.rag_answer_rows(5, "/rag #batteries what chemistry?", self.answer)
        src, ask, save = (b["callback_data"] for b in rows[0])
        self._tap(src)
        self.assertIn("<b>cells.md</b>\nLithium iron phosphate...", self.html[-1])

        self._tap(ask)
        with patch.object(gateway, "handle_text_message", lambda chat_id, text: self.ran.append(text)):
            self.assertTrue(gateway.handle_rag_followup(5, {"text": "how long exactly?"}))
            self.assertFalse(gateway.handle_rag_followup(5, {"text": "again"}))  # one follow-up per tap
        self.assertEqual(self.ran, ["/rag #batteries how long exactly? (follow-up to: what chemistry?)"])

        self._tap(save)
        pending = gateway.PENDING_CATEGORY[5]["items"][0]
        self.assertTrue(pending["original_name"].startswith("rag answer: #batteries what chemistry?"))
        self.assertIn("Phosphate cells last long.", Path(pending["path"]).read_text(encoding="utf-8"))

    def test_follow_up_query_keeps_filters(self) -> None:
        self.assertEqual(gateway.follow_up_query("/rag source:youtu.be #[Work Notes] pricing?", "and costs?"),
                         "/rag source:youtu.be #[Work Notes] and costs? (follow-up to: pricing?)")
        self.assertEqual(gateway.follow_up_query("/rag what is x", "why?"), "/rag why? (follow-up to: what is x)")

    def test_expired(self) -> None:
        self._tap("rag:src:nope")
        self.assertIn(("answerCallbackQuery", {"callback_query_id": "q",
                                               "text": "This answer has expired -- ask again with /rag."}), self.calls)


if __name__ == "__main__":
    unittest.main()
