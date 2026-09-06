"""In-process fake inference + embedding server for L2 integration tests.

Implements just enough of the OpenAI ``/v1`` and Ollama ``/api`` surfaces to
exercise the real ingest -> Qdrant -> retrieve -> format pipeline without a GPU:

* ``GET  /v1/models``           -> one fake model
* ``POST /v1/chat/completions`` -> echoes the tail of the last user message, so a
  scenario can assert what context was actually fed to the model
* ``POST /api/embed``           -> deterministic bag-of-words vector (Ollama new)
* ``POST /api/embeddings``      -> same vector, legacy response shape

The embedding is deterministic and word-based, so cosine similarity is
meaningful: two texts that share words get a non-zero dot product. A query
about "batteries" therefore retrieves the "batteries" document and not the
"tax return" one -- which is exactly what the isolation/retrieval scenarios
need to assert.
"""

from __future__ import annotations

import json
import re
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def embed(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    for tok in _TOKEN.findall((text or "").lower()):
        vec[zlib.crc32(tok.encode("utf-8")) % dim] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    if norm:
        return [v / norm for v in vec]
    out = [0.0] * dim
    out[0] = 1.0
    return out


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # OpenAI multimodal parts
                return " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
    return ""


class _Handler(BaseHTTPRequestHandler):
    dim = 64
    chat_responder = None  # optional: callable(messages) -> str

    def log_message(self, *args):  # noqa: D401 - silence test noise
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0].rstrip("/") == "/v1/models":
            self._send({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            req = {}
        path = self.path.split("?", 1)[0].rstrip("/")

        if path == "/v1/chat/completions":
            messages = req.get("messages", [])
            if callable(self.chat_responder):
                answer = self.chat_responder(messages)
            else:
                answer = "[fake-llm] " + _last_user_text(messages).strip()[:1200]
            self._send(
                {
                    "id": "fake",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": answer},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        elif path == "/api/embed":
            raw = req.get("input")
            texts = raw if isinstance(raw, list) else [raw or ""]
            self._send({"embeddings": [embed(text, self.dim) for text in texts]})
        elif path == "/api/embeddings":
            self._send({"embedding": embed(req.get("prompt", ""), self.dim)})
        else:
            self._send({"error": "not found"}, 404)


class FakeInferenceServer:
    """Context manager. ``with FakeInferenceServer(dim=64) as fake: ...``."""

    def __init__(self, *, dim: int = 64, chat_responder=None) -> None:
        handler = type(
            "_BoundHandler",
            (_Handler,),
            {"dim": dim, "chat_responder": staticmethod(chat_responder) if chat_responder else None},
        )
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def openai_base_url(self) -> str:
        return f"{self.base}/v1"

    @property
    def ollama_base_url(self) -> str:
        return self.base

    def __enter__(self) -> "FakeInferenceServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)
