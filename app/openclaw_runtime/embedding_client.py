from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import request_json


class EmbeddingClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def embed(self, text: str) -> list[float]:
        payload = {"model": self.settings.embedding_model, "input": text}
        response = request_json(
            "POST",
            f"{self.settings.ollama_base_url}/api/embed",
            payload,
            timeout=self.settings.request_timeout,
        )
        embeddings = response.get("embeddings") or []
        if embeddings:
            return list(embeddings[0])

        legacy_response = request_json(
            "POST",
            f"{self.settings.ollama_base_url}/api/embeddings",
            {"model": self.settings.embedding_model, "prompt": text},
            timeout=self.settings.request_timeout,
        )
        embedding = legacy_response.get("embedding")
        if not embedding:
            raise RuntimeError("Ollama did not return an embedding vector")
        return list(embedding)
