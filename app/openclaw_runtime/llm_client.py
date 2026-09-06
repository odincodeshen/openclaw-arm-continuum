import base64
import mimetypes
from pathlib import Path
import re
import socket
import urllib.error

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import is_reachable, request_json


VLLM_NOT_READY_MESSAGE = (
    "The OpenClaw local inference endpoint is still starting up or reloading the model. Please wait 1-3 minutes and try again."
)


def clean_model_content(content: str) -> str:
    text = str(content or "").strip()
    response_match = re.search(r"<response>\s*(.*?)\s*</response>", text, flags=re.DOTALL | re.IGNORECASE)
    if response_match:
        text = response_match.group(1).strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    text = re.sub(r"^\s*</?response>\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*</response>\s*$", "", text, flags=re.IGNORECASE)
    return text.strip()


class LlmClient:
    def __init__(self, settings: Settings, model_spec=None) -> None:
        self.settings = settings
        self.model_spec = model_spec

    @property
    def base_url(self) -> str:
        return self.model_spec.base_url if self.model_spec else self.settings.vllm_base_url

    @property
    def model(self) -> str:
        return self.model_spec.model if self.model_spec else self.settings.vllm_model

    @property
    def timeout(self) -> int:
        return self.model_spec.timeout if self.model_spec else self.settings.request_timeout

    @property
    def endpoint_id(self) -> str:
        return self.model_spec.model_id if self.model_spec else "local_default"

    def is_reachable(self) -> bool:
        return is_reachable(f"{self.base_url}/models", timeout=3)

    def _chat_completion(self, payload: dict) -> dict:
        try:
            return request_json(
                "POST",
                f"{self.base_url}/chat/completions",
                payload,
                timeout=self.timeout,
            )
        except (ConnectionResetError, ConnectionRefusedError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError(VLLM_NOT_READY_MESSAGE) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (ConnectionResetError, ConnectionRefusedError, TimeoutError, socket.timeout)):
                raise RuntimeError(VLLM_NOT_READY_MESSAGE) from exc
            message = str(exc)
            if any(fragment in message for fragment in ("Connection reset", "Connection refused", "timed out")):
                raise RuntimeError(VLLM_NOT_READY_MESSAGE) from exc
            raise

    def _answer_instruction(self) -> str:
        directive = "Answer directly and do not output your reasoning process."
        language = getattr(self.settings, "reply_language", "") or ""
        if language:
            return (
                f"Reply in {language} by default. Use another language only if the user "
                f"explicitly asks for it or writes their message in that language. {directive}"
            )
        return directive

    def chat(
        self,
        user_text: str,
        *,
        max_tokens: int | None = None,
        history: list[dict] | None = None,
    ) -> str:
        final_answer_prompt = f"{user_text}\n\n{self._answer_instruction()}"
        messages = [{"role": "system", "content": self.settings.system_prompt}]
        for turn in history or []:
            role = turn.get("role")
            content = turn.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": final_answer_prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens or self.settings.max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response = self._chat_completion(payload)
        message = response["choices"][0]["message"]
        content = message.get("content") or ""
        if not content and message.get("reasoning"):
            return "The model only returned its reasoning, not a final answer. Send it again and I'll ask for something shorter and more direct."
        return clean_model_content(content)

    def chat_json(self, user_text: str, schema: dict, *, schema_name: str, max_tokens: int | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.settings.system_prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0,
            "max_tokens": max_tokens or self.settings.max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema},
            },
        }
        response = self._chat_completion(payload)
        message = response["choices"][0]["message"]
        content = message.get("content") or ""
        if not content:
            raise ValueError("model returned an empty structured response")
        return clean_model_content(content)

    def chat_with_image(self, image_path: Path, prompt: str, *, max_tokens: int | None = None) -> str:
        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.settings.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{prompt}\n\n{self._answer_instruction()}"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
                        },
                    ],
                },
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens or self.settings.vlm_max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response = self._chat_completion(payload)
        message = response["choices"][0]["message"]
        content = message.get("content") or ""
        if not content and message.get("reasoning"):
            return "The model only returned its reasoning, not a final answer for the image analysis."
        return clean_model_content(content)
