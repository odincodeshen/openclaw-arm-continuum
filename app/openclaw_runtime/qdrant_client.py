import time
import uuid

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import get_json, request_json


class QdrantClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def ensure_collections(self) -> None:
        for collection in (self.settings.tracker_collection, self.settings.knowledge_collection):
            self.ensure_collection(collection)

    def ensure_collection(self, collection: str) -> None:
        collections = get_json(f"{self.settings.qdrant_base_url}/collections", timeout=self.settings.web_timeout)
        names = {
            item.get("name")
            for item in collections.get("result", {}).get("collections", [])
        }
        if collection in names:
            return
        request_json(
            "PUT",
            f"{self.settings.qdrant_base_url}/collections/{collection}",
            {"vectors": {"size": self.settings.embedding_vector_size, "distance": "Cosine"}},
            timeout=self.settings.request_timeout,
        )

    def upsert_text(self, collection: str, text: str, vector: list[float], metadata: dict) -> str:
        point_id = str(uuid.uuid4())
        payload_data = {
            "text": text,
            "source": metadata.get("source", "telegram"),
            "kind": metadata.get("kind", "memory"),
            "created_at": metadata.get("created_at", int(time.time())),
        }
        for key, value in metadata.items():
            if key not in payload_data:
                payload_data[key] = value
        payload = {
            "points": [
                {
                    "id": point_id,
                    "vector": vector,
                    "payload": payload_data,
                }
            ]
        }
        request_json(
            "PUT",
            f"{self.settings.qdrant_base_url}/collections/{collection}/points?wait=true",
            payload,
            timeout=self.settings.request_timeout,
        )
        return point_id

    def search(self, collection: str, vector: list[float], limit: int | None = None) -> list[dict]:
        payload = {"vector": vector, "limit": limit or self.settings.retrieval_limit, "with_payload": True}
        response = request_json(
            "POST",
            f"{self.settings.qdrant_base_url}/collections/{collection}/points/search",
            payload,
            timeout=self.settings.request_timeout,
        )
        return list(response.get("result") or [])

    def scroll_by_file_name(self, collection: str, file_name: str, limit: int = 12) -> list[dict]:
        points: list[dict] = []
        offset = None
        while len(points) < 512:
            payload = {
                "filter": {"must": [{"key": "file_name", "match": {"value": file_name}}]},
                "limit": 128,
                "with_payload": True,
                "with_vector": False,
            }
            if offset is not None:
                payload["offset"] = offset
            response = request_json(
                "POST",
                f"{self.settings.qdrant_base_url}/collections/{collection}/points/scroll",
                payload,
                timeout=self.settings.request_timeout,
            )
            result = response.get("result", {})
            batch = list(result.get("points") or [])
            points.extend(batch)
            offset = result.get("next_page_offset")
            if not batch or offset is None:
                break
        ordered = sorted(points, key=lambda point: (point.get("payload") or {}).get("chunk_index", 0))
        return ordered[:limit]
