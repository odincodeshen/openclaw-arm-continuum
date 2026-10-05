#!/usr/bin/env python3
"""What can this machine's services do, and how fast? (bin/verify platform)

Runs inside the verify sandbox (see verify/runner.py for the isolation) and
checks the services a bot would use, on any host. These are the checks from
the earlier scripts/o6_validate.py, for every platform:

- the text model answers; its context window is at least the configured one
  (llama.cpp /props or vLLM /v1/models);
- generation and prompt-reading speed;
- no reasoning leaks into answers;
- the JSON schemas the bots use come back complete;
- a long prompt is answered from its middle (size per platform);
- the vision model reads verify/fixtures/receipt_three_scripts.png word for
  word, in English, Traditional and Simplified Chinese;
- embeddings, Qdrant, Whisper and TTS when present.

Correctness failures fail the check; speed is reported as numbers, which
bin/verify compares with the platform's thresholds (verify/platforms/).
Prints one JSON line as the last line of output.

    python /src/verify/platform_check.py --long-prompt-tokens 6000
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))
os.environ.setdefault("OPENCLAW_MODEL_CATALOG", str(REPO / "app" / "models.json"))
os.environ["OPENCLAW_TELEGRAM_BOT_TOKEN"] = "0:verify-sandbox"

from openclaw_runtime.config import load_settings  # noqa: E402
from openclaw_runtime.embedding_client import EmbeddingClient  # noqa: E402
from openclaw_runtime.http_client import is_reachable, request_json  # noqa: E402
from openclaw_runtime.model_catalog import load_model_registry  # noqa: E402
from openclaw_runtime.model_client_factory import ModelClientFactory  # noqa: E402
from openclaw_runtime.night_ritual import REPORT_SCHEMA  # noqa: E402
from openclaw_runtime.skills.english_bot import GIST_OPTIONS_SCHEMA, MONDAY_LISTENING_SCHEMA  # noqa: E402
from openclaw_runtime.tts_client import TtsClient  # noqa: E402
from openclaw_runtime.vision_client import VisionClient  # noqa: E402

PROBE = REPO / "verify" / "fixtures" / "receipt_three_scripts.png"
PROBE_EXPECT = ["4417", "測試發票", "280", "电子发票", "612.5"]


class Failed(AssertionError):
    pass


def expect(ok: bool, message: str) -> None:
    if not ok:
        raise Failed(message)


class PlatformCheck:
    def __init__(self, long_prompt_tokens: int) -> None:
        self.settings = load_settings()
        factory = ModelClientFactory(self.settings, load_model_registry(self.settings))
        self.llm = factory.get("local_default")
        self.vision = VisionClient(factory.get_or_default("vision"))
        self.long_prompt_tokens = long_prompt_tokens
        self.facts: dict = {"model": self.llm.model}
        self.metrics: dict = {}
        self.checks: list[dict] = []

    def run_check(self, name: str, func) -> None:
        started = time.time()
        try:
            detail, status = func() or "", "pass"
        except Failed as exc:
            detail, status = str(exc), "fail"
        except Exception as exc:  # noqa: BLE001
            detail, status = f"{type(exc).__name__}: {exc}", "fail"
        if detail.startswith("SKIP "):
            detail, status = detail[5:], "skip"
        self.checks.append({"name": name, "status": status, "seconds": round(time.time() - started, 1),
                            "detail": detail})
        print(f"{status.upper():4}  {name}  {detail}", file=sys.stderr, flush=True)

    @staticmethod
    def nonce() -> str:
        """A fresh first line, so a prompt cache can't make reading look faster than it is."""
        return f"Run {secrets.token_hex(8)}.\n"

    def _raw(self, prompt: str, max_tokens: int) -> dict:
        return request_json("POST", f"{self.llm.base_url}/chat/completions", {
            "model": self.llm.model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }, timeout=self.llm.timeout)

    # -- checks -----------------------------------------------------------
    def text_model(self) -> str:
        reply = self.llm.chat("Reply with exactly the word OK.", max_tokens=20)
        expect("ok" in reply.lower(), f"unexpected reply {reply!r}")
        root = self.llm.base_url.rsplit("/v1", 1)[0]
        server, n_ctx = "openai-compatible", 0
        try:
            props = request_json("GET", f"{root}/props", None, timeout=10)
            n_ctx = int((props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx") or 0)
            server = "llama.cpp"
        except Exception:  # noqa: BLE001 - not llama.cpp
            try:
                models = request_json("GET", f"{self.llm.base_url}/models", None, timeout=10).get("data") or []
                n_ctx = int((models[0] if models else {}).get("max_model_len") or 0)
                server = "vllm" if n_ctx else server
            except Exception:  # noqa: BLE001
                pass
        want = self.llm.context_tokens
        self.facts.update(model_server=server, server_context=n_ctx, configured_context=want)
        if n_ctx:
            expect(not want or n_ctx >= want, f"server context {n_ctx} < OPENCLAW_MODEL_CONTEXT_TOKENS {want}")
            expect(n_ctx >= 8192, f"server context {n_ctx} is too small for the bots (use 16384 or more)")
        return f"{server}: {self.llm.model}; server context {n_ctx or 'unknown'}; configured {want or 'not set'}"

    def speed(self) -> str:
        started = time.time()
        data = self._raw("Write about 150 words on why people keep a journal.", 220)
        tokens = int((data.get("usage") or {}).get("completion_tokens") or 0)
        seconds = time.time() - started
        expect(tokens > 50, f"only {tokens} tokens came back")
        self.metrics["generation_tokens_per_s"] = round(tokens / seconds, 1)
        # prompt reading: ~1,500 tokens in, 5 out
        filler = " ".join(f"Line {n}: a plain sentence about nothing in particular." for n in range(130))
        started = time.time()
        data = self._raw(self.nonce() + filler + "\n\nReply with the word DONE.", 5)
        prompt_tokens = int((data.get("usage") or {}).get("prompt_tokens") or 0)
        seconds = time.time() - started
        self.metrics["prompt_tokens_per_s"] = round(prompt_tokens / seconds, 1) if seconds else 0
        return (f"writes {self.metrics['generation_tokens_per_s']} tokens/s, reads "
                f"{self.metrics['prompt_tokens_per_s']} tokens/s ({prompt_tokens}-token prompt)")

    def no_reasoning_leak(self) -> str:
        message = self._raw("What is 17 + 25? Answer with the number only.", 60)["choices"][0]["message"]
        content = message.get("content") or ""
        expect("42" in content, f"answer {content!r}")
        expect("<think>" not in content, "the answer contains <think>: use a non-Thinking model or template")
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
        return f"{len(cases)} schemas complete"

    def long_context(self) -> str:
        count = max(50, self.long_prompt_tokens // 12)  # each note is about 12 tokens
        filler = " ".join(f"Note {n}: the weather was unremarkable and nothing happened." for n in range(count))
        middle = len(filler) // 2
        prompt = (self.nonce() + "Answer from the notes below. What is the secret code?\n\n" + filler[:middle]
                  + " IMPORTANT: the secret code is MAPLE-3141. " + filler[middle:] + "\n\nWhat is the secret code?")
        started = time.time()
        answer = self.llm.chat(prompt, max_tokens=40)
        self.metrics["long_prompt_seconds"] = round(time.time() - started, 1)
        if self.llm.context_tokens and self.long_prompt_tokens > self.llm.context_tokens and "MAPLE-3141" not in answer:
            return "trimmed to the configured context, as expected"
        expect("MAPLE-3141" in answer, f"answer {answer[:80]!r}")
        return f"found the code in the middle of ~{self.long_prompt_tokens} tokens"

    @staticmethod
    def fresh_probe() -> Path:
        """The probe with one random corner pixel changed: the same text, but an
        image a prompt cache hasn't seen, so the timing is real."""
        from PIL import Image
        image = Image.open(PROBE).convert("RGB")
        image.putpixel((0, 0), tuple(secrets.randbelow(256) for _ in range(3)))
        target = Path("/tmp") / f"probe-{secrets.token_hex(4)}.png"
        image.save(target)
        return target

    def vision_probe(self) -> str:
        if not self.settings.vision_enabled:
            return "SKIP vision is off (OPENCLAW_VISION_ENABLED=false)"
        probe = self.fresh_probe()
        started = time.time()
        text = self.vision.transcribe_image(probe, max_tokens=300)
        self.metrics["vision_seconds"] = round(time.time() - started, 1)
        normal = text.replace("：", ":")
        missing = [want for want in PROBE_EXPECT if want not in text and want.replace("：", ":") not in normal]
        expect(not missing, f"missing {missing} in {text[:160]!r}")
        self.facts["vision"] = True
        return "English, Traditional and Simplified text read exactly"

    def services(self) -> str:
        client = EmbeddingClient(self.settings)
        client.embed("verify warm-up")  # the first call may load the model
        started = time.time()
        vector = client.embed("verify check")
        self.metrics["embedding_ms"] = round((time.time() - started) * 1000)
        expect(len(vector) == self.settings.embedding_vector_size, f"embedding size {len(vector)}")
        expect(is_reachable(self.settings.qdrant_base_url.rstrip("/") + "/collections", timeout=5), "Qdrant unreachable")
        notes = [f"embeddings {len(vector)}d", "Qdrant ok"]
        if is_reachable(self.settings.whisper_base_url.rstrip("/") + "/health", timeout=5):
            notes.append("Whisper ok")
            self.facts["whisper"] = True
        elif self.settings.whisper_enabled:
            notes.append("Whisper not reachable")
        if is_reachable(self.settings.tts_base_url.rstrip("/") + "/health", timeout=5):
            started = time.time()
            audio = TtsClient(self.settings).speak("resilient", "uk")
            expect(audio[:4] == b"OggS", "TTS did not return Ogg audio")
            # a fixed word, usually cached by the TTS service: this shows it works, not its speed
            self.facts["tts"] = True
            notes.append(f"TTS ok ({time.time() - started:.1f}s, likely cached)")
        self.facts["embedding_dims"] = len(vector)
        return ", ".join(notes)

    def run(self) -> dict:
        for name, func in [
            ("Text model and context", self.text_model),
            ("Speed", self.speed),
            ("No reasoning in answers", self.no_reasoning_leak),
            ("Structured output (JSON schema)", self.structured),
            ("Long prompt", self.long_context),
            ("Vision: verbatim text", self.vision_probe),
            ("Embeddings, Qdrant, Whisper, TTS", self.services),
        ]:
            self.run_check(name, func)
        return {"facts": self.facts, "metrics": self.metrics, "checks": self.checks}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--long-prompt-tokens", type=int, default=6000)
    args = parser.parse_args(argv)
    result = PlatformCheck(args.long_prompt_tokens).run()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if all(c["status"] != "fail" for c in result["checks"]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
