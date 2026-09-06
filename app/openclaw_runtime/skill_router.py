import json

from openclaw_runtime.config import Settings
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.intent_router import IntentRouter
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.skills.base import SkillResult, has_explicit_command_prefix
from openclaw_runtime.skills.memory import MemoryWriteSkill, RagRetrieveSkill
from openclaw_runtime.skills.weather import WeatherSkill
from openclaw_runtime.skills.web_search import WebSearchSkill


class SkillRouter:
    def __init__(self, settings: Settings, llm: LlmClient, model_clients=None) -> None:
        self.settings = settings
        self.llm = llm
        self.model_clients = model_clients
        self.config = self._load_config()
        self.skills = self._load_skills()
        self.intent_router = IntentRouter.build(
            model_clients,
            enabled=getattr(settings, "intent_router_enabled", False),
            min_confidence=getattr(settings, "intent_router_min_confidence", 0.6),
        )

    def _client_for(self, skill_config: dict) -> LlmClient:
        """Pick the expert model for a skill: its configured model_policy from
        skills.json (resolved through the catalog), else the default client."""
        policy = str(skill_config.get("model_policy") or "").strip()
        if policy and self.model_clients is not None:
            return self.model_clients.get_or_default(policy)
        return self.llm

    def route(self, text: str) -> SkillResult:
        for skill in self.skills:
            if has_explicit_command_prefix(getattr(skill, "keywords", ()), text):
                return skill.run(text)
        for skill in self.skills:
            if skill.can_handle(text):
                return skill.run(text)
        routed = self._route_by_intent(text)
        if routed is not None:
            return routed
        return SkillResult("llm", self.llm.chat(text))

    def _route_by_intent(self, text: str) -> SkillResult | None:
        if self.intent_router is None:
            return None
        intent = self.intent_router.classify(text)
        target = {"knowledge_base": "rag_retrieve", "web_search": "web_search"}.get(intent)
        if not target:
            return None
        for skill in self.skills:
            if getattr(skill, "name", None) == target:
                return skill.run(text)
        return None

    def _load_config(self) -> dict:
        if not self.settings.skills_config_path.exists():
            return {"skills": {}}
        with self.settings.skills_config_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _load_skills(self) -> list:
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
                    skills.append(
                        RagRetrieveSkill(
                            self.settings, rag_config, embeddings, qdrant, self._client_for(rag_config)
                        )
                    )
            except Exception as exc:
                print(f"[skills] memory disabled: {exc}", flush=True)

        if self.settings.web_enabled:
            weather_config = skills_config.get("weather", {})
            if weather_config.get("enabled", True):
                skills.append(WeatherSkill(self.settings, weather_config))

            web_search_config = skills_config.get("web_search", {})
            if web_search_config.get("enabled", True):
                skills.append(
                    WebSearchSkill(self.settings, web_search_config, self._client_for(web_search_config))
                )

        return skills
