import unittest
from unittest.mock import patch

from openclaw_runtime.skills.web_search import WebSearchSkill

from tests.support import build_settings


class FakeLlm:
    def __init__(self, answer: str = "answer-from-context") -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def chat(self, prompt: str, *, max_tokens: int | None = None) -> str:
        self.prompts.append(prompt)
        return self.answer


def _scrape_response(pages: list[dict] | None = None) -> dict:
    return {
        "ok": True,
        "result": {
            "saved_path": "/workspace/inbox/tracker/web/x.md",
            "results": pages or [{"title": "Example", "url": "https://example.com", "markdown": "body text"}],
        },
    }


class RunScraperUrlDetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = build_settings()
        self.llm = FakeLlm()
        self.skill = WebSearchSkill(self.settings, {}, self.llm)

    @patch("openclaw_runtime.skills.web_search.request_json")
    def test_bare_url_is_sent_as_direct_url_not_a_search_query(self, request_json) -> None:
        request_json.return_value = _scrape_response()
        self.skill._run_scraper("/search https://example.com/article")
        payload = request_json.call_args.args[2]
        self.assertEqual(payload["url"], "https://example.com/article")
        self.assertNotIn("query", payload)

    @patch("openclaw_runtime.skills.web_search.request_json")
    def test_url_with_a_trailing_question_still_extracts_just_the_url(self, request_json) -> None:
        request_json.return_value = _scrape_response()
        self.skill._run_scraper("/search https://example.com/article 這篇在講什麼")
        payload = request_json.call_args.args[2]
        self.assertEqual(payload["url"], "https://example.com/article")

    @patch("openclaw_runtime.skills.web_search.request_json")
    def test_url_leading_the_question_still_extracts_just_the_url(self, request_json) -> None:
        request_json.return_value = _scrape_response()
        self.skill._run_scraper("summarise https://example.com/report.pdf for me")
        payload = request_json.call_args.args[2]
        self.assertEqual(payload["url"], "https://example.com/report.pdf")

    @patch("openclaw_runtime.skills.web_search.request_json")
    def test_plain_text_with_no_url_is_sent_as_a_search_query(self, request_json) -> None:
        request_json.return_value = _scrape_response()
        self.skill._run_scraper("/search latest Arm Neoverse news")
        payload = request_json.call_args.args[2]
        self.assertEqual(payload["query"], "latest Arm Neoverse news")
        self.assertNotIn("url", payload)

    @patch("openclaw_runtime.skills.web_search.request_json")
    def test_full_original_text_still_used_as_the_question_for_the_summary(self, request_json) -> None:
        request_json.return_value = _scrape_response()
        self.skill._run_scraper("/search https://example.com/article 這篇在講什麼")
        self.assertIn("這篇在講什麼", self.llm.prompts[0])

    @patch("openclaw_runtime.skills.web_search.request_json")
    def test_limit_is_still_included_alongside_url(self, request_json) -> None:
        request_json.return_value = _scrape_response()
        self.skill._run_scraper("/search https://example.com")
        payload = request_json.call_args.args[2]
        self.assertIn("limit", payload)


if __name__ == "__main__":
    unittest.main()
