from html import unescape
import re
import urllib.parse

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import get_text, is_reachable, request_json
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.skills.base import SkillResult


class WebSearchSkill:
    name = "web_search"

    def __init__(self, settings: Settings, config: dict, llm: LlmClient) -> None:
        self.settings = settings
        self.llm = llm
        self.keywords = tuple(
            config.get("keywords", ["搜尋", "查詢", "查一下", "最新", "新聞", "現在", "今天", "明天", "web:", "/search"])
        )
        self.limit = int(config.get("limit", 5))

    def can_handle(self, text: str) -> bool:
        return any(keyword in text for keyword in self.keywords)

    def health_check(self) -> str:
        if is_reachable(f"{self.settings.scraper_base_url}/health"):
            return "ready"
        return "degraded: browser scraper unreachable, falling back to DuckDuckGo HTML search"

    def run(self, text: str) -> SkillResult:
        scraper_answer = self._run_scraper(text)
        if scraper_answer:
            return SkillResult(self.name, scraper_answer)

        results = self._search(text)
        if not results:
            return SkillResult(self.name, "I tried searching the web, but no usable results came back.")
        context = "\n".join(
            f"{idx}. {item['title']}\nURL: {item['url']}\nSnippet: {item['snippet']}"
            for idx, item in enumerate(results, 1)
        )
        prompt = (
            f"User question: {text}\n\n"
            "Here are live web search results. Synthesize an answer from them, citing source titles "
            f"where relevant. Do not invent information beyond what's in the results.\n\n{context}"
        )
        return SkillResult(self.name, self.llm.chat(prompt, max_tokens=320))

    def _run_scraper(self, query: str) -> str:
        cleaned = query.replace("/search", "", 1).replace("web:", "", 1).strip()
        if not cleaned:
            return ""
        try:
            response = request_json(
                "POST",
                f"{self.settings.scraper_base_url}/scrape",
                {"query": cleaned, "limit": min(self.limit, self.settings.scraper_limit)},
                timeout=max(self.settings.web_timeout, 30),
            )
        except Exception as exc:
            print(f"[web_search] scraper unavailable, falling back: {exc}", flush=True)
            return ""
        if not response.get("ok"):
            print(f"[web_search] scraper error: {response}", flush=True)
            return ""
        result = response.get("result") or {}
        pages = result.get("results") or []
        if not pages:
            return ""
        context = self._build_scraper_context(pages)
        prompt = (
            f"User question: {query}\n\n"
            "The following content was scraped live by the local Playwright headless browser and "
            "saved as Markdown. Synthesize an answer that clearly cites its sources, and avoid "
            "inventing information not present in the scraped content.\n\n"
            f"Markdown file: {result.get('saved_path')}\n\n"
            f"{context}"
        )
        try:
            answer = self.llm.chat(prompt, max_tokens=420)
        except Exception as exc:
            print(f"[web_search] llm summary failed: {exc}", flush=True)
            sources = "\n".join(
                f"{idx}. {item.get('title')}\n{item.get('url')}"
                for idx, item in enumerate(pages, 1)
            )
            return (
                "Web search completed and results saved, but the local model could not summarize them right now.\n\n"
                f"{sources}\n\n"
                f"Saved web Markdown: {result.get('saved_path')}"
            )
        return f"{answer}\n\nSaved web Markdown: {result.get('saved_path')}"

    def _build_scraper_context(self, pages: list[dict]) -> str:
        budget = max(900, int(self.settings.web_context_chars))
        per_page = max(500, budget // max(len(pages), 1))
        blocks = []
        for idx, item in enumerate(pages, 1):
            markdown = str(item.get("markdown") or "")
            blocks.append(
                f"Source {idx}: {item.get('title')}\n"
                f"URL: {item.get('url')}\n\n"
                f"{markdown[:per_page]}"
            )
        return "\n\n".join(blocks)

    def _search(self, query: str) -> list[dict[str, str]]:
        cleaned = query.replace("/search", "", 1).replace("web:", "", 1).strip()
        url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": cleaned})
        html = get_text(url, timeout=self.settings.web_timeout)
        results: list[dict[str, str]] = []
        pattern = re.compile(
            r'<a rel="nofollow" class="result__a" href="(?P<href>.*?)".*?>(?P<title>.*?)</a>.*?'
            r'<a class="result__snippet".*?>(?P<snippet>.*?)</a>',
            re.DOTALL,
        )
        for match in pattern.finditer(html):
            href = unescape(re.sub("<.*?>", "", match.group("href")))
            title = unescape(re.sub("<.*?>", "", match.group("title"))).strip()
            snippet = unescape(re.sub("<.*?>", "", match.group("snippet"))).strip()
            parsed = urllib.parse.urlparse(href)
            params = urllib.parse.parse_qs(parsed.query)
            if "uddg" in params:
                href = params["uddg"][0]
            if title:
                results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= self.limit:
                break
        return results
