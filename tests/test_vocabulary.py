import dataclasses
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import openclaw_telegram_gateway as gateway
from openclaw_runtime.dictionary import DictionaryEntry
from openclaw_runtime.vocabulary import (
    LookupResult,
    list_word_list,
    lookup_word,
    parse_bare_lookup,
    parse_lookup_command,
    remove_from_word_list,
    render_lookup_card,
    render_word_list,
    save_to_word_list,
)
from tests.card_checks import assert_valid_telegram_html

ENTRY = DictionaryEntry(
    word="resilient",
    phonetic="rɪ'zɪliənt",
    translation_lines=["a. 有彈性的", "能復原的"],
    definition_lines=["a. recovering quickly"],
    tags=["IELTS", "Oxford 3000"],
    base_form="",
)


class FakeLlm:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.calls.append((prompt, schema_name))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _dictionary(entry):
    dictionary = MagicMock()
    dictionary.lookup.return_value = entry
    return dictionary


class ParseLookupCommandTest(unittest.TestCase):
    def test_word_only(self) -> None:
        self.assertEqual(parse_lookup_command("/w resilient"), ("resilient", ""))

    def test_word_and_sentence(self) -> None:
        self.assertEqual(
            parse_lookup_command("/w  resilient | She's very resilient. "),
            ("resilient", "She's very resilient."),
        )

    def test_empty(self) -> None:
        self.assertEqual(parse_lookup_command("/w"), ("", ""))


class ParseBareLookupTest(unittest.TestCase):
    def test_single_words_and_short_phrases_count(self) -> None:
        self.assertEqual(parse_bare_lookup("resilient"), ("resilient", ""))
        self.assertEqual(parse_bare_lookup("  bust   down the door "), ("bust down the door", ""))
        self.assertEqual(parse_bare_lookup("well-being"), ("well-being", ""))
        self.assertEqual(parse_bare_lookup("don't"), ("don't", ""))

    def test_word_with_sentence(self) -> None:
        self.assertEqual(
            parse_bare_lookup("resilient | She's remarkably resilient."),
            ("resilient", "She's remarkably resilient."),
        )

    def test_sentences_numbers_and_long_phrases_do_not_count(self) -> None:
        for text in [
            "What does resilient mean?",
            "I took a gamble on it.",
            "1. move on 2. bust down the door",
            "one two three four five",
            "你好",
            "resilient, robust",
            "",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(parse_bare_lookup(text))

    def test_everyday_chat_replies_do_not_count(self) -> None:
        for text in ["thanks", "OK", "Thank you", "yes", "Good morning"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_bare_lookup(text))


class LookupWordTest(unittest.TestCase):
    def test_dictionary_hit_adds_sense_and_example_from_llm(self) -> None:
        llm = FakeLlm([json.dumps({"sense_zh": "能很快振作", "example": "Kids are remarkably resilient."})])
        result = lookup_word(_dictionary(ENTRY), llm, "resilient", "She's very resilient.")
        self.assertEqual(result.source, "dictionary")
        self.assertEqual(result.meaning_lines, ["a. 有彈性的", "能復原的"])
        self.assertEqual(result.sense_zh, "能很快振作")
        self.assertEqual(result.example, "Kids are remarkably resilient.")
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "word_sense")
        self.assertIn("She's very resilient.", prompt)
        self.assertIn("有彈性的", prompt)

    def test_without_sentence_asks_for_empty_sense(self) -> None:
        llm = FakeLlm([json.dumps({"sense_zh": "", "example": "Ex."})])
        lookup_word(_dictionary(ENTRY), llm, "resilient")
        self.assertIn("leave sense_zh as an empty string", llm.calls[0][0])

    def test_llm_failure_still_returns_the_dictionary_meaning(self) -> None:
        llm = FakeLlm([RuntimeError("model down")])
        result = lookup_word(_dictionary(ENTRY), llm, "resilient")
        self.assertEqual(result.meaning_lines, ["a. 有彈性的", "能復原的"])
        self.assertEqual(result.example, "")

    def test_falls_back_to_english_definition_when_no_chinese(self) -> None:
        entry = dataclasses.replace(ENTRY, translation_lines=[])
        llm = FakeLlm([json.dumps({"sense_zh": "", "example": "Ex."})])
        self.assertEqual(lookup_word(_dictionary(entry), llm, "resilient").meaning_lines, ["a. recovering quickly"])

    def test_not_in_dictionary_uses_labelled_ai_explanation(self) -> None:
        llm = FakeLlm(
            [json.dumps({"is_english": True, "phonetic": "", "meaning_zh": "phr. 放鬆一下",
                         "sense_zh": "", "example": "Let's chill out."})]
        )
        result = lookup_word(_dictionary(None), llm, "chill out")
        self.assertEqual(result.source, "ai")
        self.assertEqual(result.meaning_lines, ["phr. 放鬆一下"])
        self.assertEqual(llm.calls[0][1], "word_ai_entry")

    def test_non_word_returns_none(self) -> None:
        llm = FakeLlm([json.dumps({"is_english": False, "phonetic": "", "meaning_zh": "",
                                   "sense_zh": "", "example": ""})])
        self.assertIsNone(lookup_word(_dictionary(None), llm, "asdfgh"))


RESULT = LookupResult(
    word="resilient", phonetic="rɪ'zɪliənt", meaning_lines=["a. 有彈性的"], tags=["IELTS"],
    base_form="", sense_zh="", example="Kids are resilient.", source="dictionary",
)
NOW = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)


class SaveToWordListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.qdrant = MagicMock()
        self.embeddings = MagicMock()
        self.embeddings.embed.return_value = [0.1]

    def test_new_word_is_written_owner_scoped_with_review_fields(self) -> None:
        self.qdrant.scroll_by_filters.return_value = []
        count = save_to_word_list(self.qdrant, self.embeddings, "coll", "owner-a", RESULT, "She's resilient.", now=NOW)
        self.assertEqual(count, 1)
        filters = self.qdrant.scroll_by_filters.call_args.args[1]
        self.assertEqual(filters, {"owner": "owner-a", "tag": "eng_vocab", "kind": "vocab", "word": "resilient"})
        payload = self.qdrant.upsert_text.call_args.args[3]
        self.assertEqual(payload["owner"], "owner-a")
        self.assertEqual(payload["meaning"], "a. 有彈性的")
        self.assertEqual(payload["context_sentence"], "She's resilient.")
        self.assertEqual(payload["lookup_count"], 1)
        self.assertEqual(payload["review_box"], 0)
        self.assertEqual(payload["next_review"], "2026-09-28")

    def test_repeat_lookup_bumps_count_instead_of_duplicating(self) -> None:
        self.qdrant.scroll_by_filters.return_value = [{"id": "p1", "payload": {"lookup_count": 2}}]
        count = save_to_word_list(self.qdrant, self.embeddings, "coll", "owner-a", RESULT, now=NOW)
        self.assertEqual(count, 3)
        self.qdrant.upsert_text.assert_not_called()
        self.qdrant.set_payload.assert_called_once_with(
            "coll", "p1", {"lookup_count": 3, "last_lookup_at": NOW.isoformat()}
        )

    def test_owner_is_mandatory(self) -> None:
        with self.assertRaises(ValueError):
            save_to_word_list(self.qdrant, self.embeddings, "coll", "", RESULT, now=NOW)


class ListAndRemoveTest(unittest.TestCase):
    def test_list_is_newest_first(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [
            {"id": "a", "payload": {"word": "old", "added_at": "2026-09-01"}},
            {"id": "b", "payload": {"word": "new", "added_at": "2026-09-20"}},
        ]
        self.assertEqual([e["word"] for e in list_word_list(qdrant, "coll", "owner-a")], ["new", "old"])

    def test_remove_deletes_the_point(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"id": "p1", "payload": {}}]
        self.assertTrue(remove_from_word_list(qdrant, "coll", "owner-a", "Resilient"))
        qdrant.delete_points.assert_called_once_with("coll", ["p1"])
        self.assertEqual(qdrant.scroll_by_filters.call_args.args[1]["word"], "resilient")

    def test_remove_missing_word(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        self.assertFalse(remove_from_word_list(qdrant, "coll", "owner-a", "nope"))
        qdrant.delete_points.assert_not_called()


class RenderTest(unittest.TestCase):
    def test_lookup_card_first_time(self) -> None:
        html = render_lookup_card(dataclasses.replace(RESULT, sense_zh="能很快振作"), "She's <very> resilient.", 1)
        assert_valid_telegram_html(self, html)
        self.assertTrue(html.startswith("<b>查字｜resilient</b>"))
        self.assertIn("<b>resilient</b>  /rɪ'zɪliənt/", html)
        self.assertIn("<i>She's &lt;very&gt; resilient.</i>\n→ 能很快振作", html)
        self.assertIn("<b>Example</b>\n<i>Kids are resilient.</i>", html)
        self.assertIn("<b>Tags</b>　IELTS", html)
        self.assertIn("Added to your word list (looked up 1 time). Send /vocab rm resilient to undo.", html)

    def test_lookup_card_repeat_and_ai_label(self) -> None:
        html = render_lookup_card(dataclasses.replace(RESULT, source="ai", tags=[]), "", 3)
        assert_valid_telegram_html(self, html)
        self.assertIn("AI explanation", html)
        self.assertIn("looked up 3 times", html)
        self.assertNotIn("In your sentence", html)
        self.assertNotIn("Tags", html)

    def test_word_list(self) -> None:
        html = render_word_list(
            [{"display_word": "resilient", "meaning": "a. 有彈性的", "lookup_count": 2}, {"word": "run"}]
        )
        assert_valid_telegram_html(self, html)
        self.assertIn("<b>生字本</b> · 2 words", html)
        self.assertIn("1. <b>resilient</b> — a. 有彈性的 (looked up 2×)", html)
        self.assertIn("2. <b>run</b> — ", html)

    def test_empty_word_list(self) -> None:
        self.assertIn("empty", render_word_list([]))


class GatewayBareWordLookupTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(gateway, "settings", dataclasses.replace(gateway.settings, dictionary_enabled=True))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bare_word_starts_a_lookup(self) -> None:
        with patch.object(gateway.threading, "Thread") as thread, \
                patch.object(gateway, "has_pending_category", lambda chat_id: False):
            self.assertTrue(gateway.handle_bare_word_lookup(1, {"text": "resilient | She is resilient."}))
        self.assertEqual(thread.call_args.kwargs["args"], (1, "resilient", "She is resilient."))

    def test_non_words_and_commands_are_left_alone(self) -> None:
        for text in ["How are you today?", "thanks", "/w resilient", ""]:
            with self.subTest(text=text):
                self.assertFalse(gateway.handle_bare_word_lookup(1, {"text": text}))
        self.assertFalse(gateway.handle_bare_word_lookup(1, {"voice": {"file_id": "f"}}))

    def test_waiting_category_name_is_not_a_lookup(self) -> None:
        with patch.object(gateway, "has_pending_category", lambda chat_id: True):
            self.assertFalse(gateway.handle_bare_word_lookup(1, {"text": "Work"}))

    def test_disabled_without_the_dictionary(self) -> None:
        with patch.object(gateway, "settings", dataclasses.replace(gateway.settings, dictionary_enabled=False)):
            self.assertFalse(gateway.handle_bare_word_lookup(1, {"text": "resilient"}))

    def test_bare_word_wins_over_an_open_daily_task_but_not_an_open_review(self) -> None:
        calls = []
        message = {"chat": {"id": 1}, "text": "resilient"}
        with patch.object(gateway, "handle_vocab_review_answer", lambda chat_id, msg: calls.append("review")), \
                patch.object(gateway, "handle_bare_word_lookup", lambda chat_id, msg: calls.append("lookup") or True), \
                patch.object(gateway, "handle_english_bot_pending_reply", lambda chat_id, msg: calls.append("task")):
            gateway.handle_message(message)
        self.assertEqual(calls, ["review", "lookup"])


class GatewayVocabularyCommandTest(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        self.assertFalse(gateway.handle_vocabulary_command(1, "/w resilient"))
        self.assertNotIn("/vocab", gateway.help_text())

    def test_enabled_routes_commands(self) -> None:
        settings = dataclasses.replace(gateway.settings, dictionary_enabled=True)
        sent = []
        with patch.object(gateway, "settings", settings), \
                patch.object(gateway, "send_message", lambda chat_id, text: sent.append(text)), \
                patch.object(gateway.threading, "Thread") as thread:
            self.assertTrue(gateway.handle_vocabulary_command(1, "/w resilient | Ok."))
            thread.assert_called_once()
            self.assertEqual(thread.call_args.kwargs["args"], (1, "resilient", "Ok."))
            self.assertTrue(gateway.handle_vocabulary_command(1, "/w"))
            self.assertTrue(gateway.handle_vocabulary_command(1, "/vocab rm"))
            self.assertFalse(gateway.handle_vocabulary_command(1, "/weather"))
            self.assertIn("/vocab", gateway.help_text())
        self.assertIn("Usage: /w <word>", sent[0])
        self.assertEqual(sent[1], "Usage: /vocab rm <word>")


if __name__ == "__main__":
    unittest.main()
