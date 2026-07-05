import tempfile
import unittest
from pathlib import Path

from openclaw_runtime.agents.base import Task
from openclaw_runtime.agents.dispatcher import TaskDispatcher
from openclaw_runtime.skills.base import SkillResult
from openclaw_runtime.task_history import TaskHistory


class FakeAgent:
    def __init__(self, name: str, result: SkillResult | None = None, error: Exception | None = None) -> None:
        self.name = name
        self._result = result
        self._error = error

    def can_handle(self, task: Task) -> bool:
        return True

    def run(self, task: Task) -> SkillResult:
        if self._error is not None:
            raise self._error
        return self._result


class FakeRegistry:
    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent

    def find(self, task: Task):
        return self.agent


class TaskDispatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history_path = Path(self.tmp.name) / "task_history.jsonl"
        self.history = TaskHistory(self.history_path)

    def test_success_returns_result_and_logs_two_entries(self) -> None:
        agent = FakeAgent("chat_agent", result=SkillResult("chat_agent", "hello back"))
        dispatcher = TaskDispatcher(FakeRegistry(agent), self.history)

        dispatch = dispatcher.dispatch("hello", source="telegram_text", chat_id=123)

        self.assertEqual(dispatch.agent_name, "chat_agent")
        self.assertEqual(dispatch.answer, "hello back")
        self.assertTrue(dispatch.task_id.startswith("task_"))
        self.assertGreaterEqual(dispatch.duration_ms, 0)

        entries = self.history.recent(10)
        self.assertEqual(len(entries), 2)
        started, success = entries[1], entries[0]  # recent() reverses order
        self.assertEqual(started["status"], "started")
        self.assertEqual(success["status"], "success")
        self.assertEqual(success["output_summary"], "hello back")

    def test_failure_reraises_and_logs_failed_entry(self) -> None:
        agent = FakeAgent("weather_agent", error=RuntimeError("wttr.in unreachable"))
        dispatcher = TaskDispatcher(FakeRegistry(agent), self.history)

        with self.assertRaises(RuntimeError):
            dispatcher.dispatch("英國今天天氣如何", source="telegram_text", chat_id=123)

        entries = self.history.recent(10)
        self.assertEqual(len(entries), 2)
        started, failed = entries[1], entries[0]
        self.assertEqual(started["status"], "started")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("wttr.in unreachable", failed["error"])
        self.assertIn("traceback", failed)

    def test_chat_id_and_metadata_are_recorded(self) -> None:
        agent = FakeAgent("chat_agent", result=SkillResult("chat_agent", "ok"))
        dispatcher = TaskDispatcher(FakeRegistry(agent), self.history)

        dispatcher.dispatch(
            "劍橋今天天氣如何",
            source="telegram_cron_run",
            chat_id=456,
            metadata={"job_id": "abc123", "job_name": "早報"},
        )

        entries = self.history.recent(10)
        started = entries[1]
        self.assertEqual(started["chat_id"], 456)
        self.assertEqual(started["metadata"], {"job_id": "abc123", "job_name": "早報"})

    def test_summary_truncates_long_text(self) -> None:
        long_text = "x" * 400
        summary = TaskDispatcher._summary(long_text)
        self.assertEqual(len(summary), 320)
        self.assertTrue(summary.endswith("…"))

    def test_summary_collapses_whitespace(self) -> None:
        messy = "hello   \n\n  world  \t foo"
        self.assertEqual(TaskDispatcher._summary(messy), "hello world foo")

    def test_task_ids_are_unique_across_dispatches(self) -> None:
        agent = FakeAgent("chat_agent", result=SkillResult("chat_agent", "ok"))
        dispatcher = TaskDispatcher(FakeRegistry(agent), self.history)

        ids = {dispatcher.dispatch("hi", source="telegram_text").task_id for _ in range(20)}
        self.assertEqual(len(ids), 20)


if __name__ == "__main__":
    unittest.main()
