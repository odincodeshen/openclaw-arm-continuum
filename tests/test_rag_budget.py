import dataclasses
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from openclaw_runtime.llm_client import estimate_tokens  # noqa: E402
from openclaw_runtime.rag_budget import HEADER_TOKENS, drop_weak_hits, fit_passages, focus_passage, terms  # noqa: E402
from openclaw_runtime.skills.memory import RagRetrieveSkill  # noqa: E402
from support import build_settings  # noqa: E402

FILLER = "The weather was mild and nothing else of note happened that afternoon. "
BATTERY = "Battery backup gives 40 minutes at idle and about 12 minutes under full inference load. "


def hit(text, score=0.5, source="a.md"):
    return {"score": score, "payload": {"text": text, "original_file_name": source}}


class TermsTest(unittest.TestCase):
    def test_latin_words_lowercased_without_stop_words(self):
        self.assertEqual(terms("What is the Battery backup?"), {"battery", "backup"})

    def test_cjk_text_becomes_character_pairs(self):
        self.assertEqual(terms("電池備援"), {"電池", "池備", "備援"})


class FocusPassageTest(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(focus_passage(BATTERY, "battery", 200), BATTERY)

    def test_zero_means_no_limit(self):
        text = FILLER * 50
        self.assertEqual(focus_passage(text, "battery", 0), text)

    def test_keeps_the_sentences_matching_the_question(self):
        text = FILLER * 10 + BATTERY + FILLER * 10
        out = focus_passage(text, "how long does the battery backup last?", 60)
        self.assertIn("12 minutes under full inference load", out)
        self.assertTrue(out.startswith("… ") and out.endswith(" …"))
        self.assertLessEqual(estimate_tokens(out.strip("… ")), 60)

    def test_chinese_question_finds_chinese_sentence(self):
        text = "今天天氣很好，我們去公園散步。" * 8 + "電池備援在滿載時大約可以撐十二分鐘。" + "晚餐吃了麵。" * 8
        out = focus_passage(text, "電池備援可以撐多久？", 40)
        self.assertIn("十二分鐘", out)

    def test_single_over_long_sentence_is_cut(self):
        text = "word " * 400
        out = focus_passage(text, "word", 30)
        self.assertLessEqual(estimate_tokens(out.rstrip(" …")), 30)
        self.assertTrue(out.endswith(" …"))


class FitPassagesTest(unittest.TestCase):
    def test_unchanged_when_both_limits_are_zero(self):
        hits = [("kb", [hit("x")])]
        self.assertIs(fit_passages(hits, "q", context_tokens=0, passage_tokens=0), hits)

    def test_keeps_best_scores_within_budget_in_original_order(self):
        block = "alpha " * 60  # ~120 tokens each
        sections = [
            ("tracker", [hit(block + "1", 0.40)]),
            ("kb", [hit(block + "2", 0.90), hit(block + "3", 0.30)]),
            ("category:x", [hit(block + "4", 0.80)]),
        ]
        cost = estimate_tokens(block + "1") + HEADER_TOKENS
        out = fit_passages(sections, "alpha", context_tokens=cost * 2, passage_tokens=0)
        self.assertEqual([label for label, _ in out], ["tracker", "kb", "category:x"])
        kept = [h["payload"]["text"][-1] for _, hits in out for h in hits]
        self.assertEqual(kept, ["2", "4"])

    def test_named_file_comes_first(self):
        block = "alpha " * 60
        sections = [("filename_match", [hit(block + "f", 0.0)]), ("kb", [hit(block + "k", 0.99)])]
        cost = estimate_tokens(block + "f") + HEADER_TOKENS
        out = fit_passages(sections, "alpha", context_tokens=cost, passage_tokens=0)
        self.assertEqual([h["payload"]["text"][-1] for _, hits in out for h in hits], ["f"])

    def test_always_keeps_one_passage(self):
        out = fit_passages([("kb", [hit("alpha " * 500)])], "alpha", context_tokens=10, passage_tokens=0)
        self.assertEqual(len(out[0][1]), 1)

    def test_duplicate_text_kept_once(self):
        out = fit_passages([("a", [hit("same text")]), ("b", [hit("same text")])], "q",
                           context_tokens=1000, passage_tokens=0)
        self.assertEqual(sum(len(h) for _, h in out), 1)

    def test_passages_are_focused_without_touching_the_input(self):
        original = hit(FILLER * 10 + BATTERY + FILLER * 10)
        out = fit_passages([("kb", [original])], "battery backup", context_tokens=0, passage_tokens=60)
        self.assertIn("12 minutes", out[0][1][0]["payload"]["text"])
        self.assertEqual(out[0][1][0]["payload"]["original_file_name"], "a.md")
        self.assertNotIn("…", original["payload"]["text"])


class DropWeakHitsTest(unittest.TestCase):
    def test_off_by_default(self):
        hits = [("kb", [hit("a", 0.9), hit("b", 0.1)])]
        self.assertIs(drop_weak_hits(hits, 0.0), hits)

    def test_keeps_hits_near_the_best_across_sections(self):
        sections = [("kb", [hit("a", 0.80), hit("b", 0.65)]), ("category:x", [hit("c", 0.72), hit("d", 0.40)])]
        out = drop_weak_hits(sections, 0.10)
        self.assertEqual([[h["payload"]["text"] for h in hits] for _, hits in out], [["a"], ["c"]])
        self.assertEqual([label for label, _ in out], ["kb", "category:x"])

    def test_named_files_always_stay(self):
        sections = [("filename_match", [hit("f", 0.0)]), ("kb", [hit("a", 0.9)])]
        out = drop_weak_hits(sections, 0.05)
        self.assertEqual(out[0][1][0]["payload"]["text"], "f")


class RagPromptBudgetTest(unittest.TestCase):
    """The settings reach the /rag prompt and the Sources line."""

    class FakeLlm:
        def __init__(self):
            self.prompts = []

        def chat(self, prompt, **kwargs):
            self.prompts.append(prompt)
            return "answer"

    def answer(self, **limits):
        settings = dataclasses.replace(build_settings(), **limits)
        llm = self.FakeLlm()
        skill = RagRetrieveSkill(settings, {}, None, None, llm)
        sections = [
            ("kb", [hit(FILLER * 10 + BATTERY + FILLER * 10, 0.9, "power.md")]),
            ("category:x", [hit(FILLER * 30, 0.2, "garden.md")]),
        ]
        return skill._answer_from("how long does the battery backup last?", sections), llm.prompts[0]

    def test_default_sends_everything(self):
        answer, prompt = self.answer()
        self.assertIn("garden.md", answer)
        self.assertGreater(estimate_tokens(prompt), 1000)

    def test_relevance_margin_drops_unrelated_sources(self):
        answer, prompt = self.answer(rag_relevance_margin=0.10)
        self.assertNotIn("garden.md", prompt)
        self.assertTrue(answer.endswith("Sources: power.md"))

    def test_budget_trims_prompt_and_sources(self):
        answer, prompt = self.answer(rag_context_tokens=120, rag_passage_tokens=80)
        self.assertIn("12 minutes", prompt)
        self.assertNotIn("garden.md", prompt)
        self.assertTrue(answer.endswith("Sources: power.md"))
        self.assertLess(estimate_tokens(prompt), 250)


if __name__ == "__main__":
    unittest.main()
