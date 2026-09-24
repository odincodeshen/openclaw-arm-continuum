import unittest
from unittest.mock import MagicMock

from openclaw_runtime.skills.english_bot import (
    ClozeQuestion,
    build_cloze_question,
    build_saturday_message,
    evaluate_saturday_answers,
    generate_cloze,
    judge_cloze_answer,
    run_saturday_task,
)


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


class JudgeClozeAnswerTest(unittest.TestCase):
    def test_correct_answer_with_exact_phrase(self) -> None:
        self.assertTrue(judge_cloze_answer("take a gamble on", "take a gamble on"))

    def test_correct_answer_with_morphology(self) -> None:
        self.assertTrue(judge_cloze_answer("spread oneself too thin", "spreading myself too thin"))

    def test_wrong_answer(self) -> None:
        self.assertFalse(judge_cloze_answer("take a gamble on", "make a decision about"))

    def test_empty_answer_is_wrong(self) -> None:
        self.assertFalse(judge_cloze_answer("take a gamble on", ""))


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
        self.assertEqual(pending[0][1]["phrases"], ["take a gamble on"])


class EvaluateSaturdayAnswersTest(unittest.TestCase):
    def test_records_correct_and_incorrect_answers(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]

        report = evaluate_saturday_answers(
            qdrant,
            embeddings,
            "coll",
            "owner-a",
            1,
            ["take a gamble on", "spread oneself too thin"],
            "I have taken a gamble on it but I have no idea about the second one.",
        )

        self.assertIn("take a gamble on: correct", report)
        self.assertIn("spread oneself too thin: needs review", report)
        self.assertEqual(qdrant.upsert_text.call_count, 2)


if __name__ == "__main__":
    unittest.main()
