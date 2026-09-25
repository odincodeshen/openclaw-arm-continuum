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

# One shared whisper container serves every bot persona, but each persona's
# gateway mounts its OWN host workspace directory at its own container's
# /workspace -- this container only ever saw the single default /workspace,
# so it couldn't see any persona-specific file. EXTRA_ALLOWED_ROOTS
# generalizes the containment check to accept additional roots: the legacy
# WORKSPACE_ROOT stays the primary one (and stays live-patchable by tests,
# see _is_under_an_allowed_root reading it fresh each call rather than
# baking it into a snapshot list), plus (once mounted, see compose.yaml) a
# /profiles root under which every persona's own workspace lives, namespaced
# by persona so one shared container can serve all of them unambiguously.
EXTRA_ALLOWED_ROOTS = [
    Path(root).resolve()
    for root in os.environ.get("OPENCLAW_WHISPER_ALLOWED_ROOTS", "").split(",")
    if root.strip()
]

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


def _is_under_an_allowed_root(path: Path) -> bool:
    # WORKSPACE_ROOT is read fresh here (not baked into a snapshot list) so
    # tests that monkeypatch it directly (see test_audio_clip.py) still work.
    roots = [WORKSPACE_ROOT, *EXTRA_ALLOWED_ROOTS]
    return any(root in path.parents or path == root for root in roots)


def safe_audio_path(raw_path: str) -> Path:
    path = Path(raw_path).resolve()
    if not _is_under_an_allowed_root(path):
        raise ValueError(f"audio path must be inside one of {[WORKSPACE_ROOT, *EXTRA_ALLOWED_ROOTS]}")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(str(path))
    return path


def safe_audio_write_path(raw_path: str) -> Path:
    """Same allowed-roots containment check as safe_audio_path, but for an
    output file that doesn't exist yet."""
    path = Path(raw_path).resolve()
    if not _is_under_an_allowed_root(path):
        raise ValueError(f"output path must be inside one of {[WORKSPACE_ROOT, *EXTRA_ALLOWED_ROOTS]}")
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
            # segments is a lazy generator -- consume once, building both the
            # joined text (existing callers, e.g. voice-message transcription)
            # and the per-segment timestamp list (new: Monday/Tuesday need
            # real start/end times to pick a window and quote it back
            # accurately, not just plain text).
            segment_list = []
            texts = []
            for segment in segments:
                stripped = segment.text.strip()
                if not stripped:
                    continue
                texts.append(stripped)
                segment_list.append({"start": segment.start, "end": segment.end, "text": stripped})
            text = " ".join(texts)
            json_response(
                self,
                200,
                {
                    "text": text.strip(),
                    "language": getattr(info, "language", None),
                    "duration": getattr(info, "duration", None),
                    "segments": segment_list,
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
