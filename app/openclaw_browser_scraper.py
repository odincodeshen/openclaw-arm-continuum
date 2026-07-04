#!/usr/bin/env python3
import json
import os
import re
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


HOST = os.environ.get("OPENCLAW_SCRAPER_HOST", "0.0.0.0")
PORT = int(os.environ.get("OPENCLAW_SCRAPER_PORT", "8787"))
WORKSPACE_ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT", "/workspace")).resolve()
DEFAULT_LIMIT = int(os.environ.get("OPENCLAW_SCRAPER_LIMIT", "3"))
TIMEOUT_MS = int(os.environ.get("OPENCLAW_SCRAPER_TIMEOUT_MS", "20000"))
CHROMIUM_PATH = os.environ.get("OPENCLAW_CHROMIUM_PATH", "/usr/bin/chromium")

_playwright = None
_browser = None


def get_browser():
    """Reuse one Chromium process across requests instead of a cold launch per scrape.

    HTTPServer (not ThreadingHTTPServer) handles requests one at a time on the
    same thread, which is required by Playwright's sync API (not thread-safe).
    """
    global _playwright, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
    if _playwright is None:
        _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(
        executable_path=CHROMIUM_PATH,
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    return _browser


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    print(f"{stamp} {message}", flush=True)


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())[:80].strip("-")
    return slug or "scrape"


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url
    raise ValueError("url must start with http:// or https://")


def search_url(query: str) -> str:
    return "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})


def bing_search_url(query: str) -> str:
    return "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query})


def extract_search_results(page, limit: int) -> list[dict]:
    links = page.eval_on_selector_all(
        "a.result__a, a[data-testid='result-title-a'], a[href]",
        """els => els.map(a => ({title: (a.innerText || a.textContent || '').trim(), href: a.href})).filter(x => x.title && x.href)""",
    )
    results = []
    seen = set()
    for item in links:
        href = item.get("href", "")
        parsed = urllib.parse.urlparse(href)
        params = urllib.parse.parse_qs(parsed.query)
        if "uddg" in params:
            href = params["uddg"][0]
        if not href.startswith(("http://", "https://")):
            continue
        host = urllib.parse.urlparse(href).netloc
        if "duckduckgo.com" in host:
            continue
        if href in seen:
            continue
        seen.add(href)
        results.append({"title": item.get("title", "").strip(), "url": href})
        if len(results) >= limit:
            break
    return results


def extract_bing_results(page, limit: int) -> list[dict]:
    links = page.eval_on_selector_all(
        "li.b_algo h2 a[href], h2 a[href], a[href]",
        """els => els.map(a => ({title: (a.innerText || a.textContent || '').trim(), href: a.href})).filter(x => x.title && x.href)""",
    )
    results = []
    seen = set()
    blocked_hosts = {"www.bing.com", "bing.com", "go.microsoft.com", "login.live.com"}
    for item in links:
        href = item.get("href", "")
        if not href.startswith(("http://", "https://")):
            continue
        host = urllib.parse.urlparse(href).netloc.lower()
        if host in blocked_hosts or host.endswith(".bing.com"):
            continue
        if href in seen:
            continue
        seen.add(href)
        results.append({"title": item.get("title", "").strip(), "url": href})
        if len(results) >= limit:
            break
    return results


def page_to_markdown(page, url: str, title_hint: str = "") -> dict:
    title = (page.title() or title_hint or url).strip()
    data = page.evaluate(
        """() => {
            const selectors = ['script','style','noscript','svg','canvas','iframe','nav','footer'];
            const clone = document.body ? document.body.cloneNode(true) : document.documentElement.cloneNode(true);
            selectors.forEach(sel => clone.querySelectorAll(sel).forEach(el => el.remove()));
            const headings = [...clone.querySelectorAll('h1,h2,h3')].map(h => ({
                level: h.tagName.toLowerCase(),
                text: (h.innerText || h.textContent || '').trim()
            })).filter(x => x.text).slice(0, 24);
            const links = [...clone.querySelectorAll('a[href]')].map(a => ({
                text: (a.innerText || a.textContent || '').trim(),
                href: a.href
            })).filter(x => x.text && x.href).slice(0, 40);
            const text = (clone.innerText || clone.textContent || '').replace(/\\n{3,}/g, '\\n\\n').trim();
            return {text, headings, links};
        }"""
    )
    lines = [f"# {title}", "", f"URL: {url}", ""]
    if data.get("headings"):
        lines.append("## Headings")
        for heading in data["headings"]:
            prefix = {"h1": "-", "h2": "  -", "h3": "    -"}.get(heading["level"], "-")
            lines.append(f"{prefix} {heading['text']}")
        lines.append("")
    lines.append("## Page Text")
    lines.append((data.get("text") or "")[:20000])
    if data.get("links"):
        lines.append("")
        lines.append("## Links")
        for link in data["links"]:
            lines.append(f"- [{link['text']}]({link['href']})")
    return {"title": title, "url": url, "markdown": "\n".join(lines).strip() + "\n"}


def save_markdown(kind: str, name: str, markdown: str) -> str:
    directory = WORKSPACE_ROOT / "inbox" / "tracker" / "web"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}-{kind}-{safe_slug(name)}.md"
    path.write_text(markdown, encoding="utf-8")
    return str(path)


def scrape(payload: dict) -> dict:
    query = str(payload.get("query") or "").strip()
    url = str(payload.get("url") or "").strip()
    limit = max(1, min(int(payload.get("limit") or DEFAULT_LIMIT), 6))
    if not query and not url:
        raise ValueError("query or url is required")

    started = time.time()
    browser = get_browser()
    context = browser.new_context(
        ignore_https_errors=True,
        user_agent=(
            "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 OpenClaw-Arm-Continuum"
        ),
    )
    pages = []
    try:
        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)
        if url:
            target = normalize_url(url)
            page.goto(target, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            pages.append(page_to_markdown(page, target))
        else:
            page.goto(search_url(query), wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            results = extract_search_results(page, limit)
            if not results:
                page.goto(bing_search_url(query), wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                results = extract_bing_results(page, limit)
            for result in results:
                try:
                    page.goto(result["url"], wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                    pages.append(page_to_markdown(page, result["url"], result["title"]))
                except Exception as exc:
                    pages.append(
                        {
                            "title": result["title"],
                            "url": result["url"],
                            "markdown": f"# {result['title']}\n\nURL: {result['url']}\n\nScrape failed: {exc}\n",
                        }
                    )
    finally:
        context.close()

    combined = [
        f"# OpenClaw Web Scrape",
        "",
        f"- Query: {query or '(direct url)'}",
        f"- URL: {url or '(search)'}",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    for index, item in enumerate(pages, 1):
        combined.append(f"---\n\n# Result {index}: {item['title']}\n")
        combined.append(item["markdown"])
    markdown = "\n".join(combined).strip() + "\n"
    saved_path = save_markdown("search" if query else "url", query or url, markdown)
    return {
        "query": query,
        "url": url,
        "duration_seconds": round(time.time() - started, 3),
        "saved_path": saved_path,
        "results": [{"title": item["title"], "url": item["url"], "markdown": item["markdown"][:6000]} for item in pages],
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/scrape":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            result = scrape(payload)
            self._json(200, {"ok": True, "result": result})
        except Exception as exc:
            log(traceback.format_exc())
            self._json(500, {"ok": False, "error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        log(format % args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    log(f"[scraper] listening on {HOST}:{PORT} workspace={WORKSPACE_ROOT}")
    HTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
