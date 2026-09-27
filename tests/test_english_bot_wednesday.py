import json
import unittest
from unittest.mock import MagicMock

from openclaw_runtime.skills.english_bot import (
    IELTS_QUESTION_BANK,
    PART3_THEMES,
    WEDNESDAY_COMBO_THRESHOLD_WEEK,
    build_wednesday_message,
    evaluate_part3_argument,
    evaluate_wednesday_reply,
    generate_part3_question,
    is_part2_3_combo_week,
    mark_ielts_question_asked,
    pick_ielts_question,
    run_wednesday_task,
)
from tests.card_checks import assert_feedback_card, assert_task_card


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
        message = build_wednesday_message(question, 4)
        assert_task_card(self, message, "wed")
        self.assertIn("Week 4", message)
        self.assertIn("Describe a time when you received bad customer service", message)
        self.assertIn("• when and where it was", message)
        self.assertNotIn("- when and where it was", message)
        self.assertIn("STAR", message)
        self.assertIn("Voice, 1.5–2 min", message)
        self.assertIn("Think for 1 minute", message)
        self.assertNotIn("Part 3", message)

    def test_combo_week_adds_part3_block_to_the_same_card(self) -> None:
        message = build_wednesday_message(IELTS_QUESTION_BANK[0], 18, part3_question="Is AI good?")
        assert_task_card(self, message, "wed")
        self.assertIn("<b>Part 3</b>\nIs AI good?", message)
        self.assertIn("One voice message", message)


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
    def setUp(self) -> None:
        self.qdrant = MagicMock()
        self.qdrant.scroll_by_filters.return_value = [
            {"id": "pushed-point-1", "payload": {"completed": False}}
        ]

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
        feedback = evaluate_wednesday_reply(
            llm,
            "Describe a challenge...",
            "I once had a problem",
            qdrant=self.qdrant,
            collection="coll",
            owner="owner-a",
            week_number=1,
        )
        assert_feedback_card(self, feedback, "wed")
        self.assertIn("✅ Situation　✅ Task　❌ Action　❌ Result", feedback)
        self.assertIn("• Add the missing STAR part: Action, Result", feedback)
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
                        "model_answer": "One weekend I decided to...",
                    }
                )
            ]
        )
        feedback = evaluate_wednesday_reply(
            llm, "cue card", "reply text", qdrant=self.qdrant, collection="coll", owner="owner-a", week_number=1
        )
        assert_feedback_card(self, feedback, "wed")
        self.assertIn("✅ Situation　✅ Task　✅ Action　✅ Result", feedback)
        self.assertIn('• "good" → "commendable" (band 7.5+)', feedback)

    def test_appends_model_answer_after_the_evaluation(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "situation_present": True,
                        "task_present": False,
                        "action_present": True,
                        "result_present": False,
                        "overused_words": [{"word": "good", "replacement": "commendable"}],
                        "model_answer": "  Last spring I had to organise a surprise party...  ",
                    }
                )
            ]
        )
        feedback = evaluate_wednesday_reply(
            llm, "cue card", "reply text", qdrant=self.qdrant, collection="coll", owner="owner-a", week_number=1
        )
        assert_feedback_card(self, feedback, "wed")
        self.assertIn(
            "<b>Example</b> (tap to expand)\n<blockquote expandable>Last spring I had to organise a surprise party..."
            "</blockquote>",
            feedback,
        )
        prompt, _ = llm.calls[0]
        self.assertIn("model_answer", prompt)
        self.assertIn("band 8", prompt)

    def test_missing_model_answer_is_omitted_not_an_error(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "situation_present": True,
                        "task_present": True,
                        "action_present": True,
                        "result_present": True,
                        "overused_words": [],
                    }
                )
            ]
        )
        feedback = evaluate_wednesday_reply(
            llm, "cue card", "reply text", qdrant=self.qdrant, collection="coll", owner="owner-a", week_number=1
        )
        assert_feedback_card(self, feedback, "wed")
        self.assertIn("(not available this time)", feedback)

    def test_marks_wednesday_task_completed(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "situation_present": True,
                        "task_present": True,
                        "action_present": True,
                        "result_present": True,
                        "overused_words": [],
                    }
                )
            ]
        )
        evaluate_wednesday_reply(
            llm, "cue card", "reply text", qdrant=self.qdrant, collection="coll", owner="owner-a", week_number=1
        )
        self.qdrant.set_payload.assert_called_once_with("coll", "pushed-point-1", {"completed": True})


class IsPart23ComboWeekTest(unittest.TestCase):
    def test_threshold_constant_is_17(self) -> None:
        self.assertEqual(WEDNESDAY_COMBO_THRESHOLD_WEEK, 17)

    def test_week_17_is_not_combo(self) -> None:
        self.assertFalse(is_part2_3_combo_week(17))

    def test_week_18_is_combo(self) -> None:
        self.assertTrue(is_part2_3_combo_week(18))

    def test_week_1_is_not_combo(self) -> None:
        self.assertFalse(is_part2_3_combo_week(1))

    def test_week_far_beyond_threshold_is_combo(self) -> None:
        self.assertTrue(is_part2_3_combo_week(38))


class GeneratePart3QuestionTest(unittest.TestCase):
    def test_prompt_includes_part2_topic_and_theme_list_parses_response(self) -> None:
        llm = FakeLlm([json.dumps({"part3_question": "Does AI enhance or reduce problem-solving skills?"})])
        question = generate_part3_question(llm, "Describe a time you solved a difficult problem.")
        self.assertEqual(question, "Does AI enhance or reduce problem-solving skills?")
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "part3_question")
        self.assertIn("Describe a time you solved a difficult problem.", prompt)
        for theme in PART3_THEMES:
            self.assertIn(theme, prompt)


class EvaluatePart3ArgumentTest(unittest.TestCase):
    def test_prompt_includes_question_and_reply_parses_response(self) -> None:
        llm = FakeLlm(
            [json.dumps({"claim_present": True, "concession_present": False, "conclusion_present": True})]
        )
        result = evaluate_part3_argument(llm, "Does automation help?", "I think yes because...")
        self.assertEqual(
            result, {"claim_present": True, "concession_present": False, "conclusion_present": True}
        )
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "part3_argument_evaluation")
        self.assertIn("Does automation help?", prompt)
        self.assertIn("I think yes because...", prompt)


class RunWednesdayTaskComboTest(unittest.TestCase):
    def test_non_combo_week_does_not_generate_part3(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        llm = FakeLlm([])  # no responses queued -- would raise if chat_json were called

        result = run_wednesday_task(
            llm=llm,
            qdrant=qdrant,
            embeddings=embeddings,
            collection="coll",
            week_number=WEDNESDAY_COMBO_THRESHOLD_WEEK,
            owners=["owner-a"],
            send_message=lambda owner, text: None,
        )

        self.assertNotIn("part3_question", result)
        self.assertEqual(llm.calls, [])

    def test_combo_week_generates_and_pushes_part3(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        llm = FakeLlm([json.dumps({"part3_question": "Does AI enhance or reduce problem-solving skills?"})])
        sent: list[tuple[str, str]] = []

        result = run_wednesday_task(
            llm=llm,
            qdrant=qdrant,
            embeddings=embeddings,
            collection="coll",
            week_number=WEDNESDAY_COMBO_THRESHOLD_WEEK + 1,
            owners=["owner-a"],
            send_message=lambda owner, text: sent.append((owner, text)),
        )

        self.assertEqual(result["part3_question"], "Does AI enhance or reduce problem-solving skills?")
        assert_task_card(self, sent[0][1], "wed")
        self.assertIn("Does AI enhance or reduce problem-solving skills?", sent[0][1])
        self.assertIn("Part 2 + 3", sent[0][1])


class EvaluateWednesdayReplyDualEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.qdrant = MagicMock()
        self.qdrant.scroll_by_filters.return_value = [
            {"id": "pushed-point-1", "payload": {"completed": False}}
        ]

    def test_combo_week_runs_both_star_and_argument_checks(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "situation_present": True,
                        "task_present": True,
                        "action_present": True,
                        "result_present": True,
                        "overused_words": [],
                        "model_answer": "Part two story...",
                    }
                ),
                json.dumps(
                    {
                        "claim_present": True,
                        "concession_present": False,
                        "conclusion_present": True,
                        "model_answer": "Part three argument...",
                    }
                ),
            ]
        )
        feedback = evaluate_wednesday_reply(
            llm,
            "cue card",
            "full reply covering both parts",
            qdrant=self.qdrant,
            collection="coll",
            owner="owner-a",
            week_number=WEDNESDAY_COMBO_THRESHOLD_WEEK + 1,
            part3_question="Does automation help or hurt problem-solving?",
        )
        assert_feedback_card(self, feedback, "wed")
        self.assertIn("Part 2: ✅ Situation　✅ Task　✅ Action　✅ Result", feedback)
        self.assertIn("Part 3: ✅ Claim　❌ Concession　✅ Conclusion", feedback)
        self.assertIn("• Part 3 needs: Concession", feedback)
        self.assertIn(
            "Part 2:\nPart two story...\n\nPart 3 — Does automation help or hurt problem-solving?\n"
            "Part three argument...",
            feedback,
        )
        self.assertEqual(len(llm.calls), 2)
        self.qdrant.set_payload.assert_called_once_with("coll", "pushed-point-1", {"completed": True})

    def test_non_combo_week_runs_only_star_no_argument_section(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "situation_present": True,
                        "task_present": True,
                        "action_present": True,
                        "result_present": True,
                        "overused_words": [],
                    }
                )
            ]
        )
        feedback = evaluate_wednesday_reply(
            llm,
            "cue card",
            "reply text",
            qdrant=self.qdrant,
            collection="coll",
            owner="owner-a",
            week_number=1,
        )
        assert_feedback_card(self, feedback, "wed")
        self.assertNotIn("Part 2:", feedback)
        self.assertNotIn("Part 3", feedback)
        self.assertEqual(len(llm.calls), 1)
        self.qdrant.set_payload.assert_called_once_with("coll", "pushed-point-1", {"completed": True})


if __name__ == "__main__":
    unittest.main()
