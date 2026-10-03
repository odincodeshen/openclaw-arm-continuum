import base64
import mimetypes
from datetime import datetime, timezone
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


CONTEXT_MARGIN_TOKENS = 256
IMAGE_TOKENS_ESTIMATE = 1200
MIN_ANSWER_TOKENS = 128
TRIM_MARKER = "\n\n[... trimmed to fit the model's context window ...]\n\n"


def estimate_tokens(text: str) -> int:
    """A deliberately high estimate: CJK characters count one token each,
    everything else one per three characters."""
    cjk = sum(1 for ch in text if "\u2e80" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff" or "\uac00" <= ch <= "\ud7af")
    return cjk + (len(text) - cjk) // 3 + 1


def _message_tokens(message: dict) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return estimate_tokens(content) + 4
    total = 4
    for part in content or []:
        if part.get("type") == "text":
            total += estimate_tokens(part.get("text", ""))
        elif part.get("type") == "image_url":
            total += IMAGE_TOKENS_ESTIMATE
    return total


def _trim_middle(text: str, drop_chars: int) -> str:
    if drop_chars <= 0 or drop_chars >= len(text) - 200:
        drop_chars = max(0, len(text) - 200) if drop_chars > 0 else 0
    if not drop_chars:
        return text
    keep = len(text) - drop_chars
    head = keep * 3 // 5
    return text[:head] + TRIM_MARKER + text[len(text) - (keep - head):]


def fit_to_context(payload: dict, context_tokens: int) -> dict:
    """Make a chat payload fit a small context window (llama.cpp on a
    CPU-only host): cut the middle out of the longest text part -- the
    retrieved context or transcript, usually; instructions sit at the start
    and the question at the end -- and, if the prompt alone is still too
    big, lower max_tokens. Unchanged when context_tokens is 0."""
    if context_tokens <= 0:
        return payload
    messages = payload["messages"]
    answer = int(payload.get("max_tokens") or MIN_ANSWER_TOKENS)
    budget = context_tokens - CONTEXT_MARGIN_TOKENS
    for _ in range(4):
        prompt = sum(_message_tokens(m) for m in messages)
        overflow = prompt + min(answer, max(MIN_ANSWER_TOKENS, budget // 4)) - budget
        if overflow <= 0:
            break
        # the longest text piece is the one to shorten
        best = None
        for m_index, message in enumerate(messages):
            content = message.get("content")
            pieces = [(None, content)] if isinstance(content, str) else [
                (p_index, part.get("text", "")) for p_index, part in enumerate(content or []) if part.get("type") == "text"
            ]
            for p_index, text in pieces:
                if best is None or len(text) > len(best[2]):
                    best = (m_index, p_index, text)
        if best is None or len(best[2]) < 400:
            break
        m_index, p_index, text = best
        ratio = estimate_tokens(text) / max(1, len(text))
        shorter = _trim_middle(text, int(overflow / ratio) + len(TRIM_MARKER) + 16)
        if p_index is None:
            messages[m_index] = {**messages[m_index], "content": shorter}
        else:
            parts = list(messages[m_index]["content"])
            parts[p_index] = {**parts[p_index], "text": shorter}
            messages[m_index] = {**messages[m_index], "content": parts}
    prompt = sum(_message_tokens(m) for m in messages)
    payload["max_tokens"] = max(MIN_ANSWER_TOKENS, min(answer, budget - prompt))
    return payload


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

    @property
    def context_tokens(self) -> int:
        if self.model_spec is not None and getattr(self.model_spec, "context_tokens", 0):
            return self.model_spec.context_tokens
        return getattr(self.settings, "model_context_tokens", 0)

    def _chat_completion(self, payload: dict) -> dict:
        payload = fit_to_context(payload, self.context_tokens)
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

    def _system_message(self) -> dict:
        """The system prompt plus the real current date, computed fresh on
        every call. Without this, the model has no way to know "now" --
        it falls back to whatever date its training data implies, so real
        current-dated info (a 2026 booking, today's news) reads as if it
        were years in the future. UTC, not the user's local time zone --
        good enough to fix the multi-year mismatch; exact-day precision
        near midnight isn't worth the complexity of per-user time zones."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d (%A), UTC")
        grounding = (
            f"Today's date is {today}. Treat this as the actual current date for "
            "anything time-relative -- do not assume your training cutoff is 'now'."
        )
        return {"role": "system", "content": f"{self.settings.system_prompt}\n\n{grounding}"}

    # For text whose language and shape the code decides (an English closing
    # line, report sections): the bot's persona prompt -- often "always reply
    # in Traditional Chinese" -- would fight the instruction, and some models
    # (ERNIE) side with the persona.
    NEUTRAL_SYSTEM = "You follow the user's formatting and language instructions exactly."

    def _neutral_system_message(self) -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d (%A), UTC")
        return {"role": "system", "content": f"{self.NEUTRAL_SYSTEM}\n\nToday's date is {today}."}

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
        persona: bool = True,
    ) -> str:
        """persona=False: no persona prompt and no reply-language instruction --
        the prompt alone says what language and shape to answer in."""
        if persona:
            final_answer_prompt = f"{user_text}\n\n{self._answer_instruction()}"
            messages = [self._system_message()]
        else:
            final_answer_prompt = f"{user_text}\n\nAnswer directly and do not output your reasoning process."
            messages = [self._neutral_system_message()]
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

    def chat_json(self, user_text: str, schema: dict, *, schema_name: str, max_tokens: int | None = None,
                  persona: bool = True) -> str:
        payload = {
            "model": self.model,
            "messages": [
                self._system_message() if persona else self._neutral_system_message(),
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

    def read_image(self, image_path: Path, prompt: str, *, system: str, max_tokens: int) -> tuple[str, str]:
        """An image call without the persona's system prompt or reply-language
        instruction -- for work where the model must keep the image's own
        language, like transcribing its text. Returns (text, finish_reason)."""
        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
                    ],
                },
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        choice = self._chat_completion(payload)["choices"][0]
        content = clean_model_content((choice.get("message") or {}).get("content") or "")
        return content, str(choice.get("finish_reason") or "")

    def chat_with_image(self, image_path: Path, prompt: str, *, max_tokens: int | None = None) -> str:
        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                self._system_message(),
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
