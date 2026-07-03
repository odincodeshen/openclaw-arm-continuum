import base64
import mimetypes
from pathlib import Path
import re
import socket
import urllib.error

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import request_json


VLLM_NOT_READY_MESSAGE = (
    "OpenClaw 本地推理端點還在啟動或重新載入模型，請稍等 1-3 分鐘後再試。"
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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _chat_completion(self, payload: dict) -> dict:
        try:
            return request_json(
                "POST",
                f"{self.settings.vllm_base_url}/chat/completions",
                payload,
                timeout=self.settings.request_timeout,
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

    def chat(self, user_text: str, *, max_tokens: int | None = None) -> str:
        final_answer_prompt = f"{user_text}\n\n請直接回答，不要輸出推理過程。"
        payload = {
            "model": self.settings.vllm_model,
            "messages": [
                {"role": "system", "content": self.settings.system_prompt},
                {"role": "user", "content": final_answer_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens or self.settings.max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response = self._chat_completion(payload)
        message = response["choices"][0]["message"]
        content = message.get("content") or ""
        if not content and message.get("reasoning"):
            return "模型只回傳了 reasoning，沒有給最終答案。請再傳一次，我會要求它更短、更直接。"
        return clean_model_content(content)

    def chat_with_image(self, image_path: Path, prompt: str, *, max_tokens: int | None = None) -> str:
        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": self.settings.vllm_model,
            "messages": [
                {"role": "system", "content": self.settings.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{prompt}\n\n請直接回答，不要輸出推理過程。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
                        },
                    ],
                },
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens or self.settings.vision_max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response = self._chat_completion(payload)
        message = response["choices"][0]["message"]
        content = message.get("content") or ""
        if not content and message.get("reasoning"):
            return "模型只回傳了 reasoning，沒有給圖片分析的最終答案。"
        return clean_model_content(content)
