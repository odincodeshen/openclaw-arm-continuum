import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from openclaw_runtime.skills.english_bot import (
    annotate_for_shadowing,
    build_tuesday_message,
    compute_wpm,
    evaluate_tuesday_reply,
    explain_tuesday_shadowing_in_chinese,
    read_this_week_payload,
    run_tuesday_task,
    select_longest_guest_stretch,
    word_level_diff,
)


class FakeLlm:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.calls.append((prompt, schema_name))
        return self.responses.pop(0)


class SelectLongestGuestStretchTest(unittest.TestCase):
    def test_parses_response_and_prompt_requires_no_interruption(self) -> None:
        llm = FakeLlm(
            [json.dumps({"start_seconds": 15.0, "end_seconds": 55.0, "stretch_text": "I remember it well..."})]
        )
        result = select_longest_guest_stretch(llm, "[10.0-15.0] Host: go on\n[15.0-55.0] I remember it well...")
        self.assertEqual(result["start_seconds"], 15.0)
        self.assertEqual(result["end_seconds"], 55.0)
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "longest_guest_stretch")
        self.assertIn("no host interruption", prompt.lower())
        self.assertNotIn("60 second", prompt.lower())


class AnnotateForShadowingTest(unittest.TestCase):
    def test_parses_annotated_text_and_flags_best_guess(self) -> None:
        llm = FakeLlm([json.dumps({"annotated_text": "I **remember** it / well"})])
        annotated = annotate_for_shadowing(llm, "I remember it well")
        self.assertEqual(annotated, "I **remember** it / well")
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "shadowing_annotation")
        self.assertIn("best-guess", prompt.lower())
        self.assertIn("not an analysis of the actual audio", prompt.lower())


class ExplainTuesdayShadowingInChineseTest(unittest.TestCase):
    def test_parses_translation_and_analysis_and_prompt_asks_for_traditional_chinese(self) -> None:
        llm = FakeLlm([json.dumps({"translation_zh": "我記得很清楚", "analysis_zh": "發音掌握得不錯"})])
        diff = word_level_diff("I remember it well", "I remember well")
        feedback = explain_tuesday_shadowing_in_chinese(llm, "I remember it well", "I remember well", diff, 90.0)
        self.assertEqual(feedback["translation_zh"], "我記得很清楚")
        self.assertEqual(feedback["analysis_zh"], "發音掌握得不錯")
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "tuesday_chinese_feedback")
        self.assertIn("繁體中文", prompt)
        self.assertIn("it", prompt)  # the missing word is in the prompt


class BuildTuesdayMessageTest(unittest.TestCase):
    def test_message_includes_annotation_and_instructions(self) -> None:
        message = build_tuesday_message("I **remember** it / well")
        self.assertIn("I **remember** it / well", message)
        self.assertIn("voice message", message.lower())

    def test_push_message_is_english_only_no_chinese(self) -> None:
        """Spec change: Chinese content moved from the push to the
        evaluation step -- the Tuesday push stays English + stress
        annotation only."""
        message = build_tuesday_message("I **remember** it / well")
        self.assertNotIn("中文", message)


class ReadThisWeekPayloadTest(unittest.TestCase):
    def test_returns_first_matching_point_payload(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"payload": {"week_number": 3, "phrase": "p1"}}]
        payload = read_this_week_payload(qdrant, "coll", 3)
        self.assertEqual(payload["phrase"], "p1")
        qdrant.scroll_by_filters.assert_called_once_with(
            "coll", {"kind": "weekly_content", "week_number": 3}, limit=4
        )

    def test_raises_when_monday_has_not_run(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        with self.assertRaises(ValueError):
            read_this_week_payload(qdrant, "coll", 3)


class RunTuesdayTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace_dir = Path(self.tmp.name) / "wk1"
        self.workspace_dir.mkdir(parents=True)
        (self.workspace_dir / "window.mp3").write_bytes(b"fake window mp3")

        window_segments_json = json.dumps(
            [
                {"start": 0.0, "end": 10.0, "text": "Host: so tell me about it"},
                {"start": 10.0, "end": 190.0, "text": "Guest: I remember it well and it changed everything"},
            ]
        )
        self.qdrant = MagicMock()
        self.qdrant.scroll_by_filters.return_value = [
            {
                "payload": {
                    "week_number": 1,
                    "segment_start": 190.0,  # INTRO_SKIP_SECONDS(180) + 10.0 window-relative
                    "segment_end": 370.0,  # 180 + 190.0
                    "window_segments_json": window_segments_json,
                }
            }
        ]

        self.embeddings = MagicMock()
        self.embeddings.embed.return_value = [0.1]

        self.clip_client = MagicMock()
        self.clip_client.clip.return_value = 40.0

        self.llm = FakeLlm(
            [
                json.dumps(
                    {
                        "start_seconds": 10.0,
                        "end_seconds": 50.0,
                        "stretch_text": "I remember it well and it changed everything",
                    }
                ),
                json.dumps({"annotated_text": "I **remember** it well / and it changed everything"}),
            ]
        )

        self.sent_messages: list[tuple[str, str]] = []
        self.sent_audio: list[tuple[str, Path, str]] = []

        def send_message(owner, text):
            self.sent_messages.append((owner, text))

        def send_audio(owner, path, caption):
            self.sent_audio.append((owner, path, caption))

        self.send_message = send_message
        self.send_audio = send_audio

    def test_reuses_window_mp3_and_sends_text_and_audio(self) -> None:
        task = run_tuesday_task(
            week_number=1,
            clip_client=self.clip_client,
            llm=self.llm,
            qdrant=self.qdrant,
            embeddings=self.embeddings,
            collection="coll",
            owners=["owner-a", "owner-b"],
            send_message=self.send_message,
            send_audio=self.send_audio,
            workspace_dir=self.workspace_dir,
        )

        self.assertEqual(task.annotated, "I **remember** it well / and it changed everything")
        self.assertEqual(task.stretch_text, "I remember it well and it changed everything")

        # clips from window.mp3 (not episode.mp3), using window-relative timestamps
        self.clip_client.clip.assert_called_once()
        args = self.clip_client.clip.call_args.args
        self.assertEqual(args[0], self.workspace_dir / "window.mp3")
        self.assertEqual(args[1], 10.0)
        self.assertEqual(args[2], 50.0)

        self.assertEqual(len(self.sent_messages), 2)
        self.assertEqual(len(self.sent_audio), 2)
        self.assertIn("remember", self.sent_messages[0][1])
        self.assertNotIn("中文", self.sent_messages[0][1])
        self.assertEqual(self.sent_audio[0][1], self.workspace_dir / "tuesday_clip.mp3")


class WordLevelDiffTest(unittest.TestCase):
    def test_exact_match_has_zero_error_rate(self) -> None:
        result = word_level_diff("the quick brown fox jumps", "the quick brown fox jumps")
        self.assertEqual(result.error_rate, 0.0)
        self.assertEqual(result.missing_words, [])
        self.assertEqual(result.extra_words, [])

    def test_missing_word_detected(self) -> None:
        result = word_level_diff("the quick brown fox jumps", "the quick fox jumps")
        self.assertEqual(result.missing_words, ["brown"])
        self.assertEqual(result.extra_words, [])
        self.assertAlmostEqual(result.error_rate, 0.2)

    def test_extra_word_detected(self) -> None:
        result = word_level_diff("the quick brown fox jumps", "the very quick brown fox jumps")
        self.assertEqual(result.missing_words, [])
        self.assertEqual(result.extra_words, ["very"])
        self.assertAlmostEqual(result.error_rate, 0.2)

    def test_substitution_counts_as_a_single_error_not_two(self) -> None:
        result = word_level_diff("the quick brown fox jumps", "the slow brown fox jumps")
        self.assertEqual(result.missing_words, ["quick"])
        self.assertEqual(result.extra_words, ["slow"])
        # 1 substitution / 5 reference words -- not 2/5, which a naive
        # delete+insert count would wrongly produce
        self.assertAlmostEqual(result.error_rate, 0.2)

    def test_case_and_punctuation_are_ignored(self) -> None:
        result = word_level_diff("The Quick, Brown fox!", "the quick brown fox")
        self.assertEqual(result.error_rate, 0.0)

    def test_completely_different_text_has_high_error_rate(self) -> None:
        result = word_level_diff("the quick brown fox", "completely unrelated words here")
        self.assertGreaterEqual(result.error_rate, 1.0)

    def test_empty_hypothesis_reports_all_missing(self) -> None:
        result = word_level_diff("the quick brown fox", "")
        self.assertEqual(result.missing_words, ["the", "quick", "brown", "fox"])
        self.assertEqual(result.error_rate, 1.0)

    def test_empty_reference_does_not_divide_by_zero(self) -> None:
        result = word_level_diff("", "hello there")
        self.assertEqual(result.reference_word_count, 0)
        self.assertGreater(result.error_rate, 0)


class ComputeWpmTest(unittest.TestCase):
    def test_computes_words_per_minute(self) -> None:
        text = " ".join(["word"] * 10)
        self.assertEqual(compute_wpm(text, 5.0), 120.0)

    def test_zero_duration_returns_zero(self) -> None:
        self.assertEqual(compute_wpm("some words here", 0.0), 0.0)

    def test_negative_duration_returns_zero(self) -> None:
        self.assertEqual(compute_wpm("some words here", -1.0), 0.0)


class EvaluateTuesdayReplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.qdrant = MagicMock()
        # Simulates run_tuesday_task's mark_task_pushed having already
        # created a daily_task record -- mark_task_completed needs a
        # point to find and flip.
        self.qdrant.scroll_by_filters.return_value = [
            {"id": "pushed-point-1", "payload": {"completed": False}}
        ]
        self.llm = FakeLlm([json.dumps({"translation_zh": "我記得很清楚", "analysis_zh": "發音大致準確"})])

    def test_feedback_includes_transcript_match_pace_and_chinese_feedback(self) -> None:
        feedback = evaluate_tuesday_reply(
            self.llm,
            "I remember it well and it changed everything",
            "I remember it well",
            4.0,
            qdrant=self.qdrant,
            collection="coll",
            owner="owner-a",
            week_number=1,
        )
        self.assertIn("Transcribed:", feedback)
        self.assertIn("Word match:", feedback)
        self.assertIn("Missing/changed:", feedback)
        self.assertIn("Pace:", feedback)
        self.assertIn("changed", feedback)
        self.assertIn("everything", feedback)
        self.assertIn("中文翻譯：我記得很清楚", feedback)
        self.assertIn("中文分析：發音大致準確", feedback)

    def test_perfect_match_has_no_missing_or_extra_lines(self) -> None:
        feedback = evaluate_tuesday_reply(
            self.llm,
            "hello world",
            "hello world",
            2.0,
            qdrant=self.qdrant,
            collection="coll",
            owner="owner-a",
            week_number=1,
        )
        self.assertNotIn("Missing/changed:", feedback)
        self.assertNotIn("Extra words:", feedback)

    def test_marks_tuesday_task_completed(self) -> None:
        evaluate_tuesday_reply(
            self.llm,
            "hello world",
            "hello world",
            2.0,
            qdrant=self.qdrant,
            collection="coll",
            owner="owner-a",
            week_number=1,
        )
        self.qdrant.set_payload.assert_called_once_with("coll", "pushed-point-1", {"completed": True})


if __name__ == "__main__":
    unittest.main()
