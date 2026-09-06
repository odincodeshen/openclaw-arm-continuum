"""The single point where OpenClaw talks to a vision model.

Everything else (Telegram handlers, category ingest) depends only on
``VisionClient.describe_image`` -- never on which model or endpoint is behind
it. The backing ``LlmClient`` comes from the model catalog: define a model
with the ``vision`` role in ``models.json`` (or leave it out and the client
falls back to ``local_default``). Point that model's ``base_url`` at any
OpenAI-compatible ``/chat/completions`` server to switch models with no code
change.
"""

from pathlib import Path

from openclaw_runtime.llm_client import LlmClient


DEFAULT_DESCRIBE_INSTRUCTION = (
    "You are indexing this image so it can be found later by a text search. "
    "Return, in plain prose: (1) a thorough, factual description of what the "
    "image shows; (2) every piece of visible text transcribed verbatim; "
    "(3) notable objects, labels, names, numbers, and diagram structure. "
    "Do not speculate about anything not visible."
)


class VisionError(RuntimeError):
    pass


class VisionClient:
    def __init__(self, llm: LlmClient) -> None:
        self.llm = llm

    @property
    def endpoint_id(self) -> str:
        return self.llm.endpoint_id

    def is_reachable(self) -> bool:
        return self.llm.is_reachable()

    def describe_image(
        self,
        image_path: Path,
        instruction: str | None = None,
        *,
        max_tokens: int | None = None,
    ) -> str:
        prompt = (instruction or DEFAULT_DESCRIBE_INSTRUCTION).strip()
        try:
            text = self.llm.chat_with_image(Path(image_path), prompt, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001 - surface as a single vision error type
            raise VisionError(str(exc)) from exc
        cleaned = (text or "").strip()
        if not cleaned or cleaned.startswith("The model only returned its reasoning"):
            raise VisionError("The vision model returned an empty description.")
        return cleaned
