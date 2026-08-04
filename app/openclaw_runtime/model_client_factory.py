from openclaw_runtime.config import Settings
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.model_catalog import ModelRegistry


class ModelClientFactory:
    def __init__(self, settings: Settings, registry: ModelRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self._clients: dict[str, LlmClient] = {}

    def get(self, model_policy: str) -> LlmClient:
        spec = self.registry.resolve(model_policy)
        client = self._clients.get(spec.model_id)
        if client is None:
            client = LlmClient(self.settings, model_spec=spec)
            self._clients[spec.model_id] = client
        return client

    def fallback_for(self, model_policy: str) -> LlmClient | None:
        spec = self.registry.fallback_for(model_policy)
        return self.get(spec.model_id) if spec else None
