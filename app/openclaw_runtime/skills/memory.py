import re

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
            return SkillResult(self.name, "Add the content to save after /mem.")
        vector = self.embeddings.embed(content)
        point_id = self.qdrant.upsert_text(
            self.settings.tracker_collection,
            content,
            vector,
            {"source": "telegram", "kind": "tracker_memory"},
        )
        short_id = point_id.split("-")[0]
        return SkillResult(self.name, f"Saved to {self.settings.tracker_collection}. Memory ID: {short_id}")

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
        context = self._format_context(
            [
                ("filename_match", file_hits),
                (self.settings.tracker_collection, tracker_hits),
                (self.settings.knowledge_collection, knowledge_hits),
            ]
        )
        if not context:
            return SkillResult(self.name, "No relevant memory was found in either Qdrant collection.")
        return SkillResult(self.name, self._answer(query, context))

    def _run_single_category(self, token: str, query: str) -> SkillResult:
        entry = resolve_category(self.settings, token)
        known = list(registry_entries(self.settings))
        if not entry or not entry.get("known"):
            names = "、".join(item["display"] for item in known) or "（尚無任何類別）"
            return SkillResult(
                self.name,
                f"找不到類別「{token}」。目前的類別：{names}\n"
                "（用 /rag #<類別> 查詢，或上傳檔案時用 #<類別> 建立。）",
            )
        collection = entry["collection"]
        vector = self.embeddings.embed(query)
        try:
            hits = self.qdrant.search(collection, vector)
        except Exception:
            hits = []
        file_hits = self._file_hits(query, [collection])
        context = self._format_context(
            [("filename_match", file_hits), (f"category:{entry['display']}", hits)]
        )
        if not context:
            return SkillResult(
                self.name,
                f"類別「{entry['display']}」目前還沒有可檢索的內容"
                "（剛上傳的話等 10 秒左右讓索引器處理）。",
            )
        return SkillResult(self.name, self._answer(query, context))

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
        context = self._format_context(sections)
        if not context:
            return SkillResult(self.name, "No relevant content was found in any category.")
        return SkillResult(self.name, self._answer(query, context))

    def _answer(self, query: str, context: str) -> str:
        prompt = (
            "You are OpenClaw's local RAG assistant. Answer using only the Context below; "
            "if the Context is insufficient, say so explicitly.\n\n"
            f"Question: {query}\n\n"
            f"Context:\n{context}"
        )
        return self.llm.chat(prompt, max_tokens=360)

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
                if text:
                    score = hit.get("score", 0)
                    parts.append(f"[{label} #{index} score={score:.3f}] {text}")
        return "\n".join(parts)

    @staticmethod
    def _strip_command(text: str) -> str:
        for prefix in ("/rag ", "rag:"):
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
        return text.strip()
