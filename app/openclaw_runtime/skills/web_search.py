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
            return SkillResult(self.name, "我有嘗試上網搜尋，但沒有取得可用的搜尋結果。")
        context = "\n".join(
            f"{idx}. {item['title']}\nURL: {item['url']}\n摘要: {item['snippet']}"
            for idx, item in enumerate(results, 1)
        )
        prompt = (
            f"使用者問題：{text}\n\n"
            "以下是即時網路搜尋結果，請用繁體中文整合回答，必要時提到資料來源標題。"
            f"不要編造搜尋結果以外的資訊。\n\n{context}"
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
            f"使用者問題：{query}\n\n"
            "以下內容由本地 Playwright headless browser 即時抓取並已落盤為 Markdown。"
            "請用繁體中文回答，明確整合來源，避免編造未出現在抓取內容中的資訊。\n\n"
            f"Markdown 檔案：{result.get('saved_path')}\n\n"
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
                "我已經完成網頁搜尋並保存結果，但本地模型暫時無法整理摘要。\n\n"
                f"{sources}\n\n"
                f"已保存網頁 Markdown：{result.get('saved_path')}"
            )
        return f"{answer}\n\n已保存網頁 Markdown：{result.get('saved_path')}"

    def _build_scraper_context(self, pages: list[dict]) -> str:
        budget = max(900, int(self.settings.web_context_chars))
        per_page = max(500, budget // max(len(pages), 1))
        blocks = []
        for idx, item in enumerate(pages, 1):
            markdown = str(item.get("markdown") or "")
            blocks.append(
                f"來源 {idx}: {item.get('title')}\n"
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
