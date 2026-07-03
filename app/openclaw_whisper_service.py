#!/usr/bin/env python3
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import traceback

from faster_whisper import WhisperModel


HOST = os.environ.get("OPENCLAW_WHISPER_HOST", "0.0.0.0")
PORT = int(os.environ.get("OPENCLAW_WHISPER_PORT", "8765"))
MODEL_SIZE = os.environ.get("OPENCLAW_WHISPER_MODEL", "base")
DEVICE = os.environ.get("OPENCLAW_WHISPER_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("OPENCLAW_WHISPER_COMPUTE_TYPE", "int8")
WORKSPACE_ROOT = Path(os.environ.get("OPENCLAW_WORKSPACE_ROOT", "/workspace")).resolve()

model: WhisperModel | None = None


def get_model() -> WhisperModel:
    global model
    if model is None:
        print(
            f"[whisper] loading model={MODEL_SIZE} device={DEVICE} compute_type={COMPUTE_TYPE}",
            flush=True,
        )
        model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
    return model


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def safe_audio_path(raw_path: str) -> Path:
    path = Path(raw_path).resolve()
    if WORKSPACE_ROOT not in path.parents and path != WORKSPACE_ROOT:
        raise ValueError("audio path must be inside /workspace")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(str(path))
    return path


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[whisper] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:
        if self.path == "/health":
            json_response(self, 200, {"ok": True, "model": MODEL_SIZE, "device": DEVICE})
            return
        json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/transcribe":
            json_response(self, 404, {"error": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            audio_path = safe_audio_path(str(payload.get("path") or ""))
            language = payload.get("language")
            segments, info = get_model().transcribe(
                str(audio_path),
                language=language,
                vad_filter=True,
                beam_size=5,
            )
            text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
            json_response(
                self,
                200,
                {
                    "text": text.strip(),
                    "language": getattr(info, "language", None),
                    "duration": getattr(info, "duration", None),
                },
            )
        except Exception as exc:
            traceback.print_exc()
            json_response(self, 500, {"error": str(exc)})


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[whisper] listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
