"""Category RAG helpers.

A "category" is a user-supplied label (often CJK) that maps to its own
dedicated Qdrant collection so different kinds of uploaded material stay
isolated at retrieval time. This module owns the pure, testable pieces:

* turning a display name into a filesystem- and Qdrant-safe slug
* deriving the collection name for a slug
* a small on-disk registry (display name <-> slug <-> collection) that lives
  on the shared /workspace volume so both the gateway and the memory watcher
  can read and update it.
"""

import hashlib
import json
import os
import re
import time

from openclaw_runtime.config import Settings


_ASCII_SLUG_RE = re.compile(r"[^a-z0-9]+")
_REGISTRY_VERSION = 1

_CAPTION_CATEGORY_RE = re.compile(
    r"^[#＃]+[ \t]*(?:[\[［]([^\]］]+)[\]］]"
    r"|[\{｛]([^\}｝]+)[\}｝]"
    r"|(\S+))(?:\s+(.*))?$",
    re.DOTALL,
)


def parse_category_caption(caption: str) -> tuple[str | None, str]:
    """Parse a Telegram caption of the form ``#category optional note``.

    Returns ``(category_name_or_None, note)``. ``#[multi word]`` and
    ``#{multi word}`` carry a category name containing spaces. The full-width
    ``＃`` (common from CJK IMEs) is accepted too.
    """
    match = _CAPTION_CATEGORY_RE.match((caption or "").strip())
    if not match:
        return None, ""
    name = (match.group(1) or match.group(2) or match.group(3) or "").strip()
    note = (match.group(4) or "").strip()
    return (name or None), note


def normalize_category_name(name: str) -> str:
    """Collapse whitespace; used for both display and hashing."""
    return " ".join((name or "").strip().split())


def _ascii_slug(name: str) -> str:
    lowered = normalize_category_name(name).lower()
    return _ASCII_SLUG_RE.sub("-", lowered).strip("-")[:32]


def category_hash(name: str) -> str:
    normalized = normalize_category_name(name).lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]


def category_slug(name: str) -> str:
    """Stable id for a category display name.

    ``<ascii-part-or-x>_<8 hex of sha1(normalized name)>`` -- the hash suffix
    keeps CJK-only names (whose ascii part collapses to nothing) unique and
    stable across restarts.
    """
    ascii_part = _ascii_slug(name) or "x"
    return f"{ascii_part}_{category_hash(name)}"


def is_category_slug(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9-]{1,32}_[0-9a-f]{8}", value or ""))


def category_collection_name(settings: Settings, slug: str) -> str:
    return f"{settings.category_collection_prefix}{slug}"


def is_category_collection(settings: Settings, collection: str) -> bool:
    return collection.startswith(settings.category_collection_prefix)


def validate_category_name(settings: Settings, name: str) -> str:
    """Return the normalized name or raise ValueError with a user-facing reason."""
    normalized = normalize_category_name(name)
    if not normalized:
        raise ValueError("category name is empty")
    if len(normalized) > settings.category_max_name_chars:
        raise ValueError(
            f"category name is too long (max {settings.category_max_name_chars} chars)"
        )
    if normalized.startswith("/"):
        raise ValueError("category name must not start with '/'")
    return normalized


# --- registry -----------------------------------------------------------------


def _empty_registry() -> dict:
    return {"version": _REGISTRY_VERSION, "categories": {}}


def load_registry(settings: Settings) -> dict:
    path = settings.category_registry_path
    if not path.exists():
        return _empty_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_registry()
    if not isinstance(data, dict) or not isinstance(data.get("categories"), dict):
        return _empty_registry()
    data.setdefault("version", _REGISTRY_VERSION)
    return data


def _save_registry(settings: Settings, registry: dict) -> None:
    path = settings.category_registry_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def registry_entries(settings: Settings) -> list[dict]:
    registry = load_registry(settings)
    entries = []
    for slug, entry in sorted(registry["categories"].items()):
        entries.append(
            {
                "slug": slug,
                "display": entry.get("display", slug),
                "collection": entry.get(
                    "collection", category_collection_name(settings, slug)
                ),
                "created_at": entry.get("created_at"),
                "updated_at": entry.get("updated_at"),
            }
        )
    return entries


def upsert_registry_entry(settings: Settings, display_name: str) -> dict:
    """Register (or refresh) a category by display name; returns its entry.

    Re-reads the file immediately before writing so a concurrent writer in the
    other container only ever loses a timestamp refresh, never an entry.
    """
    normalized = normalize_category_name(display_name)
    slug = category_slug(normalized)
    collection = category_collection_name(settings, slug)
    now = int(time.time())

    registry = load_registry(settings)
    existing = registry["categories"].get(slug, {})
    registry["categories"][slug] = {
        "display": normalized or existing.get("display", slug),
        "collection": collection,
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }
    _save_registry(settings, registry)
    return {"slug": slug, "display": normalized or slug, "collection": collection}


def ensure_registry_entry_for_slug(settings: Settings, slug: str) -> dict:
    """Backfill an entry for a slug seen on disk without a display name."""
    registry = load_registry(settings)
    if slug in registry["categories"]:
        entry = registry["categories"][slug]
        return {
            "slug": slug,
            "display": entry.get("display", slug),
            "collection": entry.get(
                "collection", category_collection_name(settings, slug)
            ),
        }
    now = int(time.time())
    collection = category_collection_name(settings, slug)
    registry["categories"][slug] = {
        "display": slug,
        "collection": collection,
        "created_at": now,
        "updated_at": now,
    }
    _save_registry(settings, registry)
    return {"slug": slug, "display": slug, "collection": collection}


def resolve_category(settings: Settings, token: str) -> dict | None:
    """Resolve a user token (display name or slug) to a registry entry.

    Falls back to a computed entry when the token looks like a brand-new
    category name that just hasn't been registered yet.
    """
    token = normalize_category_name(token)
    if not token:
        return None

    registry = load_registry(settings)
    categories = registry["categories"]

    def _from(slug: str, entry: dict, display_fallback: str) -> dict:
        return {
            "slug": slug,
            "display": entry.get("display", display_fallback),
            "collection": entry.get("collection", category_collection_name(settings, slug)),
            "known": True,
        }

    if token in categories:
        return _from(token, categories[token], token)

    lowered = token.casefold()
    for slug, entry in categories.items():
        if entry.get("display", "").casefold() == lowered or slug.casefold() == lowered:
            return _from(slug, entry, slug)

    computed_slug = category_slug(token)
    if computed_slug in categories:
        return _from(computed_slug, categories[computed_slug], token)

    return {
        "slug": computed_slug,
        "display": token,
        "collection": category_collection_name(settings, computed_slug),
        "known": False,
    }
