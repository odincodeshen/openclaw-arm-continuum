import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openclaw_telegram_gateway as gateway
import openclaw_tts_service as service


class TtsServiceTest(unittest.TestCase):
    def test_request_validation(self) -> None:
        self.assertEqual(service.parse_request(b'{"text": "  resilient \\n", "accent": "US"}'), ("resilient", "us"))
        self.assertEqual(service.parse_request(b'{"text": "hi"}'), ("hi", "uk"))
        for body in (b"not json", b'{"text": ""}', b'{"text": "x", "accent": "au"}',
                     json.dumps({"text": "x" * 301}).encode()):
            with self.assertRaises(service.BadRequest):
                service.parse_request(body)

    def test_each_text_and_accent_is_synthesized_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, "CACHE_DIR", Path(tmp)), \
                patch.object(service, "synthesize", side_effect=lambda text, accent: f"{accent}:{text}".encode()) as synth:
            self.assertEqual(service.speak("resilient", "uk"), b"uk:resilient")
            self.assertEqual(service.speak("resilient", "uk"), b"uk:resilient")
            self.assertEqual(service.speak("resilient", "us"), b"us:resilient")
        self.assertEqual(synth.call_count, 2)
        self.assertNotEqual(service.cache_path("a", "uk"), service.cache_path("a", "us"))

    def test_pcm_becomes_ogg_opus(self) -> None:
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("PyAV not installed here")
        ogg = service.pcm_to_ogg(b"\x00\x00" * 24000)
        self.assertTrue(ogg.startswith(b"OggS"))


class GatewayPronunciationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(gateway, "settings", dataclasses.replace(
            gateway.settings, tts_enabled=True, dictionary_enabled=True, inbox_path=Path(self.tmp.name) / "inbox"))
        p.start()
        self.addCleanup(p.stop)
        self.calls: list[tuple[str, dict]] = []
        self.voices: list[tuple[int, str, bool]] = []

        class InlineThread:
            def __init__(inner, target=None, args=(), daemon=None):
                inner.target, inner.args = target, args

            def start(inner):
                inner.target(*inner.args)

        def fake_voice(chat_id, path, caption=""):
            self.voices.append((chat_id, caption, Path(path).exists()))

        patches = [patch.object(gateway, "telegram", lambda m, p=None, timeout=60: self.calls.append((m, p or {})) or {}),
                   patch.object(gateway, "send_voice_file", fake_voice),
                   patch.object(gateway.threading, "Thread", InlineThread)]
        for q in patches:
            q.start()
            self.addCleanup(q.stop)

    def _tap(self, data: str) -> None:
        gateway.handle_callback_query({"id": "q", "data": data, "message": {"message_id": 1, "chat": {"id": 5}}})

    def test_rows_for_word_and_sentence_only_when_enabled(self) -> None:
        rows = gateway.pronunciation_rows("resilient", "She is resilient.")
        self.assertEqual([[b["text"] for b in row] for row in rows],
                         [["🔊 UK", "🔊 US"], ["🔊 UK sentence", "🔊 US sentence"]])
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for row in rows for b in row))
        self.assertEqual(len(gateway.pronunciation_rows("resilient")), 1)
        with patch.object(gateway, "settings", dataclasses.replace(gateway.settings, tts_enabled=False)):
            self.assertEqual(gateway.pronunciation_rows("resilient", "x"), [])

    def test_tap_sends_a_voice_message_and_cleans_up(self) -> None:
        rows = gateway.pronunciation_rows("resilient", "She is resilient.")
        with patch.object(gateway.tts, "speak", lambda text, accent: b"OggS" + text.encode()) as _:
            self._tap(rows[1][1]["callback_data"])
        self.assertEqual(self.voices, [(5, "US · She is resilient.", True)])
        folder = Path(self.tmp.name) / ".openclaw" / "tts"
        self.assertEqual(list(folder.glob("*.ogg")), [])

    def test_buttons_still_work_after_a_restart(self) -> None:
        rows = gateway.pronunciation_rows("resilient")
        with gateway.TTS_TEXTS_LOCK:
            gateway.TTS_TEXTS.clear()  # a restart empties memory; the file remains
        with patch.object(gateway.tts, "speak", lambda text, accent: b"OggS"):
            self._tap(rows[0][0]["callback_data"])
        self.assertEqual(self.voices, [(5, "UK · resilient", True)])

    def test_expired_button_and_service_down(self) -> None:
        self._tap("tts:ffffffffffff:uk")
        self.assertIn(("answerCallbackQuery", {"callback_query_id": "q",
                                               "text": "This button has expired -- look the word up again."}), self.calls)
        rows = gateway.pronunciation_rows("resilient")
        sent = []

        def down(text, accent):
            raise OSError("connection refused")

        with patch.object(gateway.tts, "speak", down), \
                patch.object(gateway, "send_message", lambda chat_id, text: sent.append(text)):
            self._tap(rows[0][0]["callback_data"])
        self.assertIn("Couldn't make the pronunciation audio", sent[0])
        self.assertEqual(self.voices, [])

    def test_lookup_card_gets_the_buttons(self) -> None:
        from openclaw_runtime.vocabulary import LookupResult

        result = LookupResult("resilient", "rɪˈzɪliənt", ["adj. 有彈性的"], [], "", "", "She is resilient.", "dictionary")
        with patch.object(gateway, "lookup_word", lambda *a: result), \
                patch.object(gateway, "save_to_word_list", lambda *a: 1):
            gateway._lookup_and_reply(5, "resilient", "")
        card = [p for m, p in self.calls if m == "sendMessage"][-1]
        self.assertIn("查字｜resilient", card["text"])
        self.assertEqual(len(card["reply_markup"]["inline_keyboard"]), 2)


if __name__ == "__main__":
    unittest.main()
