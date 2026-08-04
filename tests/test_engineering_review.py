import json
import tempfile
import unittest
from pathlib import Path

from openclaw_runtime.agents.base import Task
from openclaw_runtime.engineering_review import (
    EngineeringReviewAgent,
    RouteDecision,
    parse_specialist_result,
)
from openclaw_runtime.task_history import TaskHistory


def route_json(agents=None, confidence=0.9) -> str:
    return json.dumps(
        {
            "task_type": "engineering_review",
            "confidence": confidence,
            "required_agents": agents or ["code_review", "architecture_review"],
            "reason": "The request includes implementation and architecture review.",
        }
    )


def specialist_json(agent: str, title: str = "Risk") -> str:
    return json.dumps(
        {
            "agent": agent,
            "findings": [
                {
                    "severity": "high",
                    "category": "testing",
                    "title": title,
                    "evidence": "The request says tests are missing.",
                    "recommendation": "Add a regression test.",
                }
            ],
            "open_questions": ["Who owns the fix?"],
            "limitations": ["Only supplied text was reviewed."],
        }
    )


class FakeClient:
    def __init__(self, endpoint_id: str, responses: list[object]) -> None:
        self.endpoint_id = endpoint_id
        self.responses = list(responses)

    def chat(self, _prompt: str, **_kwargs) -> str:
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def chat_json(self, prompt: str, _schema: dict, **kwargs) -> str:
        return self.chat(prompt, **kwargs)

    def is_reachable(self) -> bool:
        return True


class FakeFactory:
    def __init__(self, clients: dict[str, FakeClient], fallbacks: dict[str, str] | None = None) -> None:
        self.clients = clients
        self.fallbacks = fallbacks or {}

    def get(self, policy: str) -> FakeClient:
        return self.clients[policy]

    def fallback_for(self, policy: str):
        fallback = self.fallbacks.get(policy)
        return self.clients[fallback] if fallback else None


class RouteDecisionTest(unittest.TestCase):
    def test_accepts_bounded_route(self) -> None:
        decision = RouteDecision.from_dict(json.loads(route_json()))
        self.assertEqual(decision.required_agents, ("code_review", "architecture_review"))

    def test_rejects_low_confidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "below threshold"):
            RouteDecision.from_dict(json.loads(route_json(confidence=0.2)))

    def test_rejects_unknown_agent(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown"):
            RouteDecision.from_dict(json.loads(route_json(["shell_agent"])))

    def test_rejects_duplicate_agent(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            RouteDecision.from_dict(json.loads(route_json(["code_review", "code_review"])))


class SpecialistOutputTest(unittest.TestCase):
    def test_parses_structured_finding(self) -> None:
        result = parse_specialist_result(specialist_json("code_review"), "code_review", "local_coder", "coder")
        self.assertEqual(result.findings[0].severity, "high")
        self.assertEqual(result.endpoint_id, "coder")

    def test_rejects_wrong_agent_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be code_review"):
            parse_specialist_result(specialist_json("architecture_review"), "code_review", "local_coder", "coder")


class EngineeringReviewAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history = TaskHistory(Path(self.tmp.name) / "history.jsonl")

    def make_agent(self, clients, fallbacks=None) -> EngineeringReviewAgent:
        return EngineeringReviewAgent(FakeFactory(clients, fallbacks), self.history)

    def test_explicit_review_command_is_deterministic(self) -> None:
        agent = self.make_agent({})
        task = Task("parent", "test", "/review sanitized package")
        self.assertTrue(agent.matches_explicit_command(task))
        self.assertTrue(agent.can_handle(task))

    def test_complete_workflow_records_specialist_metadata(self) -> None:
        clients = {
            "local_router": FakeClient("router", [route_json()]),
            "local_coder": FakeClient("coder", [specialist_json("code_review", "Code risk")]),
            "local_reasoner": FakeClient(
                "reasoner",
                [specialist_json("architecture_review", "Architecture risk"), "Consolidated review"],
            ),
        }
        agent = self.make_agent(clients)
        result = agent.run(Task("parent-1", "test", "/review implementation and architecture risks"))
        self.assertEqual(result.answer, "Consolidated review")
        entries = self.history.recent(10)
        self.assertEqual(len(entries), 3)
        self.assertEqual({entry["agent"] for entry in entries}, {"code_review", "architecture_review", "synthesis"})
        self.assertTrue(all(entry["parent_task_id"] == "parent-1" for entry in entries))
        self.assertTrue(all("input_summary" not in entry for entry in entries))

    def test_specialist_failure_produces_degraded_review(self) -> None:
        clients = {
            "local_router": FakeClient("router", [route_json()]),
            "local_coder": FakeClient("coder", [RuntimeError("offline")]),
            "local_reasoner": FakeClient(
                "reasoner",
                [specialist_json("architecture_review"), "Architecture-only report"],
            ),
        }
        result = self.make_agent(clients).run(Task("parent-2", "test", "/review engineering risk"))
        self.assertTrue(result.answer.startswith("DEGRADED REVIEW"))
        entries = self.history.recent(10)
        failed = next(entry for entry in entries if entry["agent"] == "code_review")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("offline", failed["error"])

    def test_specialist_fallback_is_recorded(self) -> None:
        clients = {
            "local_router": FakeClient("router", [route_json(["code_review"])]),
            "local_coder": FakeClient("coder", [RuntimeError("offline")]),
            "local_default": FakeClient("general", [specialist_json("code_review")]),
            "local_reasoner": FakeClient("reasoner", ["Fallback review"]),
        }
        result = self.make_agent(clients, {"local_coder": "local_default"}).run(
            Task("parent-3", "test", "/review source code")
        )
        self.assertEqual(result.answer, "Fallback review")
        entry = next(item for item in self.history.recent(5) if item["agent"] == "code_review")
        self.assertEqual(entry["endpoint_id"], "general")
        self.assertEqual(entry["fallback_from"], "coder")


if __name__ == "__main__":
    unittest.main()
