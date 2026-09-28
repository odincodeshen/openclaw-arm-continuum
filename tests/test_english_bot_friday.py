import json
import unittest
from unittest.mock import MagicMock

from openclaw_runtime.skills.english_bot import (
    build_friday_message,
    evaluate_chunk_usage,
    evaluate_friday_reply,
    read_chunk_progress,
    read_this_week_chunks,
    record_chunk_usage,
    run_friday_task,
)
from tests.card_checks import assert_feedback_card, assert_task_card


class FakeLlm:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.calls.append((prompt, schema_name))
        return self.responses.pop(0)


CHUNKS = [
    {"phrase": "take a gamble on", "definition": "冒險一試", "context_sentence": "We took a gamble on it."},
    {"phrase": "spread oneself too thin", "definition": "心力過度分散", "context_sentence": "I was spreading myself too thin."},
    {"phrase": "get to grips with", "definition": "掌握複雜事物", "context_sentence": "It took weeks to get to grips with it."},
]


class ReadThisWeekChunksTest(unittest.TestCase):
    def test_returns_all_chunk_payloads(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"payload": c} for c in CHUNKS]
        result = read_this_week_chunks(qdrant, "coll", 1)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["phrase"], "take a gamble on")

    def test_raises_when_monday_has_not_run(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        with self.assertRaises(ValueError):
            read_this_week_chunks(qdrant, "coll", 1)


class BuildFridayMessageTest(unittest.TestCase):
    def test_lists_chunks_and_asks_for_voice_ramble(self) -> None:
        message = build_friday_message(CHUNKS, 2)
        assert_task_card(self, message, "fri")
        self.assertIn("• <b>take a gamble on</b> — 冒險一試\n  <i>We took a gamble on it.</i>", message)
        self.assertIn("spread oneself too thin", message)
        self.assertIn("get to grips with", message)
        self.assertIn("voice", message.lower())
        self.assertIn("2", message)

    def test_example_is_left_out_when_it_does_not_contain_the_phrase(self) -> None:
        chunks = [{"phrase": "from scratch", "definition": "從零開始", "context_sentence": "我們從零開始。"}]
        message = build_friday_message(chunks, 2)
        self.assertIn("• <b>from scratch</b> — 從零開始", message)
        self.assertNotIn("我們從零開始", message)


class RunFridayTaskTest(unittest.TestCase):
    def test_pushes_message_and_marks_task_for_every_owner(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.side_effect = lambda collection, filters, limit=64, **kw: (
            [{"payload": c} for c in CHUNKS] if filters.get("kind") == "weekly_content" else []
        )
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        sent = []

        chunks = run_friday_task(
            qdrant=qdrant,
            embeddings=embeddings,
            collection="coll",
            week_number=1,
            owners=["owner-a", "owner-b"],
            send_message=lambda owner, text: sent.append((owner, text)),
        )

        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(sent), 2)
        self.assertIn("take a gamble on", sent[0][1])


class EvaluateChunkUsageTest(unittest.TestCase):
    def test_prompt_mentions_semantic_morphology_tolerance(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "chunk_results": [
                            {"phrase": "take a gamble on", "used_correctly": False, "user_sentence": ""},
                        ]
                    }
                )
            ]
        )
        result = evaluate_chunk_usage(llm, CHUNKS[:1], "some transcribed reply")
        self.assertEqual(result["chunk_results"][0]["phrase"], "take a gamble on")
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "friday_chunk_usage")
        self.assertIn("spreading myself too thin", prompt)


class RecordChunkUsageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.qdrant = MagicMock()
        self.embeddings = MagicMock()
        self.embeddings.embed.return_value = [0.1]

    def test_creates_new_record_when_none_exists(self) -> None:
        self.qdrant.scroll_by_filters.return_value = []
        record_chunk_usage(
            self.qdrant,
            self.embeddings,
            "coll",
            "owner-a",
            1,
            "take a gamble on",
            used_correctly=True,
            user_sentence="We took a gamble on it.",
            source="fri_voice",
            evaluation="fri",
        )
        self.qdrant.upsert_text.assert_called_once()
        args, kwargs = self.qdrant.upsert_text.call_args
        payload = args[3]
        self.assertEqual(payload["owner"], "owner-a")
        self.assertEqual(payload["chunk"], "take a gamble on")
        self.assertFalse(payload["needs_review"])
        self.assertEqual(payload["review_by_source"], {"fri": False})
        self.assertEqual(payload["user_sentence"], "We took a gamble on it.")
        self.assertEqual(payload["user_sentence_source"], "fri_voice")

    def test_updates_existing_record_instead_of_creating_a_new_one(self) -> None:
        self.qdrant.scroll_by_filters.return_value = [
            {"id": "existing-id", "payload": {"needs_review": False, "chunk": "take a gamble on"}}
        ]
        record_chunk_usage(
            self.qdrant,
            self.embeddings,
            "coll",
            "owner-a",
            1,
            "take a gamble on",
            used_correctly=False,
            user_sentence="",
            source="",
            evaluation="sat",
        )
        self.qdrant.upsert_text.assert_not_called()
        self.qdrant.set_payload.assert_called_once_with(
            "coll", "existing-id", {"review_by_source": {"sat": True}, "needs_review": True}
        )

    def _record(self, existing_payload: dict, *, used_correctly: bool, evaluation: str) -> dict:
        self.qdrant.reset_mock()
        self.qdrant.scroll_by_filters.return_value = [{"id": "existing-id", "payload": existing_payload}]
        record_chunk_usage(
            self.qdrant,
            self.embeddings,
            "coll",
            "owner-a",
            1,
            "take a gamble on",
            used_correctly=used_correctly,
            user_sentence="",
            source="",
            evaluation=evaluation,
        )
        return self.qdrant.set_payload.call_args.args[2]

    def test_redoing_the_same_evaluation_correctly_clears_its_own_miss(self) -> None:
        fields = self._record({"review_by_source": {"fri": True}}, used_correctly=True, evaluation="fri")
        self.assertEqual(fields, {"review_by_source": {"fri": False}, "needs_review": False})

    def test_another_evaluations_miss_still_flags_the_chunk(self) -> None:
        fields = self._record({"review_by_source": {"fri": False}}, used_correctly=False, evaluation="sat")
        self.assertTrue(fields["needs_review"])
        # ...and getting Friday right again doesn't clear Saturday's miss
        fields = self._record(
            {"review_by_source": {"fri": False, "sat": True}}, used_correctly=True, evaluation="fri"
        )
        self.assertEqual(fields, {"review_by_source": {"fri": False, "sat": True}, "needs_review": True})

    def test_a_flag_from_before_the_per_evaluation_rule_is_kept(self) -> None:
        fields = self._record({"needs_review": True}, used_correctly=True, evaluation="fri")
        self.assertEqual(fields, {"review_by_source": {"legacy": True, "fri": False}, "needs_review": True})

    def test_does_not_overwrite_user_sentence_when_none_given(self) -> None:
        self.qdrant.scroll_by_filters.return_value = [
            {"id": "existing-id", "payload": {"needs_review": False, "chunk": "take a gamble on"}}
        ]
        record_chunk_usage(
            self.qdrant,
            self.embeddings,
            "coll",
            "owner-a",
            1,
            "take a gamble on",
            used_correctly=True,
            user_sentence="",
            source="",
            evaluation="sat",
        )
        self.qdrant.set_payload.assert_called_once_with(
            "coll", "existing-id", {"review_by_source": {"sat": False}, "needs_review": False}
        )


class ReadChunkProgressTest(unittest.TestCase):
    def test_returns_payload_when_found(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"id": "p1", "payload": {"chunk": "take a gamble on"}}]
        result = read_chunk_progress(qdrant, "coll", "owner-a", 1, "take a gamble on")
        self.assertEqual(result["chunk"], "take a gamble on")

    def test_returns_none_when_not_found(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        result = read_chunk_progress(qdrant, "coll", "owner-a", 1, "take a gamble on")
        self.assertIsNone(result)


class EvaluateFridayReplyTest(unittest.TestCase):
    def test_writes_back_user_sentence_and_returns_report(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "chunk_results": [
                            {
                                "phrase": "take a gamble on",
                                "used_correctly": True,
                                "user_sentence": "I took a gamble on the new job.",
                            },
                            {"phrase": "spread oneself too thin", "used_correctly": False, "user_sentence": ""},
                            {"phrase": "get to grips with", "used_correctly": False, "user_sentence": ""},
                        ],
                        "model_answer": "Honestly, I took a gamble on the new job...",
                    }
                )
            ]
        )

        report = evaluate_friday_reply(
            qdrant, embeddings, llm, "coll", "owner-a", 1, CHUNKS, "I took a gamble on the new job."
        )

        assert_feedback_card(self, report, "fri")
        self.assertIn("✅ take a gamble on", report)
        self.assertIn("❌ spread oneself too thin (not used, or not used naturally)", report)
        self.assertIn("• Next time, try working in: spread oneself too thin, get to grips with", report)
        self.assertEqual(qdrant.upsert_text.call_count, 3)
        first_call_payload = qdrant.upsert_text.call_args_list[0].args[3]
        self.assertEqual(first_call_payload["user_sentence"], "I took a gamble on the new job.")
        self.assertEqual(first_call_payload["user_sentence_source"], "fri_voice")
        self.assertIn("<blockquote expandable>Honestly, I took a gamble on the new job...</blockquote>", report)
        prompt, _ = llm.calls[0]
        self.assertIn("model_answer", prompt)


if __name__ == "__main__":
    unittest.main()
