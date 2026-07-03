import json

from openclaw_runtime.config import Settings
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.skills.base import SkillResult
from openclaw_runtime.skills.memory import MemoryWriteSkill, RagRetrieveSkill
from openclaw_runtime.skills.weather import WeatherSkill
from openclaw_runtime.skills.web_search import WebSearchSkill


class SkillRouter:
    def __init__(self, settings: Settings, llm: LlmClient) -> None:
        self.settings = settings
        self.llm = llm
        self.config = self._load_config()
        self.skills = self._load_skills()

    def route(self, text: str) -> SkillResult:
        for skill in self.skills:
            if skill.can_handle(text):
                return skill.run(text)
        return SkillResult("llm", self.llm.chat(text))

    def _load_config(self) -> dict:
        if not self.settings.skills_config_path.exists():
            return {"skills": {}}
        with self.settings.skills_config_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _load_skills(self) -> list:
        if not self.settings.web_enabled:
            return []
        skills_config = self.config.get("skills", {})
        skills = []

        if self.settings.memory_enabled:
            memory_config = skills_config.get("memory_write", {})
            rag_config = skills_config.get("rag_retrieve", {})
            try:
                embeddings = EmbeddingClient(self.settings)
                qdrant = QdrantClient(self.settings)
                qdrant.ensure_collections()
                if memory_config.get("enabled", True):
                    skills.append(MemoryWriteSkill(self.settings, memory_config, embeddings, qdrant))
                if rag_config.get("enabled", True):
                    skills.append(RagRetrieveSkill(self.settings, rag_config, embeddings, qdrant, self.llm))
            except Exception as exc:
                print(f"[skills] memory disabled: {exc}", flush=True)

        weather_config = skills_config.get("weather", {})
        if weather_config.get("enabled", True):
            skills.append(WeatherSkill(self.settings, weather_config))

        web_search_config = skills_config.get("web_search", {})
        if web_search_config.get("enabled", True):
            skills.append(WebSearchSkill(self.settings, web_search_config, self.llm))

        return skills
