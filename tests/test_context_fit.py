import unittest

from openclaw_runtime.llm_client import TRIM_MARKER, estimate_tokens, fit_to_context
from openclaw_runtime.model_catalog import ModelSpec


class FitToContextTest(unittest.TestCase):
    def _payload(self, user: str, max_tokens: int = 500) -> dict:
        return {"messages": [{"role": "system", "content": "You are OpenClaw."},
                             {"role": "user", "content": user}], "max_tokens": max_tokens}

    def test_off_and_small_prompts_are_untouched(self) -> None:
        big = "word " * 5000
        self.assertEqual(fit_to_context(self._payload(big), 0)["messages"][1]["content"], big)
        small = self._payload("Question: what is x?\n\nContext: short.")
        self.assertEqual(fit_to_context(small, 8192)["messages"][1]["content"], "Question: what is x?\n\nContext: short.")
        self.assertEqual(small["max_tokens"], 500)

    def test_long_context_is_cut_from_the_middle_keeping_start_and_end(self) -> None:
        user = "INSTRUCTIONS FIRST. " + ("transcript line. " * 3000) + " QUESTION LAST?"
        payload = fit_to_context(self._payload(user, 800), 4096)
        text = payload["messages"][1]["content"]
        self.assertTrue(text.startswith("INSTRUCTIONS FIRST."))
        self.assertTrue(text.endswith("QUESTION LAST?"))
        self.assertIn(TRIM_MARKER.strip(), text)
        total = sum(estimate_tokens(m["content"]) + 4 for m in payload["messages"]) + payload["max_tokens"]
        self.assertLessEqual(total, 4096)

    def test_cjk_counts_more_and_images_are_budgeted(self) -> None:
        self.assertGreater(estimate_tokens("中文" * 100), estimate_tokens("ab" * 100))
        payload = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "Transcribe. " + "x" * 30000},
            {"type": "image_url", "image_url": {"url": "data:..."}}]}], "max_tokens": 4096}
        fit_to_context(payload, 8192)
        self.assertLess(len(payload["messages"][0]["content"][0]["text"]), 30000)
        self.assertGreaterEqual(payload["max_tokens"], 128)

    def test_catalog_context_tokens(self) -> None:
        spec = ModelSpec.from_dict("local_default", {"base_url": "http://x/v1", "model": "m", "roles": ["general"],
                                                     "context_tokens": 16384}, default_timeout=30)
        self.assertEqual(spec.context_tokens, 16384)
        with self.assertRaises(ValueError):
            ModelSpec.from_dict("m", {"base_url": "http://x", "model": "m", "roles": ["general"],
                                      "context_tokens": -1}, default_timeout=30)


if __name__ == "__main__":
    unittest.main()
