"""The single point where OpenClaw talks to a vision model.

Everything else (Telegram handlers, category ingest) depends only on
``VisionClient.describe_image`` -- never on which model, endpoint, or provider
is behind it. Point ``OPENCLAW_VLM_BASE_URL`` / ``OPENCLAW_VLM_MODEL`` at any
OpenAI-compatible ``/chat/completions`` server (vLLM, Ollama, llama.cpp,
LM Studio, a hosted API) to switch models without touching code.
"""

import base64
import mimetypes
import socket
import urllib.error
from pathlib import Path

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import is_reachable, request_json
from openclaw_runtime.llm_client import VLLM_NOT_READY_MESSAGE, clean_model_content


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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_reachable(self) -> bool:
        return is_reachable(f"{self.settings.vlm_base_url}/models", timeout=3)

    def describe_image(
        self,
        image_path: Path,
        instruction: str | None = None,
        *,
        max_tokens: int | None = None,
    ) -> str:
        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        image_b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        text = (instruction or DEFAULT_DESCRIBE_INSTRUCTION).strip()
        payload = {
            "model": self.settings.vlm_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
                        },
                    ],
                }
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens or self.settings.vlm_max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response = self._completion(payload)
        message = response["choices"][0]["message"]
        content = message.get("content") or ""
        if not content and message.get("reasoning"):
            raise VisionError("The vision model returned only its reasoning, not a description.")
        cleaned = clean_model_content(content)
        if not cleaned:
            raise VisionError("The vision model returned an empty description.")
        return cleaned

    def _completion(self, payload: dict) -> dict:
        try:
            return request_json(
                "POST",
                f"{self.settings.vlm_base_url}/chat/completions",
                payload,
                timeout=self.settings.request_timeout,
            )
        except (ConnectionResetError, ConnectionRefusedError, TimeoutError, socket.timeout) as exc:
            raise VisionError(VLLM_NOT_READY_MESSAGE) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (ConnectionResetError, ConnectionRefusedError, TimeoutError, socket.timeout)):
                raise VisionError(VLLM_NOT_READY_MESSAGE) from exc
            raise VisionError(str(exc)) from exc
