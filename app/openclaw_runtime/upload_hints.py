"""Hints for a file waiting for its category: which category it most
likely belongs to, and whether the same file is already saved.

Standard library only (the Telegram container has no PDF library): a PDF's
opening text comes from inflating its content streams with zlib and
reading the text-showing operators -- good enough for a hint, and when it
can't read a PDF the file name alone is used.
"""

import hashlib
import json
import re
import zlib
from pathlib import Path

PREVIEW_CHARS = 1500
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_TEXT_OP = re.compile(rb"\((.*?)(?<!\\)\)\s*Tj|\[(.*?)\]\s*TJ", re.S)
_TJ_PART = re.compile(rb"\((.*?)(?<!\\)\)", re.S)


def _pdf_string(raw: bytes) -> str:
    raw = raw.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
    return raw.decode("latin-1", errors="ignore")


def pdf_preview_text(path: Path, limit: int = PREVIEW_CHARS) -> str:
    try:
        data = path.read_bytes()[:5_000_000]
    except OSError:
        return ""
    pieces: list[str] = []
    for match in _STREAM.finditer(data):
        body = match.group(1)
        try:
            body = zlib.decompress(body)
        except zlib.error:
            pass
        for op in _TEXT_OP.finditer(body):
            if op.group(1) is not None:
                pieces.append(_pdf_string(op.group(1)))
            else:
                pieces.append("".join(_pdf_string(p) for p in _TJ_PART.findall(op.group(2))))
            if sum(len(p) for p in pieces) >= limit:
                break
        if sum(len(p) for p in pieces) >= limit:
            break
    text = " ".join(" ".join(pieces).split())
    return text[:limit] if looks_like_text(text) else ""


def looks_like_text(text: str) -> bool:
    """Reject what comes out of PDFs whose fonts use private encodings:
    printable, but symbols rather than words."""
    if not text:
        return False
    words = re.findall(r"[A-Za-z]{3,}", text)
    letters = sum(ch.isalpha() or ch.isspace() for ch in text)
    return len(words) >= 5 and letters / len(text) > 0.7


def preview_text(path: Path, limit: int = PREVIEW_CHARS) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return pdf_preview_text(path, limit)
    if suffix in (".md", ".txt", ".csv", ".tsv", ".json", ".log"):
        try:
            return path.read_text(encoding="utf-8", errors="ignore")[:limit]
        except OSError:
            return ""
    return ""


SUGGEST_SCHEMA_NAME = "category_suggestion"


def suggest_category(llm, file_name: str, text: str, categories: dict[str, list[str]]) -> str | None:
    """categories: display name -> a few file names already in it. Returns a
    display name, or None when nothing fits well (or the model fails)."""
    names = list(categories)
    if len(names) < 2:
        return None
    listing = "\n".join(
        f"- {name}: " + (", ".join(files[:5]) if files else "(no files yet)") for name, files in categories.items()
    )
    prompt = (
        "A file is being saved into one of these knowledge categories (with a few file names already in each):\n"
        f"{listing}\n\n"
        f"File name: {file_name}\n"
        f"Opening text: {text[:PREVIEW_CHARS] or '(not readable)'}\n\n"
        "Which category fits it best? Answer \"none\" if none clearly fits."
    )
    schema = {
        "type": "object",
        "properties": {"category": {"type": "string", "enum": names + ["none"]}},
        "required": ["category"],
        "additionalProperties": False,
    }
    try:
        choice = json.loads(llm.chat_json(prompt, schema, schema_name=SUGGEST_SCHEMA_NAME, max_tokens=60))["category"]
    except Exception:  # noqa: BLE001 - a hint is optional
        return None
    return choice if choice in names else None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


_HASH_CACHE: dict[tuple[str, float, int], str] = {}


def _cached_sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_mtime, stat.st_size)
    if key not in _HASH_CACHE:
        _HASH_CACHE[key] = file_sha256(path)
    return _HASH_CACHE[key]


def find_duplicate(path: Path, places: dict[str, Path]) -> tuple[str, str] | None:
    """places: label (e.g. "#aitool", "knowledge base") -> folder. Returns
    (label, stored file name) of a file with the same bytes, if any."""
    try:
        size = path.stat().st_size
        digest = None
        for label, folder in places.items():
            if not folder.is_dir():
                continue
            for candidate in folder.rglob("*"):
                if not candidate.is_file() or candidate.name.endswith(".meta.json"):
                    continue
                if candidate.stat().st_size != size:
                    continue
                digest = digest or file_sha256(path)
                if _cached_sha256(candidate) == digest:
                    return label, candidate.name
    except OSError:
        return None
    return None
