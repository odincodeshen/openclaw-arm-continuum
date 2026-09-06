"""Optional LLM intent classifier for the natural-language residual.

Deterministic keyword / slash-command routing always runs first and always
wins. Only when nothing matched -- a plain-language message that would
otherwise fall straight through to the chat model -- does this classifier get
a turn, and only when a `local_router` model is configured in the catalog.
It never overrides an explicit command.
"""

import json
import re

from openclaw_runtime.model_client_factory import ModelClientFactory


ROUTER_POLICY = "local_router"

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["knowledge_base", "web_search", "chat"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["intent", "confidence"],
    "additionalProperties": False,
}

_PROMPT = (
    "You route a personal assistant message to one handler. Reply with one JSON "
    "object only: {\"intent\":\"knowledge_base|web_search|chat\",\"confidence\":0.0-1.0,"
    "\"reason\":\"short\"}.\n"
    "- knowledge_base: the user is asking about their own saved notes, uploaded "
    "documents, or previously stored personal information.\n"
    "- web_search: the user wants current or external information that needs a "
    "web lookup (news, prices, releases, 'latest', 'today').\n"
    "- chat: general conversation, reasoning, writing help, or anything a plain "
    "language model answers without retrieval.\n\n"
    "Message:\n"
)


def _json_object(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip(), flags=re.IGNORECASE)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("router output is not an object")
    return value


class IntentRouter:
    def __init__(self, clients: ModelClientFactory, *, min_confidence: float = 0.6) -> None:
        self.clients = clients
        self.min_confidence = min_confidence

    @classmethod
    def build(cls, clients, *, enabled: bool, min_confidence: float):
        """Return an IntentRouter only if enabled and a local_router model exists."""
        if not enabled or clients is None:
            return None
        try:
            clients.get(ROUTER_POLICY)
        except LookupError:
            return None
        return cls(clients, min_confidence=min_confidence)

    def classify(self, text: str) -> str | None:
        """Return 'knowledge_base' | 'web_search' | 'chat', or None to defer."""
        try:
            client = self.clients.get(ROUTER_POLICY)
            raw = client.chat_json(
                _PROMPT + text[:4000], INTENT_SCHEMA, schema_name="assistant_intent", max_tokens=160
            )
            value = _json_object(raw)
        except Exception:
            return None
        intent = value.get("intent")
        confidence = value.get("confidence")
        if intent not in {"knowledge_base", "web_search", "chat"}:
            return None
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            return None
        if float(confidence) < self.min_confidence:
            return None
        return intent
