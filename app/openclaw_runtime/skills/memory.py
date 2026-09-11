import re
import time
import uuid
from datetime import date

from openclaw_runtime.categories import registry_entries, resolve_category
from openclaw_runtime.config import Settings
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.http_client import is_reachable
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.skills.base import SkillResult


# Accept the half-width "#" and the full-width "＃" (common from CJK IMEs),
# and half/full-width brackets for multi-word names.
_CATEGORY_PREFIX_RE = re.compile(
    r"^[#＃]+[ \t]*(?:[\[［]([^\]］]+)[\]］]"
    r"|[\{｛]([^\}｝]+)[\}｝]"
    r"|(\S+))(?:\s+(.*))?$",
    re.DOTALL,
)
_ALL_CATEGORIES_TOKEN = "\x00all\x00"

# due:YYYY-MM-DD / tag:<word> anywhere in a /mem write, as standalone tokens
# (bounded by whitespace or string edges, so they don't match mid-word).
_DUE_TOKEN_RE = re.compile(r"(?<!\S)due:(\S+)(?!\S)", re.IGNORECASE)
_TAG_TOKEN_RE = re.compile(r"(?<!\S)tag:(\S+)(?!\S)", re.IGNORECASE)


def parse_memory_metadata(content: str) -> tuple[str, str | None, list[str]]:
    """Pull ``due:YYYY-MM-DD`` and ``tag:<word>`` tokens out of free text.

    Returns ``(clean_text, due_or_None, tags)``. Every ``tag:`` token is
    consumed. Only the first ``due:`` token that parses as a real ISO date is
    consumed as the due date; anything that doesn't parse (or a second due:)
    is left in the text untouched -- it likely wasn't meant as metadata.
    """
    tags: list[str] = []

    def _take_tag(match: re.Match) -> str:
        tags.append(match.group(1))
        return ""

    working = _TAG_TOKEN_RE.sub(_take_tag, content)

    due: str | None = None

    def _take_due(match: re.Match) -> str:
        nonlocal due
        if due is not None:
            return match.group(0)
        value = match.group(1)
        try:
            date.fromisoformat(value)
        except ValueError:
            return match.group(0)
        due = value
        return ""

    working = _DUE_TOKEN_RE.sub(_take_due, working)
    clean = " ".join(working.split())
    return clean, due, tags


def split_category_prefix(query: str) -> tuple[str | None, str]:
    """Pull a leading ``#category`` / ``#[multi word]`` / ``#all`` off a query.

    Returns ``(category_token_or_None, remaining_query)``. ``#all`` yields the
    internal all-categories sentinel. Also accepts the full-width ``＃``.
    """
    match = _CATEGORY_PREFIX_RE.match(query.strip())
    if not match:
        return None, query.strip()
    token = (match.group(1) or match.group(2) or match.group(3) or "").strip()
    rest = (match.group(4) or "").strip()
    if token.casefold() in {"all", "*", "全部", "所有"}:
        return _ALL_CATEGORIES_TOKEN, rest
    return token, rest


class MemoryWriteSkill:
    name = "memory_write"

    def __init__(self, settings: Settings, config: dict, embeddings: EmbeddingClient, qdrant: QdrantClient) -> None:
        self.settings = settings
        self.keywords = tuple(config.get("keywords", ["/mem", "/remember", "mem:", "remember:"]))
        self.embeddings = embeddings
        self.qdrant = qdrant

    def can_handle(self, text: str) -> bool:
        return any(text.startswith(keyword) for keyword in self.keywords)

    def health_check(self) -> str:
        if is_reachable(f"{self.settings.qdrant_base_url}/collections"):
            return "ready"
        return "error: Qdrant unreachable"

    def run(self, text: str) -> SkillResult:
        content = self._strip_command(text)
        if not content:
            return SkillResult(
                self.name,
                "Add the content to save after /mem, or use:\n"
                "/mem list [done]\n/mem done <id>\n/mem rm <id>\n"
                "Add due:YYYY-MM-DD and/or tag:<word> anywhere in the text to save them.",
            )
        stripped = content.strip()
        first_word, _, rest = stripped.partition(" ")
        keyword = first_word.lower()
        if keyword == "list":
            return self._list(rest.strip())
        if keyword == "done":
            return self._mark_done(rest.strip())
        if keyword in ("rm", "delete"):
            return self._remove(rest.strip())
        return self._write(content)

    def _write(self, content: str) -> SkillResult:
        clean_text, due, tags = parse_memory_metadata(content)
        if not clean_text:
            return SkillResult(self.name, "Add some content to remember, not just due:/tag: metadata.")

        point_id = str(uuid.uuid4())
        short_id = point_id.split("-")[0]
        now = int(time.time())
        metadata = {
            "source": "telegram",
            "kind": "tracker_memory",
            "short_id": short_id,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        if due:
            metadata["due"] = due
        if tags:
            metadata["tags"] = tags

        vector = self.embeddings.embed(clean_text)
        self.qdrant.upsert_text(
            self.settings.tracker_collection, clean_text, vector, metadata, point_id=point_id
        )

        bits = []
        if due:
            bits.append(f"due {due}")
        if tags:
            bits.append("tags: " + ", ".join(tags))
        suffix = f" ({'; '.join(bits)})" if bits else ""
        return SkillResult(self.name, f"Saved to {self.settings.tracker_collection}. Memory ID: {short_id}{suffix}")

    def _list(self, arg: str) -> SkillResult:
        status = "done" if arg.strip().lower() == "done" else "active"
        hits = self.qdrant.scroll_by_filters(
            self.settings.tracker_collection,
            {"kind": "tracker_memory", "status": status},
            limit=200,
        )
        if not hits:
            return SkillResult(self.name, f"No {'completed' if status == 'done' else 'active'} memory items.")

        def sort_key(hit: dict) -> tuple:
            payload = hit.get("payload") or {}
            return (payload.get("due") or "9999-99-99", -(payload.get("created_at") or 0))

        hits.sort(key=sort_key)
        label = "Completed" if status == "done" else "Active"
        lines = [f"{label} memory ({len(hits)}):"]
        for hit in hits[:50]:
            payload = hit.get("payload") or {}
            short_id = payload.get("short_id", "?")
            item_text = str(payload.get("text") or "").strip()
            bits = []
            if payload.get("due"):
                bits.append(f"due {payload['due']}")
            tags = payload.get("tags") or []
            if tags:
                bits.append("tags: " + ", ".join(tags))
            suffix = f" ({'; '.join(bits)})" if bits else ""
            lines.append(f"#{short_id} {item_text}{suffix}")
        if len(hits) > 50:
            lines.append(f"... and {len(hits) - 50} more.")
        return SkillResult(self.name, "\n".join(lines))

    def _mark_done(self, short_id: str) -> SkillResult:
        if not short_id:
            return SkillResult(self.name, "Usage: /mem done <id>")
        point = self._find_by_short_id(short_id)
        if not point:
            return SkillResult(self.name, f'No memory item with ID "{short_id}".')
        self.qdrant.set_payload(
            self.settings.tracker_collection,
            point["id"],
            {"status": "done", "updated_at": int(time.time())},
        )
        return SkillResult(self.name, f"Marked #{short_id} as done.")

    def _remove(self, short_id: str) -> SkillResult:
        if not short_id:
            return SkillResult(self.name, "Usage: /mem rm <id>")
        point = self._find_by_short_id(short_id)
        if not point:
            return SkillResult(self.name, f'No memory item with ID "{short_id}".')
        self.qdrant.delete_points(self.settings.tracker_collection, [point["id"]])
        return SkillResult(self.name, f"Deleted #{short_id}.")

    def _find_by_short_id(self, short_id: str) -> dict | None:
        hits = self.qdrant.scroll_by_filters(
            self.settings.tracker_collection,
            {"kind": "tracker_memory", "short_id": short_id},
            limit=1,
        )
        return hits[0] if hits else None

    @staticmethod
    def _strip_command(text: str) -> str:
        for prefix in ("/mem ", "/remember ", "mem:", "remember:"):
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
        if text in {"/mem", "/remember"}:
            return ""
        return text.strip()


class RagRetrieveSkill:
    name = "rag_retrieve"

    def __init__(
        self,
        settings: Settings,
        config: dict,
        embeddings: EmbeddingClient,
        qdrant: QdrantClient,
        llm: LlmClient,
    ) -> None:
        self.settings = settings
        self.keywords = tuple(config.get("keywords", ["/rag", "rag:"]))
        self.embeddings = embeddings
        self.qdrant = qdrant
        self.llm = llm
        self.model_policy = str(config.get("model_policy") or "").strip() or "local_default"

    @property
    def endpoint_id(self):
        return getattr(self.llm, "endpoint_id", None)

    def can_handle(self, text: str) -> bool:
        return any(keyword in text for keyword in self.keywords)

    def health_check(self) -> str:
        if not is_reachable(f"{self.settings.qdrant_base_url}/collections"):
            return "error: Qdrant unreachable"
        if not self.llm.is_reachable():
            return "error: vLLM unreachable"
        return "ready"

    def run(self, text: str) -> SkillResult:
        raw_query = self._strip_command(text)
        category_token, query = split_category_prefix(raw_query)
        if not query:
            return SkillResult(self.name, "Add the question to look up after /rag.")

        if self.settings.category_rag_enabled and category_token == _ALL_CATEGORIES_TOKEN:
            return self._run_all_categories(query)
        if self.settings.category_rag_enabled and category_token:
            return self._run_single_category(category_token, query)
        return self._run_default(query)

    def _run_default(self, query: str) -> SkillResult:
        file_hits = self._file_hits(
            query, [self.settings.knowledge_collection, self.settings.tracker_collection]
        )
        vector = self.embeddings.embed(query)
        tracker_hits = self.qdrant.search(self.settings.tracker_collection, vector)
        knowledge_hits = self.qdrant.search(self.settings.knowledge_collection, vector)
        answer = self._answer_from(
            query,
            [
                ("filename_match", file_hits),
                (self.settings.tracker_collection, tracker_hits),
                (self.settings.knowledge_collection, knowledge_hits),
            ],
        )
        if answer is None:
            return SkillResult(self.name, "No relevant memory was found in either Qdrant collection.")
        return SkillResult(self.name, answer)

    def _run_single_category(self, token: str, query: str) -> SkillResult:
        entry = resolve_category(self.settings, token)
        if not entry or not entry.get("known"):
            names = ", ".join(item["display"] for item in registry_entries(self.settings)) or "(none yet)"
            return SkillResult(
                self.name,
                f"No category named \"{token}\". Existing categories: {names}\n"
                "Query with /rag #<category>, or create one by captioning an upload #<category>.",
            )
        collection = entry["collection"]
        vector = self.embeddings.embed(query)
        try:
            hits = self.qdrant.search(collection, vector)
        except Exception:
            hits = []
        file_hits = self._file_hits(query, [collection])
        answer = self._answer_from(
            query, [("filename_match", file_hits), (f"category:{entry['display']}", hits)]
        )
        if answer is None:
            return SkillResult(
                self.name,
                f"Category \"{entry['display']}\" has no indexed content yet "
                "(if you just uploaded, give the indexer ~10 seconds).",
            )
        return SkillResult(self.name, answer)

    def _run_all_categories(self, query: str) -> SkillResult:
        entries = registry_entries(self.settings)
        if not entries:
            return SkillResult(self.name, "No categories have been created yet.")
        vector = self.embeddings.embed(query)
        sections = []
        for entry in entries:
            try:
                hits = self.qdrant.search(entry["collection"], vector, limit=3)
            except Exception:
                hits = []
            if hits:
                sections.append((f"category:{entry['display']}", hits))
        answer = self._answer_from(query, sections)
        if answer is None:
            return SkillResult(self.name, "No relevant content was found in any category.")
        return SkillResult(self.name, answer)

    def _answer_from(self, query: str, labelled_hits: list[tuple[str, list[dict]]]) -> str | None:
        context = self._format_context(labelled_hits)
        if not context:
            return None
        prompt = (
            "You are OpenClaw's local RAG assistant. Answer using only the Context below; "
            "if the Context is insufficient, say so explicitly. When you use a fact, name "
            "the source document it came from.\n\n"
            f"Question: {query}\n\n"
            f"Context:\n{context}"
        )
        answer = self.llm.chat(prompt, max_tokens=360)
        sources = self._collect_sources(labelled_hits)
        if sources:
            answer = f"{answer}\n\nSources: {', '.join(sources)}"
        return answer

    @staticmethod
    def _source_name(payload: dict) -> str | None:
        for key in ("original_file_name", "doc_title", "file_name"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return None

    def _collect_sources(self, labelled_hits: list[tuple[str, list[dict]]]) -> list[str]:
        seen: list[str] = []
        for _label, hits in labelled_hits:
            for hit in hits:
                payload = hit.get("payload") or {}
                if not str(payload.get("text") or "").strip():
                    continue
                name = self._source_name(payload)
                if name and name not in seen:
                    seen.append(name)
        return seen

    def _file_hits(self, query: str, collections: list[str]) -> list[dict]:
        file_names = re.findall(r"[\w.+-]+\.(?:pdf|odf|md|txt|log|json|csv|tsv)", query, flags=re.IGNORECASE)
        hits = []
        seen = set()
        for file_name in file_names:
            normalized = file_name.strip()
            for candidate in (normalized, normalized.replace(".odf", ".pdf")):
                if candidate in seen:
                    continue
                seen.add(candidate)
                for collection in collections:
                    try:
                        hits.extend(self.qdrant.scroll_by_file_name(collection, candidate, limit=10))
                    except Exception:
                        continue
        return hits

    def _format_context(self, labelled_hits: list[tuple[str, list[dict]]]) -> str:
        parts = []
        for label, hits in labelled_hits:
            for index, hit in enumerate(hits, start=1):
                payload = hit.get("payload") or {}
                text = str(payload.get("text") or "").strip()
                if not text:
                    continue
                score = hit.get("score", 0)
                source = self._source_name(payload)
                tag = f"{label} · {source}" if source else label
                parts.append(f"[{tag} #{index} score={score:.3f}] {text}")
        return "\n".join(parts)

    @staticmethod
    def _strip_command(text: str) -> str:
        for prefix in ("/rag ", "rag:"):
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
        return text.strip()
