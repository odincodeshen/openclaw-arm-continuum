import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import USER_AGENT


GOOGLE_DOC_ID_RE = re.compile(r"https?://docs\.google\.com/document/(?:u/\d+/)?d/([^/?#]+)")
GOOGLE_DOC_PUBLISHED_ID_RE = re.compile(r"https?://docs\.google\.com/document/d/e/([^/?#]+)")


@dataclass(frozen=True)
class SourceIngestResult:
    path: Path
    title: str
    source_url: str
    export_url: str
    collection_kind: str
    char_count: int


def is_google_doc_url(url: str) -> bool:
    return bool(GOOGLE_DOC_ID_RE.search(url) or GOOGLE_DOC_PUBLISHED_ID_RE.search(url))


def google_doc_export_url(url: str) -> str:
    published = GOOGLE_DOC_PUBLISHED_ID_RE.search(url)
    if published:
        doc_id = published.group(1)
        return f"https://docs.google.com/document/d/e/{doc_id}/pub?output=txt"

    match = GOOGLE_DOC_ID_RE.search(url)
    if not match:
        raise ValueError("This is not a supported Google Docs document URL")
    doc_id = match.group(1)
    return f"https://docs.google.com/document/d/{doc_id}/export?format=txt"


def fetch_public_text(url: str, timeout: int) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        charset = _charset_from_content_type(content_type) or "utf-8"
        data = response.read()
    text = data.decode(charset, errors="replace").strip()
    if _looks_like_google_access_error(text):
        raise RuntimeError("The Google Doc may not be publicly readable, or requires a Google account login")
    if not text:
        raise RuntimeError("The Google Doc export did not return any text content")
    return text


def save_google_doc(settings: Settings, url: str, collection_kind: str = "knowledge") -> SourceIngestResult:
    normalized_kind = collection_kind.lower().strip()
    if normalized_kind not in {"knowledge", "tracker"}:
        raise ValueError("collection_kind must be knowledge or tracker")
    if not is_google_doc_url(url):
        raise ValueError("The current /doc url implementation only supports Google Docs document links")

    export_url = google_doc_export_url(url)
    text = fetch_public_text(export_url, timeout=max(settings.request_timeout, 60))
    title = _title_from_text(text) or "Google Doc"
    markdown = _to_markdown(title, url, export_url, text)

    directory = settings.inbox_path / normalized_kind / "google-docs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = _unique_path(directory / f"{stamp}-google-doc-{_safe_slug(title)}.md")
    path.write_text(markdown, encoding="utf-8")
    return SourceIngestResult(
        path=path,
        title=title,
        source_url=url,
        export_url=export_url,
        collection_kind=normalized_kind,
        char_count=len(text),
    )


def _charset_from_content_type(content_type: str) -> str:
    for part in content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip()
    return ""


def _looks_like_google_access_error(text: str) -> bool:
    lowered = text[:4000].lower()
    return (
        "sign in" in lowered
        and "google" in lowered
        or "you need permission" in lowered
        or "request access" in lowered
        or "document is not published" in lowered
    )


def _title_from_text(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return ""


def _to_markdown(title: str, source_url: str, export_url: str, text: str) -> str:
    return (
        f"# {title}\n\n"
        f"Source: {source_url}\n"
        f"Export: {export_url}\n"
        f"Imported: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n\n"
        "## Document Text\n\n"
        f"{text.strip()}\n"
    )


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())[:64].strip("-")
    return slug or "untitled"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot allocate unique filename for {path}")
