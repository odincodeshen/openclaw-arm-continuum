#!/usr/bin/env python3
"""Local text-to-speech for pronunciation audio (Kokoro-82M on CPU).

POST /speak {"text": "...", "accent": "uk" | "us"} -> audio/ogg (Opus, mono,
48 kHz) -- ready to send as a Telegram voice message.
GET /health -> {"ok": true, "loaded": [...]}.

Runs on CPU so the GPU stays with the main model. Every clip is cached on
disk by (accent, text), so a word is only synthesized once. Nothing leaves
the host: the voice model is downloaded once into the Hugging Face cache.
"""

import hashlib
import io
import json
import os
import sys
import threading
import traceback
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("OPENCLAW_TTS_HOST", "0.0.0.0")
PORT = int(os.environ.get("OPENCLAW_TTS_PORT", "8766"))
CACHE_DIR = Path(os.environ.get("OPENCLAW_TTS_CACHE_DIR", "/cache/tts"))
MAX_TEXT_CHARS = int(os.environ.get("OPENCLAW_TTS_MAX_CHARS", "300"))
SAMPLE_RATE = 24000  # Kokoro's output rate

# accent -> (Kokoro lang_code, voice)
VOICES = {
    "uk": ("b", os.environ.get("OPENCLAW_TTS_VOICE_UK", "bf_emma")),
    "us": ("a", os.environ.get("OPENCLAW_TTS_VOICE_US", "af_heart")),
}

_pipelines: dict[str, object] = {}
_lock = threading.Lock()  # one synthesis at a time: torch on a small CPU budget


class BadRequest(ValueError):
    pass


def parse_request(body: bytes) -> tuple[str, str]:
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise BadRequest("body must be JSON") from exc
    text = " ".join(str(data.get("text") or "").split())
    accent = str(data.get("accent") or "uk").lower()
    if not text:
        raise BadRequest("text is empty")
    if len(text) > MAX_TEXT_CHARS:
        raise BadRequest(f"text is longer than {MAX_TEXT_CHARS} characters")
    if accent not in VOICES:
        raise BadRequest(f"accent must be one of {sorted(VOICES)}")
    return text, accent


def cache_path(text: str, accent: str) -> Path:
    voice = VOICES[accent][1]
    digest = hashlib.sha256(f"{voice}\n{text}".encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / accent / f"{digest}.ogg"


def pcm_to_ogg(pcm16: bytes, rate: int = SAMPLE_RATE) -> bytes:
    """16-bit mono PCM -> Ogg/Opus bytes (Telegram voice-message format)."""
    import av

    wav = io.BytesIO()
    with wave.open(wav, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm16)
    wav.seek(0)
    out_buffer = io.BytesIO()
    source = av.open(wav, format="wav")
    output = av.open(out_buffer, "w", format="ogg")
    stream = output.add_stream("libopus", rate=48000)
    stream.layout = "mono"
    resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
    for frame in source.decode(audio=0):
        for item in resampler.resample(frame):
            for packet in stream.encode(item):
                output.mux(packet)
    for item in resampler.resample(None):
        for packet in stream.encode(item):
            output.mux(packet)
    for packet in stream.encode():
        output.mux(packet)
    output.close()
    source.close()
    return out_buffer.getvalue()


def _pipeline(accent: str):
    if accent not in _pipelines:
        from kokoro import KPipeline

        _pipelines[accent] = KPipeline(lang_code=VOICES[accent][0], repo_id="hexgrad/Kokoro-82M")
    return _pipelines[accent]


def synthesize(text: str, accent: str) -> bytes:
    import numpy as np

    with _lock:
        pipeline = _pipeline(accent)
        chunks = [np.asarray(audio) for _, _, audio in pipeline(text, voice=VOICES[accent][1])]
    if not chunks:
        raise RuntimeError("no audio produced")
    audio = np.concatenate(chunks)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    return pcm_to_ogg(pcm)


def speak(text: str, accent: str) -> bytes:
    path = cache_path(text, accent)
    if path.exists():
        return path.read_bytes()
    audio = synthesize(text, accent)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(audio)
    tmp.replace(path)
    return audio


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True, "loaded": sorted(_pipelines)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/speak":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            text, accent = parse_request(self.rfile.read(length))
            audio = speak(text, accent)
        except BadRequest as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json(500, {"error": str(exc)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/ogg")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - keep the access log quiet
        pass


def main() -> int:
    if os.environ.get("OPENCLAW_TTS_PRELOAD", "true").lower() == "true":
        for accent in VOICES:
            try:
                _pipeline(accent)
            except Exception:  # noqa: BLE001 - loads on first request instead
                traceback.print_exc()
    print(f"[tts] listening on {HOST}:{PORT} voices={ {k: v[1] for k, v in VOICES.items()} }", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
