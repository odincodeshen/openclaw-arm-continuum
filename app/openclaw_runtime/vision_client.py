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


NO_TEXT = "NO_TEXT"
READER_SYSTEM = "You read images precisely. You never translate, summarise or change the script of what you read."
TRANSCRIBE_INSTRUCTION = (
    "Transcribe every piece of text visible in this image, exactly as written. Keep the original "
    "language and script: do not translate, and do not convert between Traditional and Simplified "
    "Chinese. Keep the reading order and line breaks; write each table row on one line with the cells "
    "separated by ' | '. Include small print, numbers, codes, dates, prices and names exactly, character "
    "by character. Do not describe the image and do not add any commentary. If the image has no text at "
    f"all, reply with exactly: {NO_TEXT}"
)
CONTINUE_INSTRUCTION = (
    "\n\nYou already transcribed the text up to the end of this excerpt -- do not repeat it, continue "
    "from exactly where it stops:\n"
)


def describe_instruction(fallback_language: str, note: str = "") -> str:
    text = (
        "Describe this image for a search index in at most 80 words: what it is (screenshot, ticket, "
        "receipt, document page, slide, whiteboard, handwritten note, photo...), its main subject, and "
        "what can be seen beyond its text. Do not copy out its text, and do not mention anything that "
        "is missing or not shown. Write the description in "
        "the same language and script as the main text in the image (English, Traditional Chinese or "
        "Simplified Chinese); if it has no text, write it in "
        f"{fallback_language or 'English'}."
    )
    if note:
        text += f"\n\nThe uploader added this note, use it as context: {note}"
    return text


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

    def transcribe_image(self, image_path: Path, *, max_tokens: int = 4096) -> str:
        """All visible text, verbatim, in its own language and script ("" if
        there is none). A transcription cut off by the token limit gets one
        continuation call."""
        try:
            text, finish = self.llm.read_image(
                Path(image_path), TRANSCRIBE_INSTRUCTION, system=READER_SYSTEM, max_tokens=max_tokens
            )
            if finish == "length" and text:
                more, finish = self.llm.read_image(
                    Path(image_path),
                    TRANSCRIBE_INSTRUCTION + CONTINUE_INSTRUCTION + text[-400:],
                    system=READER_SYSTEM,
                    max_tokens=max_tokens,
                )
                text = text + "\n" + more
            if finish == "length":
                text += "\n[transcription cut off at the length limit]"
        except Exception as exc:  # noqa: BLE001
            raise VisionError(str(exc)) from exc
        text = (text or "").strip()
        return "" if text == NO_TEXT or text.endswith(NO_TEXT) and len(text) < 20 else text

    def describe_for_index(self, image_path: Path, *, fallback_language: str = "", note: str = "",
                           max_tokens: int = 400) -> str:
        try:
            text, _ = self.llm.read_image(
                Path(image_path), describe_instruction(fallback_language, note), system=READER_SYSTEM,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            raise VisionError(str(exc)) from exc
        return (text or "").strip()
