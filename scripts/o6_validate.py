#!/usr/bin/env python3
"""Check that an Orion O6 (or any CPU-only host) can run what the GB10 runs.

Runs inside one bot's Telegram container, so it sees that bot's settings and
reaches llama.cpp, Ollama and Qdrant on the host and whisper / tts by name:

    docker exec -i openclaw-telegram-<bot> python3 - < scripts/o6_validate.py

It sends no Telegram messages and writes nothing to Qdrant. Checks:
  1. the text model answers, and its context window (llama.cpp /props) is
     at least OPENCLAW_MODEL_CONTEXT_TOKENS;
  2. speed (tokens per second) on a ~200-token answer;
  3. no reasoning leaking into answers (Thinking models);
  4. structured output: three real JSON schemas the bots use;
  5. a long prompt (~6k tokens) answered from its middle;
  6. the vision model reads a bundled probe image verbatim (English,
     Traditional and Simplified Chinese, numbers);
  7. embeddings, Qdrant, Whisper and the pronunciation service.
Prints a Markdown report; exit code 1 if anything failed. Then run the full
L3 suite: scripts/e2e_run.py.
"""

import base64
import json
import sys
import tempfile
import time
from pathlib import Path

for candidate in (Path("/app"),):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from openclaw_runtime.config import load_settings  # noqa: E402
from openclaw_runtime.embedding_client import EmbeddingClient  # noqa: E402
from openclaw_runtime.http_client import is_reachable, request_json  # noqa: E402
from openclaw_runtime.model_catalog import load_model_registry  # noqa: E402
from openclaw_runtime.model_client_factory import ModelClientFactory  # noqa: E402
from openclaw_runtime.night_ritual import REPORT_SCHEMA  # noqa: E402
from openclaw_runtime.skills.english_bot import GIST_OPTIONS_SCHEMA, MONDAY_LISTENING_SCHEMA  # noqa: E402
from openclaw_runtime.tts_client import TtsClient  # noqa: E402
from openclaw_runtime.vision_client import VisionClient  # noqa: E402

PROBE_PNG = (
    "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"
)
PROBE_EXPECT = ["OpenClaw O6 probe 7429", "測試收據", "NT$ 305", "电子客票", "¥553.0"]


class Failed(AssertionError):
    pass


def expect(ok: bool, message: str) -> None:
    if not ok:
        raise Failed(message)


class Validate:
    def __init__(self) -> None:
        self.settings = load_settings()
        factory = ModelClientFactory(self.settings, load_model_registry(self.settings))
        self.llm = factory.get("local_default")
        self.vision = VisionClient(factory.get_or_default("vision"))
        self.results: list[tuple[str, bool, float, str]] = []

    def check(self, name: str, func) -> None:
        started = time.time()
        try:
            detail, ok = func() or "", True
        except Failed as exc:
            detail, ok = str(exc), False
        except Exception as exc:  # noqa: BLE001
            detail, ok = f"{type(exc).__name__}: {exc}", False
        self.results.append((name, ok, time.time() - started, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", file=sys.stderr, flush=True)

    def _raw(self, prompt: str, max_tokens: int) -> dict:
        return request_json("POST", f"{self.llm.base_url}/chat/completions", {
            "model": self.llm.model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }, timeout=self.llm.timeout)

    def text_model(self) -> str:
        reply = self.llm.chat("Reply with exactly the word OK.", max_tokens=20)
        expect("ok" in reply.lower(), f"unexpected reply {reply!r}")
        root = self.llm.base_url.rsplit("/v1", 1)[0]
        try:
            props = request_json("GET", f"{root}/props", None, timeout=10)
            n_ctx = int((props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx") or 0)
        except Exception:  # noqa: BLE001 - not llama.cpp, or an older one
            n_ctx = 0
        want = self.llm.context_tokens
        if n_ctx:
            expect(not want or n_ctx >= want, f"server context {n_ctx} < OPENCLAW_MODEL_CONTEXT_TOKENS {want}")
            expect(n_ctx >= 8192, f"server context {n_ctx} is too small (start llama-server with -c 16384)")
        return f"{self.llm.model}; server context {n_ctx or 'unknown'}; configured {want or 'not set'}"

    def speed(self) -> str:
        started = time.time()
        data = self._raw("Write about 150 words on why people keep a journal.", 220)
        tokens = int((data.get("usage") or {}).get("completion_tokens") or 0)
        seconds = time.time() - started
        expect(tokens > 50, f"only {tokens} tokens came back")
        rate = tokens / seconds
        note = "" if rate >= 4 else " -- slow: English-bot feedback will take minutes"
        return f"{rate:.1f} tokens/s ({tokens} tokens in {seconds:.0f}s){note}"

    def no_reasoning_leak(self) -> str:
        message = self._raw("What is 17 + 25? Answer with the number only.", 60)["choices"][0]["message"]
        content = message.get("content") or ""
        expect("42" in content, f"answer {content!r} (reasoning field: {bool(message.get('reasoning_content') or message.get('reasoning'))})")
        expect("<think>" not in content, "the answer contains <think> -- use a non-Thinking model or a template without it")
        return "clean answer"

    def structured(self) -> str:
        cases = [
            ("gist_options", GIST_OPTIONS_SCHEMA, "Write a multiple-choice gist question about this: "
             "'I moved to London at nineteen with my brother; the city changed everything.'"),
            ("night_report", REPORT_SCHEMA, "Summarise this journal week: wins=fixed a bug; better=asked "
             "earlier; adjust=stop work earlier."),
            ("monday_listening", MONDAY_LISTENING_SCHEMA, "A learner heard 'I moved to London with my "
             "brother'. Dictation sentence: 'I moved to ____ with my ____.' Reply: '1. moving 2. London "
             "brother 3. I had to move on.' Chunk: move on."),
        ]
        for name, schema, prompt in cases:
            data = json.loads(self.llm.chat_json(prompt, schema, schema_name=name, max_tokens=700))
            missing = [key for key in schema.get("required", []) if key not in data]
            expect(not missing, f"{name}: missing {missing}")
        return f"{len(cases)} schemas parsed with all required fields"

    def long_context(self) -> str:
        filler = " ".join(f"Note {n}: the weather was unremarkable and nothing happened." for n in range(900))
        middle = len(filler) // 2
        prompt = ("Answer from the notes below. What is the secret code?\n\n" + filler[:middle]
                  + " IMPORTANT: the secret code is MAPLE-3141. " + filler[middle:] + "\n\nWhat is the secret code?")
        answer = self.llm.chat(prompt, max_tokens=40)
        if self.llm.context_tokens and "MAPLE-3141" not in answer:
            return f"answer {answer[:60]!r} -- the prompt was trimmed to fit the configured context (expected)"
        expect("MAPLE-3141" in answer, f"answer {answer[:80]!r}")
        return "found the code in the middle of ~6k tokens"

    def vision_probe(self) -> str:
        if not self.settings.vision_enabled:
            raise Failed("OPENCLAW_VISION_ENABLED is false -- image text and photos need a vision model")
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "probe.png"
            image.write_bytes(base64.b64decode("".join(PROBE_PNG)))
            text = self.vision.transcribe_image(image, max_tokens=300)
        missing = [want for want in PROBE_EXPECT if want not in text.replace("：", ":").replace(":", "：") and want not in text]
        expect(not missing, f"missing {missing} in {text!r}")
        return "English, Traditional and Simplified text read exactly"

    def services(self) -> str:
        vector = EmbeddingClient(self.settings).embed("O6 check")
        expect(len(vector) == self.settings.embedding_vector_size, f"embedding size {len(vector)}")
        expect(is_reachable(self.settings.qdrant_base_url.rstrip("/") + "/collections", timeout=5), "Qdrant unreachable")
        notes = [f"embeddings {len(vector)}d", "Qdrant ok"]
        if self.settings.whisper_enabled:
            expect(is_reachable(self.settings.whisper_base_url.rstrip("/") + "/health", timeout=5), "Whisper unreachable")
            notes.append("Whisper ok")
        if self.settings.tts_enabled:
            started = time.time()
            audio = TtsClient(self.settings).speak("resilient", "uk")
            expect(audio[:4] == b"OggS", "TTS did not return Ogg audio")
            notes.append(f"TTS {time.time() - started:.1f}s")
        return ", ".join(notes)

    def run(self) -> int:
        for name, func in [
            ("Text model + context", self.text_model),
            ("Speed", self.speed),
            ("No reasoning in answers", self.no_reasoning_leak),
            ("Structured output (JSON schema)", self.structured),
            ("Long prompt", self.long_context),
            ("Vision: verbatim text", self.vision_probe),
            ("Embeddings / Qdrant / Whisper / TTS", self.services),
        ]:
            self.check(name, func)
        passed = sum(1 for _, ok, _, _ in self.results if ok)
        print(f"# O6 validation -- {self.settings.runtime_label}\n\n{passed}/{len(self.results)} passed\n")
        print("| Check | Result | Time | Detail |\n| --- | --- | --- | --- |")
        for name, ok, seconds, detail in self.results:
            print(f"| {name} | {'PASS' if ok else '**FAIL**'} | {seconds:.0f}s | {detail.replace('|', '/')} |")
        return 0 if passed == len(self.results) else 1


if __name__ == "__main__":
    sys.exit(Validate().run())
