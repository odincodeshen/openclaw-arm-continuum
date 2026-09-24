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
        message = build_friday_message(CHUNKS)
        self.assertIn("take a gamble on", message)
        self.assertIn("spread oneself too thin", message)
        self.assertIn("get to grips with", message)
        self.assertIn("voice", message.lower())
        self.assertIn("2", message)


class RunFridayTaskTest(unittest.TestCase):
    def test_pushes_message_and_marks_task_for_every_owner(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"payload": c} for c in CHUNKS]
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
        )
        self.qdrant.upsert_text.assert_called_once()
        args, kwargs = self.qdrant.upsert_text.call_args
        payload = args[3]
        self.assertEqual(payload["owner"], "owner-a")
        self.assertEqual(payload["chunk"], "take a gamble on")
        self.assertFalse(payload["needs_review"])
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
        )
        self.qdrant.upsert_text.assert_not_called()
        self.qdrant.set_payload.assert_called_once_with("coll", "existing-id", {"needs_review": True})

    def test_needs_review_is_sticky_once_set(self) -> None:
        """A later correct evaluation must not clear an existing True flag
        (spec 2.5 point 4: any single failure marks it, no un-flagging)."""
        self.qdrant.scroll_by_filters.return_value = [
            {"id": "existing-id", "payload": {"needs_review": True, "chunk": "take a gamble on"}}
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
        )
        self.qdrant.set_payload.assert_called_once_with("coll", "existing-id", {"needs_review": True})

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
        )
        self.qdrant.set_payload.assert_called_once_with("coll", "existing-id", {"needs_review": False})


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
                        ]
                    }
                )
            ]
        )

        report = evaluate_friday_reply(
            qdrant, embeddings, llm, "coll", "owner-a", 1, CHUNKS, "I took a gamble on the new job."
        )

        self.assertIn("used correctly", report)
        self.assertIn("not detected", report)
        self.assertEqual(qdrant.upsert_text.call_count, 3)
        first_call_payload = qdrant.upsert_text.call_args_list[0].args[3]
        self.assertEqual(first_call_payload["user_sentence"], "I took a gamble on the new job.")
        self.assertEqual(first_call_payload["user_sentence_source"], "fri_voice")


if __name__ == "__main__":
    unittest.main()
