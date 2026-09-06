import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from openclaw_runtime.agents.base import Task
from openclaw_runtime.engineering_review import EngineeringReviewAgent
from openclaw_runtime.model_catalog import ModelRegistry, ModelSpec
from openclaw_runtime.model_client_factory import ModelClientFactory
from openclaw_runtime.task_history import TaskHistory
from tests.support import build_settings


class SyntheticOpenAIHandler(BaseHTTPRequestHandler):
    role = "general"

    def do_GET(self):
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": f"synthetic-{self.role}"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        prompt = str(payload.get("messages", [{}])[-1].get("content", ""))
        content = self._response(prompt)
        self._json(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})

    def _response(self, prompt: str) -> str:
        if "Classify this request" in prompt:
            return json.dumps(
                {
                    "task_type": "engineering_review",
                    "confidence": 0.96,
                    "required_agents": ["code_review", "architecture_review"],
                    "reason": "Synthetic package contains implementation and architecture material.",
                }
            )
        if "You are the code_review specialist" in prompt:
            return self._specialist("code_review", "Missing idempotency and concurrency controls")
        if "You are the architecture_review specialist" in prompt:
            return self._specialist("architecture_review", "Process-local state is not durable or shared")
        return "Synthetic consolidated review with sourced findings and explicit limitations."

    @staticmethod
    def _specialist(agent: str, title: str) -> str:
        return json.dumps(
            {
                "agent": agent,
                "findings": [
                    {
                        "severity": "high",
                        "category": "synthetic_validation",
                        "title": title,
                        "evidence": "The sanitized fixture explicitly describes this behavior.",
                        "recommendation": "Use durable shared state and add bounded retry tests.",
                    }
                ],
                "open_questions": [],
                "limitations": ["Synthetic endpoint validation only."],
            }
        )

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class SyntheticEndpoint:
    def __init__(self, role: str) -> None:
        handler = type(f"{role.title()}Handler", (SyntheticOpenAIHandler,), {"role": role})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class EngineeringReviewSyntheticIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.endpoints = {role: SyntheticEndpoint(role) for role in ("general", "router", "coder", "reasoner")}
        for endpoint in self.endpoints.values():
            endpoint.start()
        self.addCleanup(self._stop_endpoints)
        specs = [
            ModelSpec("local_default", self.endpoints["general"].base_url, "synthetic-general", ("general",), 3),
            ModelSpec("local_router", self.endpoints["router"].base_url, "synthetic-router", ("classification",), 3, fallback="local_default"),
            ModelSpec("local_coder", self.endpoints["coder"].base_url, "synthetic-coder", ("code_review",), 3, fallback="local_default"),
            ModelSpec(
                "local_reasoner",
                self.endpoints["reasoner"].base_url,
                "synthetic-reasoner",
                ("architecture_review", "synthesis"),
                3,
                fallback="local_default",
            ),
        ]
        self.registry = ModelRegistry(specs)
        self.history = TaskHistory(Path(self.tmp.name) / "history.jsonl")
        self.factory = ModelClientFactory(build_settings(), self.registry)

    def _stop_endpoints(self) -> None:
        for endpoint in self.endpoints.values():
            try:
                endpoint.stop()
            except Exception:
                pass

    def test_real_http_workflow_and_coder_fallback(self) -> None:
        agent = EngineeringReviewAgent(self.factory, self.history)
        request = Path(__file__).resolve().parents[1] / "examples" / "engineering-review" / "sanitized-design-package.md"
        task = Task("synthetic-parent-success", "dgx_synthetic", "/review " + request.read_text(encoding="utf-8"))

        result = agent.run(task)

        self.assertEqual(result.status, "success")
        entries = self.history.recent(10)
        self.assertEqual({entry["endpoint_id"] for entry in entries}, {"local_coder", "local_reasoner"})
        self.assertEqual({entry["agent"] for entry in entries}, {"code_review", "architecture_review", "synthesis"})

        self.endpoints["coder"].stop()
        fallback_task = Task("synthetic-parent-fallback", "dgx_synthetic", task.text)
        fallback_result = agent.run(fallback_task)

        self.assertEqual(fallback_result.status, "success")
        fallback_entries = [entry for entry in self.history.recent(10) if entry.get("task_id") == fallback_task.task_id]
        code_entry = next(entry for entry in fallback_entries if entry["agent"] == "code_review")
        self.assertEqual(code_entry["endpoint_id"], "local_default")
        self.assertEqual(code_entry["fallback_from"], "local_coder")


if __name__ == "__main__":
    unittest.main()
