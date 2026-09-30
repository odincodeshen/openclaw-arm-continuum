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
                patch.object(service, "synthesize", side_effect=lambda text, accent, fmt="ogg": f"{accent}:{text}".encode()) as synth:
            self.assertEqual(service.speak("resilient", "uk"), b"uk:resilient")
            self.assertEqual(service.speak("resilient", "uk"), b"uk:resilient")
            self.assertEqual(service.speak("resilient", "us"), b"us:resilient")
        self.assertEqual(synth.call_count, 2)
        self.assertNotEqual(service.cache_path("a", "uk"), service.cache_path("a", "us"))
        self.assertTrue(service.cache_path("a", "uk", "mp3").name.endswith(".mp3"))
        self.assertEqual(service.parse_format(b'{"text": "a", "format": "MP3"}'), "mp3")
        with self.assertRaises(service.BadRequest):
            service.parse_format(b'{"format": "wav"}')

    def test_unused_clips_are_pruned(self) -> None:
        import os
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, "CACHE_DIR", Path(tmp)):
            old, new = Path(tmp) / "uk" / "old.ogg", Path(tmp) / "uk" / "new.ogg"
            old.parent.mkdir()
            old.write_bytes(b"x")
            new.write_bytes(b"x")
            os.utime(old, (0, 0))
            self.assertEqual(service.prune_cache(), 1)
            self.assertEqual([p.name for p in old.parent.iterdir()], ["new.ogg"])

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
        with patch.object(gateway.tts, "speak", lambda text, accent, fmt="ogg": b"OggS" + text.encode()) as _:
            self._tap(rows[1][1]["callback_data"])
        self.assertEqual(self.voices, [(5, "US · She is resilient.", True)])
        folder = Path(self.tmp.name) / ".openclaw" / "tts"
        self.assertEqual(list(folder.glob("*.ogg")), [])

    def test_buttons_still_work_after_a_restart(self) -> None:
        rows = gateway.pronunciation_rows("resilient")
        with gateway.TTS_TEXTS_LOCK:
            gateway.TTS_TEXTS.clear()  # a restart empties memory; the file remains
        with patch.object(gateway.tts, "speak", lambda text, accent, fmt="ogg": b"OggS"):
            self._tap(rows[0][0]["callback_data"])
        self.assertEqual(self.voices, [(5, "UK · resilient", True)])

    def test_expired_button_and_service_down(self) -> None:
        self._tap("tts:ffffffffffff:uk")
        self.assertIn(("answerCallbackQuery", {"callback_query_id": "q",
                                               "text": "This button has expired -- look the word up again."}), self.calls)
        rows = gateway.pronunciation_rows("resilient")
        sent = []

        def down(text, accent, fmt="ogg"):
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


class AnkiPackageTest(unittest.TestCase):
    def test_package_layout_notes_and_media(self) -> None:
        import sqlite3
        import zipfile

        from openclaw_runtime.anki_package import AnkiNote, write_apkg

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.apkg"
            count = write_apkg(path, "OpenClaw words", [
                AnkiNote("resilient", "rɪˈzɪliənt", "有彈性的", "She is resilient.", b"ID3uk", b"ID3us", ["openclaw_lookup"]),
                AnkiNote("Resilient", meaning="dup"),  # same word: one note
                AnkiNote("chill out", meaning="放鬆"),
            ])
            self.assertEqual(count, 2)
            with zipfile.ZipFile(path) as package:
                media = json.loads(package.read("media"))
                self.assertEqual(sorted(media.values())[0].endswith("_uk.mp3"), True)
                self.assertEqual(package.read("0"), b"ID3uk")
                (Path(tmp) / "c.anki2").write_bytes(package.read("collection.anki2"))
            con = sqlite3.connect(Path(tmp) / "c.anki2")
            flds = [row[0].split("\x1f") for row in con.execute("select flds from notes order by id")]
            self.assertEqual(con.execute("select count(*) from cards").fetchone()[0], 2)
            self.assertIn(["resilient", "rɪˈzɪliənt", "有彈性的", "She is resilient."], [f[:4] for f in flds])
            self.assertTrue(any(f[4].startswith("[sound:openclaw_") for f in flds))
            con.close()

    def test_export_builds_notes_with_audio_and_survives_failures(self) -> None:
        def speak(text, accent):
            if accent == "us":
                raise OSError("down")
            return b"ID3"

        entries = [{"word": "resilient", "display_word": "resilient", "meaning": "a\nb", "source": "weekly_chunk"},
                   {"word": ""}]
        notes = gateway.build_anki_notes(entries, speak=speak)
        self.assertEqual(len(notes), 1)
        self.assertEqual((notes[0].audio_uk, notes[0].audio_us), (b"ID3", None))
        self.assertEqual(notes[0].meaning, "a<br>b")
        self.assertEqual(notes[0].tags, ["openclaw_chunk"])


class SayPracticeTest(unittest.TestCase):
    def test_matching(self) -> None:
        from openclaw_runtime.vocabulary import pronunciation_matches

        self.assertTrue(pronunciation_matches("resilient", "Resilient."))
        self.assertTrue(pronunciation_matches("resilient", "resiliant"))  # one letter off, long word
        self.assertFalse(pronunciation_matches("resilient", "resident"))
        self.assertTrue(pronunciation_matches("chill out", "OK, chill out!"))
        self.assertFalse(pronunciation_matches("chill out", "out chill"))
        self.assertFalse(pronunciation_matches("cat", "cut"))
        self.assertFalse(pronunciation_matches("resilient", ""))

    def test_voice_after_say_is_checked(self) -> None:
        sent, cards = [], []
        settings = dataclasses.replace(gateway.settings, dictionary_enabled=True, tts_enabled=True)

        class InlineThread:
            def __init__(inner, target=None, args=(), daemon=None):
                inner.target, inner.args = target, args

            def start(inner):
                inner.target(*inner.args)

        def fake_download(file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
            return target, 1

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(gateway, "settings", dataclasses.replace(settings, inbox_path=Path(tmp) / "inbox")), \
                patch.object(gateway, "send_card_with_buttons", lambda c, html, rows: cards.append((html, rows))), \
                patch.object(gateway, "send_message", lambda c, t: sent.append(t)), \
                patch.object(gateway, "telegram_file_info", lambda f: {"file_path": "v.oga"}), \
                patch.object(gateway, "download_telegram_file", fake_download), \
                patch.object(gateway.transcriber, "transcribe", lambda path: "resident"), \
                patch.object(gateway.threading, "Thread", InlineThread):
            gateway.handle_vocabulary_command(5, "/say resilient")
            self.assertIn("saying: <b>resilient</b>", cards[-1][0])
            self.assertFalse(gateway.handle_say_reply(5, {"text": "typing doesn't count"}))
            self.assertTrue(gateway.handle_say_reply(5, {"voice": {"file_id": "f", "duration": 1}}))
            result, rows = cards[-1]
            self.assertIn("❌ Not quite", result)
            self.assertIn("Heard: “resident”", result)
            self.assertEqual(rows[0][0]["text"], "Try again")
            self.assertFalse(gateway.handle_say_reply(5, {"voice": {"file_id": "f"}}))  # one attempt per /say
            gateway.handle_callback_query({"id": "q", "data": rows[0][0]["callback_data"],
                                           "message": {"message_id": 1, "chat": {"id": 5}}})
            self.assertIn(5, gateway.SAY_PENDING)
        gateway.SAY_PENDING.clear()


class MorePronunciationTest(unittest.TestCase):
    def test_word_list_pronounce_mode_and_chunk_card(self) -> None:
        calls = []
        settings = dataclasses.replace(gateway.settings, dictionary_enabled=True, tts_enabled=True,
                                       english_bot_enabled=True, pending_state_path=None)
        chunks = [{"phrase": "move on", "context_sentence": "We had to move on."}, {"phrase": "chill out"}]
        with patch.object(gateway, "settings", settings), \
                patch.object(gateway, "telegram", lambda m, p=None, timeout=60: calls.append((m, p or {})) or {}), \
                patch.object(gateway, "list_word_list", lambda *a: [{"word": "resilient", "display_word": "resilient"}]), \
                patch.object(gateway, "count_due_words", lambda *a: 0), \
                patch.object(gateway, "read_this_week_chunks", lambda q, c, w: chunks), \
                patch.object(gateway, "read_this_week_payload", lambda q, c, w: {}):
            gateway.handle_vocabulary_command(5, "/vocab")
            first = calls[-1][1]["reply_markup"]["inline_keyboard"][0]
            self.assertEqual([b["text"] for b in first], ["Remove words…", "🔊 Pronounce…"])
            gateway.handle_callback_query({"id": "q", "data": "vw:say", "message": {"message_id": 2, "chat": {"id": 5}}})
            rows = calls[-1][1]["reply_markup"]["inline_keyboard"]
            self.assertEqual(rows[0][0]["text"], "🔊 resilient")
            self.assertTrue(rows[0][0]["callback_data"].startswith("tts:"))
            gateway._english_bot_set_pending_answer("5", {"kind": "eng_fri", "week_number": 3})
            card = [p for m, p in calls if m == "sendMessage"][-1]
            self.assertIn("【語塊發音】", card["text"])
            labels = [[b["text"] for b in row] for row in card["reply_markup"]["inline_keyboard"]]
            self.assertEqual(labels, [["🔊 UK · move on", "🔊 US"], ["🔊 UK example", "🔊 US example"],
                                      ["🔊 UK · chill out", "🔊 US"]])
            calls.clear()
            gateway._english_bot_set_pending_answer("5", {"kind": "eng_sat", "week_number": 3})
            self.assertEqual([p for m, p in calls if m == "sendMessage"], [])  # never on cloze day
        gateway.PENDING_ANSWER.pop(5, None)
