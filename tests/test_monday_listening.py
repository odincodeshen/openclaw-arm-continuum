import json
import unittest
from unittest.mock import MagicMock

from openclaw_runtime.english_weekly import render_weekly_recap
from openclaw_runtime.skills.english_bot import (
    Chunk,
    WeeklyContent,
    build_monday_message,
    choose_dictation,
    dictation_word_correct,
    evaluate_monday_reply,
)
from openclaw_runtime.transcription_client import TranscriptSegment
from tests.card_checks import assert_feedback_card, assert_task_card

SEGMENTS = [
    TranscriptSegment(0.0, 5.0, "Right."),
    TranscriptSegment(5.0, 20.0, "I was nineteen years old when I moved to London with my brother, and it changed everything."),
    TranscriptSegment(20.0, 30.0, "So what happened next?"),
    TranscriptSegment(200.0, 220.0, "This segment is outside the stretch and has plenty of candidate words inside it."),
]


class ChooseDictationTest(unittest.TestCase):
    def test_three_spread_content_words_from_the_stretch(self) -> None:
        prompt, answers = choose_dictation(SEGMENTS, 0.0, 60.0)
        self.assertEqual(answers, ["nineteen", "London", "everything"])
        self.assertEqual(prompt, "I was ____ years old when I moved to ____ with my brother, and it changed ____.")

    def test_words_from_this_weeks_chunks_are_never_blanked(self) -> None:
        # "move on" is on the same card: "moved" stays, other words are picked
        prompt, answers = choose_dictation(SEGMENTS, 0.0, 60.0, avoid_words={"london"})
        self.assertNotIn("London", answers)
        self.assertIn("London", prompt)

    def test_none_when_nothing_suitable_in_the_stretch(self) -> None:
        self.assertIsNone(choose_dictation([TranscriptSegment(0.0, 5.0, "Yes, right, I see.")], 0.0, 60.0))


class DictationWordCorrectTest(unittest.TestCase):
    def test_case_punctuation_and_one_typo_on_long_words(self) -> None:
        self.assertTrue(dictation_word_correct("London,", "London"))
        self.assertTrue(dictation_word_correct("NINETEEN", "nineteen"))
        self.assertTrue(dictation_word_correct("everthing", "everything"))  # one letter missing
        self.assertFalse(dictation_word_correct("bat", "cat"))  # short words must be exact
        self.assertFalse(dictation_word_correct("", "London"))
        self.assertFalse(dictation_word_correct("Paris", "London"))


def _content(listening: bool) -> WeeklyContent:
    return WeeklyContent(
        week_number=3, episode_title="Guest", episode_guid="g", segment_start=180.0, segment_end=360.0,
        transcript_excerpt="x", chunks=[Chunk("move on", "繼續前進", "We had to move on.")], window_segments=[],
        episode_link="https://www.bbc.co.uk/programmes/x", listening_check=listening,
        dictation_prompt="I moved to ____ with my ____." if listening else "",
        dictation_answers=["London", "brother"] if listening else [],
    )


class MondayCardTest(unittest.TestCase):
    def test_three_numbered_parts_when_the_week_has_a_listening_check(self) -> None:
        html = build_monday_message(_content(True))
        assert_task_card(self, html, "mon")
        body = html.split("<b>Today</b>")[1]
        self.assertLess(body.index("<b>1. Gist</b>"), body.index("<b>2. Dictation</b>"))
        self.assertLess(body.index("<b>2. Dictation</b>"), body.index("<b>3. Chunk</b>"))
        self.assertIn("Fill the 2 gaps", html)
        self.assertIn("<i>I moved to ____ with my ____.</i>", html)
        self.assertIn("One message, by number", html)
        self.assertNotIn("London", html)  # never leak the answers

    def test_older_weeks_keep_the_chunk_only_card(self) -> None:
        html = build_monday_message(_content(False))
        self.assertNotIn("Gist", html)
        self.assertNotIn("Dictation", html)


def _qdrant(payload: dict):
    qdrant = MagicMock()

    def scroll(collection, filters, limit=64, **kw):
        if filters.get("kind") == "weekly_content":
            return [{"id": "w", "payload": payload}]
        if filters.get("kind") == "daily_task":
            return [{"id": "t", "payload": {"completed": False}}]
        return []

    qdrant.scroll_by_filters.side_effect = scroll
    return qdrant


class FakeLlm:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.calls.append((prompt, schema_name))
        return json.dumps(self.response)


LISTENING_PAYLOAD = {
    "listening_check": True,
    "transcript_excerpt": "I moved to London with my brother.",
    "dictation_prompt": "I moved to ____ with my ____.",
    "dictation_answers": ["London", "brother"],
}


class EvaluateMondayListeningTest(unittest.TestCase):
    def _run(self, response: dict, payload: dict = LISTENING_PAYLOAD):
        qdrant = _qdrant(payload)
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        llm = FakeLlm(response)
        html = evaluate_monday_reply(
            qdrant, embeddings, llm, "coll", "owner-a", 3, [{"phrase": "move on"}], "1. moving 2. lodon, sister 3. ..."
        )
        return html, qdrant, llm

    def test_grades_all_three_parts_and_reveals_the_transcript(self) -> None:
        html, qdrant, llm = self._run(
            {
                "gist_correct": True, "gist_feedback": "Yes, it's about moving.", "gist_model": "He moved to London.",
                "dictation_words": ["lodon", "sister"],
                "chunk_results": [{"phrase": "move on", "used_correctly": True, "user_sentence": "I moved on."}],
                "model_answer": "We had to move on.",
            }
        )
        assert_feedback_card(self, html, "mon")
        self.assertIn("✅ Gist — Yes, it's about moving.", html)
        self.assertIn("Dictation 1/2", html)
        self.assertIn('✅ 1. London', html)  # "lodon" is one typo away
        self.assertIn('❌ 2. "sister" → brother', html)
        self.assertIn("✅ move on — used correctly", html)
        self.assertIn("What was said:\nI moved to London with my brother.", html)
        self.assertEqual(llm.calls[0][1], "monday_listening")
        self.assertNotIn("London", llm.calls[0][0].split("Dictation sentence")[1].split("This week")[0])
        record = [c for c in qdrant.upsert_text.call_args_list if c.args[3].get("kind") == "listening_check"][0]
        self.assertEqual(
            {k: record.args[3][k] for k in ("owner", "gist_correct", "dictation_correct", "dictation_total")},
            {"owner": "owner-a", "gist_correct": True, "dictation_correct": 1, "dictation_total": 2},
        )

    def test_blank_gaps_and_no_chunk(self) -> None:
        html, _, _ = self._run(
            {
                "gist_correct": False, "gist_feedback": "Not quite.", "gist_model": "m",
                "dictation_words": [], "chunk_results": [{"phrase": "move on", "used_correctly": False, "user_sentence": ""}],
                "model_answer": "",
            }
        )
        self.assertIn("❌ Gist — Not quite.", html)
        self.assertIn("Dictation 0/2", html)
        self.assertIn("❌ 1. (blank) → London", html)
        self.assertIn("❌ No chunk from this week spotted", html)

    def test_week_without_listening_check_uses_the_chunk_only_path(self) -> None:
        qdrant = _qdrant({"listening_check": False})
        llm = MagicMock()
        llm.chat_json.return_value = json.dumps(
            {"chunk_results": [{"phrase": "move on", "used_correctly": True, "user_sentence": "x"}], "model_answer": "m"}
        )
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        html = evaluate_monday_reply(qdrant, embeddings, llm, "coll", "owner-a", 2, [{"phrase": "move on"}], "reply")
        self.assertNotIn("Gist", html)
        self.assertEqual(llm.chat_json.call_args.kwargs["schema_name"], "friday_chunk_usage")


class RecapListeningLineTest(unittest.TestCase):
    def test_score_or_not_answered(self) -> None:
        html = render_weekly_recap(3, {}, [], promoted=[], listening_expected=True,
                                   listening={"gist_correct": True, "dictation_correct": 2, "dictation_total": 3})
        self.assertIn("<b>Listening</b>\nGist ✅ · Dictation 2/3", html)
        html = render_weekly_recap(3, {}, [], promoted=[], listening_expected=True, listening=None)
        self.assertIn("<b>Listening</b>\nNot answered this week", html)
        self.assertNotIn("Listening", render_weekly_recap(3, {}, [], promoted=[]))


if __name__ == "__main__":
    unittest.main()
