import re

from openclaw_runtime.config import Settings
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.skills.base import SkillResult


class MemoryWriteSkill:
    name = "memory_write"

    def __init__(self, settings: Settings, config: dict, embeddings: EmbeddingClient, qdrant: QdrantClient) -> None:
        self.settings = settings
        self.keywords = tuple(config.get("keywords", ["/mem", "/remember", "mem:", "remember:"]))
        self.embeddings = embeddings
        self.qdrant = qdrant

    def can_handle(self, text: str) -> bool:
        return any(text.startswith(keyword) for keyword in self.keywords)

    def run(self, text: str) -> SkillResult:
        content = self._strip_command(text)
        if not content:
            return SkillResult(self.name, "請在 /mem 後面加上要保存的內容。")
        vector = self.embeddings.embed(content)
        point_id = self.qdrant.upsert_text(
            self.settings.tracker_collection,
            content,
            vector,
            {"source": "telegram", "kind": "tracker_memory"},
        )
        short_id = point_id.split("-")[0]
        return SkillResult(self.name, f"已寫入 personal_tracker_memory。記憶 ID：{short_id}")

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

    def run(self, text: str) -> SkillResult:
        query = self._strip_command(text)
        if not query:
            return SkillResult(self.name, "請在 /rag 後面加上要查詢的問題。")

        file_hits = self._file_hits(query)
        vector = self.embeddings.embed(query)
        tracker_hits = self.qdrant.search(self.settings.tracker_collection, vector)
        knowledge_hits = self.qdrant.search(self.settings.knowledge_collection, vector)
        context = self._format_context(file_hits, tracker_hits, knowledge_hits)
        if not context:
            return SkillResult(self.name, "目前兩個 Qdrant collection 都沒有找到相關記憶。")

        prompt = (
            "你正在使用 OpenClaw 本地 RAG。請只根據下方 Context 回答；"
            "如果 Context 不足，請明確說不足。\n\n"
            f"問題：{query}\n\n"
            f"Context:\n{context}"
        )
        return SkillResult(self.name, self.llm.chat(prompt, max_tokens=360))

    def _file_hits(self, query: str) -> list[dict]:
        file_names = re.findall(r"[\w.+-]+\.(?:pdf|odf|md|txt|log|json|csv|tsv)", query, flags=re.IGNORECASE)
        hits = []
        seen = set()
        for file_name in file_names:
            normalized = file_name.strip()
            for candidate in (normalized, normalized.replace(".odf", ".pdf")):
                if candidate in seen:
                    continue
                seen.add(candidate)
                hits.extend(self.qdrant.scroll_by_file_name(self.settings.knowledge_collection, candidate, limit=10))
                hits.extend(self.qdrant.scroll_by_file_name(self.settings.tracker_collection, candidate, limit=6))
        return hits

    def _format_context(self, file_hits: list[dict], tracker_hits: list[dict], knowledge_hits: list[dict]) -> str:
        parts = []
        for label, hits in (
            ("filename_match", file_hits),
            (self.settings.tracker_collection, tracker_hits),
            (self.settings.knowledge_collection, knowledge_hits),
        ):
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
