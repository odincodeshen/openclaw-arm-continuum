import json
import unittest
from unittest.mock import MagicMock

from openclaw_runtime.skills.english_bot import (
    THURSDAY_CATEGORIES,
    THURSDAY_SAFE_AVOID,
    build_thursday_message,
    evaluate_thursday_reply,
    generate_thursday_opener,
    run_thursday_task,
)


class FakeLlm:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.calls.append((prompt, schema_name))
        return self.responses.pop(0)


class ThursdayCategoriesTest(unittest.TestCase):
    def test_seven_categories_including_sports_and_bank_holiday(self) -> None:
        self.assertEqual(len(THURSDAY_CATEGORIES), 7)
        self.assertIn("sports_banter", THURSDAY_CATEGORIES)
        self.assertIn("bank_holiday_plans", THURSDAY_CATEGORIES)

    def test_six_categories_have_a_safe_avoid_table_bank_holiday_does_not(self) -> None:
        for category in THURSDAY_CATEGORIES:
            if category == "bank_holiday_plans":
                self.assertEqual(THURSDAY_SAFE_AVOID[category], {"safe": [], "avoid": []})
            else:
                table = THURSDAY_SAFE_AVOID[category]
                self.assertTrue(table["safe"], f"{category} should have safe examples")
                self.assertTrue(table["avoid"], f"{category} should have avoid examples")


class GenerateThursdayOpenerPromptContentTest(unittest.TestCase):
    """The regression test the spec explicitly calls out (Section 6, v1.13):
    the safe/avoid table must actually land in the constructed prompt, not
    just exist as an unused constant somewhere."""

    def test_weather_banter_safe_and_avoid_phrases_are_in_the_prompt(self) -> None:
        llm = FakeLlm([json.dumps({"opener": "Blimey, another washout weekend, eh?"})])
        generate_thursday_opener(llm, "weather_banter")
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "thursday_opener")
        self.assertIn("Typical British summer, can't make up its mind.", prompt)
        self.assertIn("raining cats and dogs", prompt)
        self.assertIn("Stating an exact temperature", prompt)

    def test_pub_meetup_safe_and_avoid_phrases_are_in_the_prompt(self) -> None:
        llm = FakeLlm([json.dumps({"opener": "Fancy a swift one after work?"})])
        generate_thursday_opener(llm, "pub_meetup")
        prompt, _ = llm.calls[0]
        self.assertIn("A swift half after work?", prompt)
        self.assertIn("consume alcohol", prompt)

    def test_sports_banter_safe_and_avoid_phrases_are_in_the_prompt(self) -> None:
        llm = FakeLlm([json.dumps({"opener": "Did you catch the match?"})])
        generate_thursday_opener(llm, "sports_banter")
        prompt, _ = llm.calls[0]
        self.assertIn("absolute shambles this season", prompt)
        self.assertIn("Pretending to know detailed tactics", prompt)

    def test_bank_holiday_falls_back_to_shared_tone_instruction(self) -> None:
        llm = FakeLlm([json.dumps({"opener": "Got much planned for the long weekend?"})])
        generate_thursday_opener(llm, "bank_holiday_plans")
        prompt, _ = llm.calls[0]
        self.assertIn("understated, self-deprecating British tone", prompt)

    def test_prompt_includes_the_no_outdated_idioms_instruction(self) -> None:
        llm = FakeLlm([json.dumps({"opener": "x"})])
        generate_thursday_opener(llm, "weather_banter")
        prompt, _ = llm.calls[0]
        self.assertIn("Do not use outdated idioms", prompt)
        self.assertIn("understatement and self-deprecating banter", prompt)


class BuildThursdayMessageTest(unittest.TestCase):
    def test_includes_opener_and_anchor_bounce_instructions(self) -> None:
        message = build_thursday_message("pub_meetup", "Fancy a swift one?")
        self.assertIn("Fancy a swift one?", message)
        self.assertIn("Anchor & Bounce", message)
        self.assertIn("pub meetup", message)


class RunThursdayTaskTest(unittest.TestCase):
    def test_picks_a_valid_category_and_pushes_to_all_owners(self) -> None:
        qdrant = MagicMock()
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        llm = FakeLlm([json.dumps({"opener": "Did you get caught in that rain?"})])
        sent: list[tuple[str, str]] = []

        opener = run_thursday_task(
            llm=llm,
            qdrant=qdrant,
            embeddings=embeddings,
            collection="coll",
            week_number=3,
            owners=["owner-a", "owner-b"],
            send_message=lambda owner, text: sent.append((owner, text)),
        )

        self.assertEqual(opener, "Did you get caught in that rain?")
        self.assertEqual(len(sent), 2)
        self.assertTrue(all("Did you get caught in that rain?" in text for _, text in sent))
        self.assertEqual(qdrant.upsert_text.call_count, 2)


class EvaluateThursdayReplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.qdrant = MagicMock()
        self.qdrant.scroll_by_filters.return_value = [
            {"id": "pushed-point-1", "payload": {"completed": False}}
        ]

    def _eval(self, llm, opener, reply):
        return evaluate_thursday_reply(
            llm, opener, reply, qdrant=self.qdrant, collection="coll", owner="owner-a", week_number=1
        )

    def test_complete_anchor_and_bounce_with_banter_reply(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "anchor_present": True,
                        "bounce_present": True,
                        "vocabulary_original": "",
                        "vocabulary_replacement": "",
                        "banter_reply": "Haha, tell me about it! Same here.",
                    }
                )
            ]
        )
        report = self._eval(llm, "Did you get caught in that rain?", "Yeah drenched, you?")
        self.assertIn("complete (empathy, own situation, and a bounce-back question)", report)
        self.assertIn("Colleague text reply (Pub Banter):", report)
        self.assertIn("Haha, tell me about it! Same here.", report)
        self.assertNotIn("Suggestion:", report)

    def test_missing_bounce_is_reported(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "anchor_present": True,
                        "bounce_present": False,
                        "vocabulary_original": "",
                        "vocabulary_replacement": "",
                        "banter_reply": "Ah well, could be worse!",
                    }
                )
            ]
        )
        report = self._eval(llm, "opener", "reply with no question back")
        self.assertIn("missing Bounce (an open question thrown back)", report)

    def test_vocabulary_suggestion_included_when_present(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "anchor_present": True,
                        "bounce_present": True,
                        "vocabulary_original": "I think the garden is very bad.",
                        "vocabulary_replacement": "The lawn is a bit of a nightmare at the moment.",
                        "banter_reply": "Haha, tell me about it!",
                    }
                )
            ]
        )
        report = self._eval(llm, "opener", "reply")
        self.assertIn('Original: "I think the garden is very bad."', report)
        self.assertIn('More natural: "The lawn is a bit of a nightmare at the moment."', report)

    def test_prompt_never_mentions_tts_as_something_to_do(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "anchor_present": True,
                        "bounce_present": True,
                        "vocabulary_original": "",
                        "vocabulary_replacement": "",
                        "banter_reply": "x",
                    }
                )
            ]
        )
        self._eval(llm, "opener", "reply")
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "thursday_evaluation")
        self.assertIn("not TTS -- just text", prompt)

    def test_marks_thursday_task_completed(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "anchor_present": True,
                        "bounce_present": True,
                        "vocabulary_original": "",
                        "vocabulary_replacement": "",
                        "banter_reply": "x",
                    }
                )
            ]
        )
        self._eval(llm, "opener", "reply")
        self.qdrant.set_payload.assert_called_once_with("coll", "pushed-point-1", {"completed": True})


if __name__ == "__main__":
    unittest.main()
