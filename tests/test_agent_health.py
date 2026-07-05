import http.server
import json
import threading
import unittest

from openclaw_runtime.agents.registry import AgentRegistry
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.skills.memory import MemoryWriteSkill, RagRetrieveSkill
from openclaw_runtime.skills.web_search import WebSearchSkill

from tests.support import UNREACHABLE_URL, build_settings


class _OKHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"ok": True, "result": {"collections": []}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def start_fake_server() -> tuple[http.server.HTTPServer, int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _OKHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


class LlmClientReachabilityTest(unittest.TestCase):
    def test_unreachable_vllm_reports_false(self) -> None:
        client = LlmClient(build_settings(vllm_base_url=UNREACHABLE_URL))
        self.assertFalse(client.is_reachable())

    def test_reachable_vllm_reports_true(self) -> None:
        server, port = start_fake_server()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        client = LlmClient(build_settings(vllm_base_url=f"http://127.0.0.1:{port}"))
        self.assertTrue(client.is_reachable())


class SkillHealthCheckTest(unittest.TestCase):
    def test_memory_write_reports_error_when_qdrant_down(self) -> None:
        settings = build_settings(qdrant_base_url=UNREACHABLE_URL)
        skill = MemoryWriteSkill(settings, {}, embeddings=None, qdrant=None)
        self.assertEqual(skill.health_check(), "error: Qdrant unreachable")

    def test_memory_write_reports_ready_when_qdrant_up(self) -> None:
        server, port = start_fake_server()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        settings = build_settings(qdrant_base_url=f"http://127.0.0.1:{port}")
        skill = MemoryWriteSkill(settings, {}, embeddings=None, qdrant=None)
        self.assertEqual(skill.health_check(), "ready")

    def test_rag_retrieve_reports_qdrant_error_before_checking_llm(self) -> None:
        settings = build_settings(qdrant_base_url=UNREACHABLE_URL, vllm_base_url=UNREACHABLE_URL)
        skill = RagRetrieveSkill(settings, {}, embeddings=None, qdrant=None, llm=LlmClient(settings))
        self.assertEqual(skill.health_check(), "error: Qdrant unreachable")

    def test_rag_retrieve_reports_llm_error_when_only_llm_down(self) -> None:
        server, port = start_fake_server()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        settings = build_settings(qdrant_base_url=f"http://127.0.0.1:{port}", vllm_base_url=UNREACHABLE_URL)
        skill = RagRetrieveSkill(settings, {}, embeddings=None, qdrant=None, llm=LlmClient(settings))
        self.assertEqual(skill.health_check(), "error: vLLM unreachable")

    def test_web_search_degrades_gracefully_when_scraper_down(self) -> None:
        settings = build_settings(scraper_base_url=UNREACHABLE_URL)
        skill = WebSearchSkill(settings, {}, llm=LlmClient(settings))
        self.assertIn("degraded", skill.health_check())

    def test_web_search_reports_ready_when_scraper_up(self) -> None:
        server, port = start_fake_server()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        settings = build_settings(scraper_base_url=f"http://127.0.0.1:{port}")
        skill = WebSearchSkill(settings, {}, llm=LlmClient(settings))
        self.assertEqual(skill.health_check(), "ready")


class AgentRegistryHealthTest(unittest.TestCase):
    def test_falls_back_to_ready_when_agent_has_no_health_check(self) -> None:
        class NoHealthAgent:
            name = "plain_agent"
            description = "no health_check method"

            def can_handle(self, task) -> bool:
                return True

            def run(self, task):
                raise NotImplementedError

        registry = AgentRegistry([NoHealthAgent()])
        statuses = registry.statuses()
        self.assertEqual(statuses[0].status, "ready")

    def test_surfaces_health_check_result(self) -> None:
        class DegradedAgent:
            name = "degraded_agent"
            description = "reports degraded"

            def can_handle(self, task) -> bool:
                return True

            def run(self, task):
                raise NotImplementedError

            def health_check(self) -> str:
                return "error: dependency unreachable"

        registry = AgentRegistry([DegradedAgent()])
        statuses = registry.statuses()
        self.assertEqual(statuses[0].status, "error: dependency unreachable")

    def test_health_check_exception_is_caught(self) -> None:
        class BrokenHealthAgent:
            name = "broken_agent"
            description = "health_check raises"

            def can_handle(self, task) -> bool:
                return True

            def run(self, task):
                raise NotImplementedError

            def health_check(self) -> str:
                raise RuntimeError("boom")

        registry = AgentRegistry([BrokenHealthAgent()])
        statuses = registry.statuses()
        self.assertIn("error", statuses[0].status)
        self.assertIn("boom", statuses[0].status)


if __name__ == "__main__":
    unittest.main()
