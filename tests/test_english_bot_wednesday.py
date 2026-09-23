import json
import unittest
from unittest.mock import MagicMock

from openclaw_runtime.skills.english_bot import (
    IELTS_QUESTION_BANK,
    build_wednesday_message,
    evaluate_wednesday_reply,
    mark_ielts_question_asked,
    pick_ielts_question,
    run_wednesday_task,
)


class FakeLlm:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.calls.append((prompt, schema_name))
        return self.responses.pop(0)


class QuestionBankTest(unittest.TestCase):
    def test_bank_has_twelve_questions_across_four_categories(self) -> None:
        self.assertEqual(len(IELTS_QUESTION_BANK), 12)
        categories = {q["category"] for q in IELTS_QUESTION_BANK}
        self.assertEqual(categories, {"event", "people", "place", "object"})
        for category in categories:
            self.assertEqual(sum(1 for q in IELTS_QUESTION_BANK if q["category"] == category), 3)

    def test_every_question_has_standard_cue_card_structure(self) -> None:
        for question in IELTS_QUESTION_BANK:
            self.assertTrue(question["cue_card"].startswith("Describe"))
            self.assertIn("You should say:", question["cue_card"])
            self.assertIn("and explain", question["cue_card"])

    def test_ids_are_unique(self) -> None:
        ids = [q["id"] for q in IELTS_QUESTION_BANK]
        self.assertEqual(len(ids), len(set(ids)))


class PickIeltsQuestionTest(unittest.TestCase):
    def test_excludes_already_asked_questions(self) -> None:
        asked_ids = {q["id"] for q in IELTS_QUESTION_BANK[:11]}
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [
            {"payload": {"question_id": qid}} for qid in asked_ids
        ]
        picked = pick_ielts_question(qdrant, "coll")
        self.assertEqual(picked["id"], IELTS_QUESTION_BANK[11]["id"])
        qdrant.scroll_by_filters.assert_called_once_with("coll", {"tag": "eng_ielts_topics"}, limit=512)

    def test_no_questions_asked_yet_picks_from_full_bank(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        picked = pick_ielts_question(qdrant, "coll")
        self.assertIn(picked["id"], [q["id"] for q in IELTS_QUESTION_BANK])

    def test_all_questions_already_asked_resets_and_still_picks_one(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [
            {"payload": {"question_id": q["id"]}} for q in IELTS_QUESTION_BANK
        ]
        picked = pick_ielts_question(qdrant, "coll")
        self.assertIn(picked["id"], [q["id"] for q in IELTS_QUESTION_BANK])

    def test_malformed_points_without_question_id_are_ignored(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"payload": {}}, {}]
        picked = pick_ielts_question(qdrant, "coll")
        self.assertIn(picked["id"], [q["id"] for q in IELTS_QUESTION_BANK])


class MarkIeltsQuestionAskedTest(unittest.TestCase):
    def test_writes_shared_point_with_no_owner(self) -> None:
        qdrant = MagicMock()
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1, 0.2]
        question = IELTS_QUESTION_BANK[0]

        mark_ielts_question_asked(qdrant, embeddings, "coll", question)

        args, kwargs = qdrant.upsert_text.call_args
        collection, text, vector, payload = args
        self.assertEqual(collection, "coll")
        self.assertEqual(payload["tag"], "eng_ielts_topics")
        self.assertEqual(payload["question_id"], question["id"])
        self.assertNotIn("owner", payload)


class BuildWednesdayMessageTest(unittest.TestCase):
    def test_includes_cue_card_and_star_and_timing_instructions(self) -> None:
        question = IELTS_QUESTION_BANK[0]
        message = build_wednesday_message(question)
        self.assertIn(question["cue_card"], message)
        self.assertIn("STAR", message)
        self.assertIn("1.5 to 2 minutes", message)
        self.assertIn("think for 1 minute", message)


class RunWednesdayTaskTest(unittest.TestCase):
    def test_pushes_to_every_owner_and_marks_task_pushed(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        sent: list[tuple[str, str]] = []

        question = run_wednesday_task(
            llm=FakeLlm([]),
            qdrant=qdrant,
            embeddings=embeddings,
            collection="coll",
            week_number=3,
            owners=["owner-a", "owner-b"],
            send_message=lambda owner, text: sent.append((owner, text)),
        )

        self.assertIn(question["id"], [q["id"] for q in IELTS_QUESTION_BANK])
        self.assertEqual(len(sent), 2)
        self.assertEqual({o for o, _ in sent}, {"owner-a", "owner-b"})
        # 1 upsert_text to mark the question asked (shared, no owner) + 1
        # per owner via mark_task_pushed -> owned_records.write_owned_point
        self.assertEqual(qdrant.upsert_text.call_count, 3)
        asked_call_payload = qdrant.upsert_text.call_args_list[0][0][3]
        self.assertNotIn("owner", asked_call_payload)
        owner_call_payloads = [c[0][3] for c in qdrant.upsert_text.call_args_list[1:]]
        self.assertEqual({p["owner"] for p in owner_call_payloads}, {"owner-a", "owner-b"})


class EvaluateWednesdayReplyTest(unittest.TestCase):
    def test_reports_missing_star_elements(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "situation_present": True,
                        "task_present": True,
                        "action_present": False,
                        "result_present": False,
                        "overused_words": [],
                    }
                )
            ]
        )
        feedback = evaluate_wednesday_reply(llm, "Describe a challenge...", "I once had a problem")
        self.assertIn("missing Action, Result", feedback)
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "star_evaluation")
        self.assertIn("Describe a challenge...", prompt)

    def test_reports_complete_star_and_vocabulary_suggestions(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "situation_present": True,
                        "task_present": True,
                        "action_present": True,
                        "result_present": True,
                        "overused_words": [{"word": "good", "replacement": "commendable"}],
                    }
                )
            ]
        )
        feedback = evaluate_wednesday_reply(llm, "cue card", "reply text")
        self.assertIn("complete (Situation, Task, Action, Result all present)", feedback)
        self.assertIn('"good" -> try "commendable" (band 7.5+)', feedback)


if __name__ == "__main__":
    unittest.main()
