import json
import unittest
from unittest.mock import MagicMock

from openclaw_runtime.skills.english_bot import (
    ClozeQuestion,
    build_cloze_question,
    build_saturday_message,
    evaluate_saturday_answers,
    generate_cloze,
    judge_cloze_answers_with_llm,
    run_saturday_task,
)


class FakeLlm:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.calls.append((prompt, schema_name))
        return self.responses.pop(0)


class GenerateClozeTest(unittest.TestCase):
    def test_exact_phrase_present_verbatim(self) -> None:
        result = generate_cloze("take a gamble on", "We decided to take a gamble on the new design.")
        self.assertEqual(result, "We decided to ____ the new design.")

    def test_verb_inflection_variant_matches(self) -> None:
        result = generate_cloze("get to grips with", "It took weeks to really get to grips with the codebase.")
        self.assertEqual(result, "It took weeks to really ____ the codebase.")

    def test_reflexive_pronoun_variant_matches(self) -> None:
        """The spec's own example: 'spread oneself too thin' is never said
        with the literal word 'oneself' -- it always surfaces as
        myself/yourself/etc. This is the single most important case."""
        result = generate_cloze(
            "spread oneself too thin",
            "Juggling three feature rollouts meant I was spreading myself too thin.",
        )
        self.assertEqual(result, "Juggling three feature rollouts meant I was ____.")

    def test_different_reflexive_pronoun_also_matches(self) -> None:
        result = generate_cloze("spread oneself too thin", "You are spreading yourself too thin.")
        self.assertEqual(result, "You are ____.")

    def test_case_insensitive_match(self) -> None:
        result = generate_cloze("take a gamble on", "We Take A Gamble On the plan.")
        self.assertEqual(result, "We ____ the plan.")

    def test_phrase_not_found_returns_none(self) -> None:
        result = generate_cloze("spread oneself too thin", "This sentence is about something else entirely.")
        self.assertIsNone(result)

    def test_empty_sentence_returns_none(self) -> None:
        self.assertIsNone(generate_cloze("take a gamble on", ""))

    def test_empty_phrase_returns_none(self) -> None:
        self.assertIsNone(generate_cloze("", "some sentence"))

    def test_known_limitation_irregular_past_tense_does_not_match(self) -> None:
        """Documents a real, accepted limitation: the matcher tolerates
        suffix variation that keeps the literal stem as a prefix (take ->
        takes, take -> taken), via a simple word-stem + \\w* match. It does
        NOT handle irregular stem changes (take -> took) or silent-e-drop
        spelling changes (take -> taking, since "take" is not a literal
        prefix of "taking"). Building a full verb conjugator is out of
        scope; callers fall back to context_sentence or a phrase-only
        prompt when this happens (see build_cloze_question)."""
        self.assertIsNone(generate_cloze("take a gamble on", "We took a gamble on the new design."))
        self.assertIsNone(generate_cloze("take a gamble on", "We are taking a gamble on the new design."))


def _fake_cloze_result(correct: bool) -> dict:
    return {
        "correct": correct,
        "explanation_zh": "說明文字",
        "example_sentence": "An example sentence.",
    }


class JudgeClozeAnswersWithLlmTest(unittest.TestCase):
    def test_returns_one_result_per_question_in_order(self) -> None:
        llm = FakeLlm([json.dumps({"results": [_fake_cloze_result(True), _fake_cloze_result(False)]})])
        questions = [
            {"phrase": "move on", "prompt_text": "We had to ____."},
            {"phrase": "bust down the door", "prompt_text": "They ____ to get in."},
        ]
        results = judge_cloze_answers_with_llm(llm, questions, "1. Move on 2. Something wrong")
        self.assertEqual([r["correct"] for r in results], [True, False])
        self.assertIn("explanation_zh", results[0])
        self.assertIn("example_sentence", results[0])
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "saturday_cloze_judge")

    def test_prompt_warns_against_position_agnostic_matching(self) -> None:
        """The whole point of this function: catch an answer that mentions
        the right phrase but for the WRONG blank -- the prompt must tell
        the LLM position matters, not just presence anywhere."""
        llm = FakeLlm([json.dumps({"results": [_fake_cloze_result(False), _fake_cloze_result(False)]})])
        questions = [
            {"phrase": "move on", "prompt_text": "We had to ____."},
            {"phrase": "bust down the door", "prompt_text": "They ____ to get in."},
        ]
        judge_cloze_answers_with_llm(llm, questions, "1. Bust down the door 2. Move on")
        prompt, _ = llm.calls[0]
        self.assertIn("position matters", prompt.lower())

    def test_prompt_asks_for_chinese_explanation_and_example_sentence(self) -> None:
        llm = FakeLlm([json.dumps({"results": [_fake_cloze_result(True)]})])
        questions = [{"phrase": "move on", "prompt_text": "We had to ____."}]
        judge_cloze_answers_with_llm(llm, questions, "Move on")
        prompt, _ = llm.calls[0]
        self.assertIn("繁體中文", prompt)
        self.assertIn("example sentence", prompt.lower())


class BuildClozeQuestionTest(unittest.TestCase):
    def test_prefers_user_sentence_when_present_and_matchable(self) -> None:
        chunk = {"phrase": "take a gamble on", "context_sentence": "We take a gamble on it."}
        progress = {"user_sentence": "I have taken a gamble on the new job this year."}
        question = build_cloze_question(chunk, progress)
        self.assertEqual(question.source, "user_sentence")
        self.assertIn("____", question.prompt_text)
        self.assertIn("new job", question.prompt_text)

    def test_falls_back_to_context_sentence_when_no_progress(self) -> None:
        chunk = {"phrase": "take a gamble on", "context_sentence": "We take a gamble on it."}
        question = build_cloze_question(chunk, None)
        self.assertEqual(question.source, "context_sentence")
        self.assertEqual(question.prompt_text, "We ____ it.")

    def test_falls_back_to_context_sentence_when_user_sentence_does_not_match(self) -> None:
        chunk = {"phrase": "take a gamble on", "context_sentence": "We take a gamble on it."}
        progress = {"user_sentence": "This sentence never mentions the target chunk at all."}
        question = build_cloze_question(chunk, progress)
        self.assertEqual(question.source, "context_sentence")

    def test_falls_back_to_phrase_only_when_nothing_matches(self) -> None:
        chunk = {"phrase": "take a gamble on", "context_sentence": "Unrelated sentence entirely."}
        question = build_cloze_question(chunk, None)
        self.assertEqual(question.source, "phrase_only")
        self.assertIn("take a gamble on", question.prompt_text)


class BuildSaturdayMessageTest(unittest.TestCase):
    def test_numbers_each_question(self) -> None:
        questions = [
            ClozeQuestion(phrase="a", prompt_text="Blank 1 ____ here.", source="context_sentence"),
            ClozeQuestion(phrase="b", prompt_text="Blank 2 ____ here.", source="context_sentence"),
        ]
        message = build_saturday_message(questions)
        self.assertIn("1. Blank 1", message)
        self.assertIn("2. Blank 2", message)
        self.assertIn("text or voice", message.lower())


class RunSaturdayTaskTest(unittest.TestCase):
    def test_pushes_quiz_and_sets_pending_answer_per_owner(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.side_effect = lambda collection, filters, limit=64: (
            [{"payload": {"phrase": "take a gamble on", "context_sentence": "We took a gamble on it."}}]
            if filters.get("kind") == "weekly_content"
            else []
        )
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        sent = []
        pending = []

        result = run_saturday_task(
            qdrant=qdrant,
            embeddings=embeddings,
            collection="coll",
            week_number=1,
            owners=["owner-a", "owner-b"],
            send_message=lambda owner, text: sent.append((owner, text)),
            set_pending_answer=lambda owner, item: pending.append((owner, item)),
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0][1]["kind"], "eng_saturday_quiz")
        self.assertEqual(
            pending[0][1]["questions"],
            [
                {
                    "phrase": "take a gamble on",
                    "prompt_text": '(no example sentence on file) Use "take a gamble on" correctly in a sentence.',
                }
            ],
        )


class EvaluateSaturdayAnswersTest(unittest.TestCase):
    def test_records_correct_and_incorrect_answers(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        llm = FakeLlm(
            [json.dumps({"results": [_fake_cloze_result(True), _fake_cloze_result(False)]})]
        )
        questions = [
            {"phrase": "take a gamble on", "prompt_text": "We ____ the plan."},
            {"phrase": "spread oneself too thin", "prompt_text": "I was ____."},
        ]

        report = evaluate_saturday_answers(
            llm,
            qdrant,
            embeddings,
            "coll",
            "owner-a",
            1,
            questions,
            "I have taken a gamble on it but I have no idea about the second one.",
        )

        self.assertIn("take a gamble on: correct", report)
        self.assertIn("spread oneself too thin: needs review", report)
        self.assertIn("說明文字", report)
        self.assertIn("An example sentence.", report)
        self.assertEqual(qdrant.upsert_text.call_count, 2)

    def test_position_swapped_phrase_is_not_credited(self) -> None:
        """Regression test for the real bug found live: an answer that
        mentions the right phrase for the WRONG question must not be
        credited -- the LLM judge (unlike the old substring-anywhere
        check) is told which result goes with which question."""
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        llm = FakeLlm(
            [json.dumps({"results": [_fake_cloze_result(False), _fake_cloze_result(False)]})]
        )
        questions = [
            {"phrase": "move on", "prompt_text": "We had to ____."},
            {"phrase": "bust down the door", "prompt_text": "They ____ to get in."},
        ]

        report = evaluate_saturday_answers(
            llm, qdrant, embeddings, "coll", "owner-a", 1, questions, "1. Bust down the door 2. Move on"
        )

        self.assertIn("move on: needs review", report)
        self.assertIn("bust down the door: needs review", report)

    def test_marks_saturday_task_completed(self) -> None:
        qdrant = MagicMock()

        def scroll_by_filters(collection, filters, limit=64):
            if filters.get("kind") == "daily_task":
                return [{"id": "pushed-point-1", "payload": {"completed": False}}]
            return []  # no existing chunk_progress record for either phrase

        qdrant.scroll_by_filters.side_effect = scroll_by_filters
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        llm = FakeLlm([json.dumps({"results": [_fake_cloze_result(True)]})])
        questions = [{"phrase": "take a gamble on", "prompt_text": "We ____ it."}]

        evaluate_saturday_answers(llm, qdrant, embeddings, "coll", "owner-a", 1, questions, "I take a gamble on it.")

        qdrant.set_payload.assert_any_call("coll", "pushed-point-1", {"completed": True})


if __name__ == "__main__":
    unittest.main()
