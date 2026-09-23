#!/usr/bin/env python3
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import traceback

import av
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


def safe_audio_write_path(raw_path: str) -> Path:
    """Same workspace-containment check as safe_audio_path, but for an
    output file that doesn't exist yet."""
    path = Path(raw_path).resolve()
    if WORKSPACE_ROOT not in path.parents and path != WORKSPACE_ROOT:
        raise ValueError("output path must be inside /workspace")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def clip_audio_segment(input_path: Path, start_seconds: float, end_seconds: float, output_path: Path) -> float:
    """Extract [start_seconds, end_seconds) from an audio file into a new
    MP3 at output_path. Returns the actual clipped duration. Runs in this
    service (not the stdlib-only gateway) because it's the only container
    with a real audio-decoding dependency (av) installed."""
    if end_seconds <= start_seconds:
        raise ValueError("end_seconds must be greater than start_seconds")

    input_container = av.open(str(input_path))
    try:
        in_stream = input_container.streams.audio[0]
        output_container = av.open(str(output_path), mode="w")
        try:
            out_stream = output_container.add_stream("libmp3lame", rate=in_stream.rate)
            start_pts = int(start_seconds / in_stream.time_base)
            input_container.seek(start_pts, stream=in_stream)

            clipped_duration = 0.0
            for frame in input_container.decode(in_stream):
                if frame.time is None:
                    continue
                if frame.time < start_seconds:
                    continue
                if frame.time >= end_seconds:
                    break
                for packet in out_stream.encode(frame):
                    output_container.mux(packet)
                clipped_duration = frame.time - start_seconds

            for packet in out_stream.encode():
                output_container.mux(packet)
        finally:
            output_container.close()
    finally:
        input_container.close()

    return clipped_duration


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[whisper] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:
        if self.path == "/health":
            json_response(self, 200, {"ok": True, "model": MODEL_SIZE, "device": DEVICE})
            return
        json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/clip":
            self._handle_clip()
            return
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

    def _handle_clip(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            input_path = safe_audio_path(str(payload.get("path") or ""))
            output_path = safe_audio_write_path(str(payload.get("output_path") or ""))
            start_seconds = float(payload.get("start_seconds"))
            end_seconds = float(payload.get("end_seconds"))
            duration = clip_audio_segment(input_path, start_seconds, end_seconds, output_path)
            json_response(self, 200, {"ok": True, "output_path": str(output_path), "duration": duration})
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
