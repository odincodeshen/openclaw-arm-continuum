import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import openclaw_cron_worker as cron_worker
from openclaw_runtime.skills.base import SkillResult

from tests.support import build_settings


class _FakeRouter:
    def __init__(self, result: SkillResult) -> None:
        self.result = result

    def route(self, prompt: str) -> SkillResult:
        return self.result


class RunDynamicJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = build_settings(cron_chat_ids={123}, inbox_path=Path(self.tmp.name))
        self.sent: list[tuple[int, str]] = []
        self._orig_send = cron_worker.send_message
        cron_worker.send_message = lambda settings, chat_id, text: self.sent.append((chat_id, text))
        self.addCleanup(setattr, cron_worker, "send_message", self._orig_send)

    def _job(self) -> dict:
        return {"id": "j1", "name": "Memory digest", "prompt": "/mem digest", "chat_id": 123}

    def test_normal_result_is_delivered(self) -> None:
        router = _FakeRouter(SkillResult("memory_write", "3 things due this week"))
        result = cron_worker.run_dynamic_job(self.settings, router, datetime(2026, 1, 1, 8, 0), self._job())
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["delivered"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("3 things due this week", self.sent[0][1])

    def test_suppressed_result_is_recorded_but_not_delivered(self) -> None:
        router = _FakeRouter(
            SkillResult("memory_write", "You're all caught up.", suppress_if_routine=True)
        )
        result = cron_worker.run_dynamic_job(self.settings, router, datetime(2026, 1, 1, 8, 0), self._job())
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["delivered"])
        self.assertEqual(self.sent, [])

    def test_result_is_pushed_but_never_saved_to_the_inbox(self) -> None:
        router = _FakeRouter(SkillResult("memory_write", "3 things due this week"))
        cron_worker.run_dynamic_job(self.settings, router, datetime(2026, 1, 1, 8, 0), self._job())
        self.assertEqual(list(Path(self.tmp.name).rglob("*")), [])

    def test_message_is_just_the_name_and_result(self) -> None:
        router = _FakeRouter(SkillResult("memory_write", "3 things due this week"))
        cron_worker.run_dynamic_job(self.settings, router, datetime(2026, 1, 1, 8, 0), self._job())
        self.assertEqual(self.sent[0][1], "Memory digest\n\n3 things due this week")

    def test_result_that_already_carries_the_title_is_not_prefixed_twice(self) -> None:
        router = _FakeRouter(SkillResult("memory_write", "Memory digest\n---\nnothing"))
        cron_worker.run_dynamic_job(self.settings, router, datetime(2026, 1, 1, 8, 0), self._job())
        self.assertEqual(self.sent[0][1], "Memory digest\n---\nnothing")

    def test_router_exception_is_an_error_not_a_skip(self) -> None:
        # An error still gets delivered (you want to know a job failed) --
        # only a routine "nothing new" result is suppressed.
        class _RaisingRouter:
            def route(self, prompt: str) -> SkillResult:
                raise RuntimeError("boom")

        result = cron_worker.run_dynamic_job(self.settings, _RaisingRouter(), datetime(2026, 1, 1, 8, 0), self._job())
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["delivered"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Task failed: boom", self.sent[0][1])


class WriteGatewayRunbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = build_settings()
        self.captured: list[dict] = []
        self._orig = cron_worker.update_gateway_job_state
        cron_worker.update_gateway_job_state = lambda settings, job_id, state: self.captured.append(state)
        self.addCleanup(setattr, cron_worker, "update_gateway_job_state", self._orig)

    def _run(self, gateway_state: dict, result: dict) -> dict:
        job = {"id": "j1", "gateway_state": gateway_state}
        cron_worker.write_gateway_runback(self.settings, job, datetime(2026, 1, 1), result)
        return self.captured[-1]

    def test_error_increments_errors_and_resets_skipped(self) -> None:
        state = self._run(
            {"consecutiveErrors": 2, "consecutiveSkipped": 3},
            {"status": "error", "error": "boom", "delivered": False, "duration_ms": 5},
        )
        self.assertEqual(state["consecutiveErrors"], 3)
        self.assertEqual(state["consecutiveSkipped"], 0)
        self.assertEqual(state["lastDeliveryStatus"], "not-delivered")
        self.assertEqual(state["lastError"], "boom")

    def test_skipped_increments_skipped_and_resets_errors(self) -> None:
        state = self._run(
            {"consecutiveErrors": 2, "consecutiveSkipped": 1},
            {"status": "skipped", "error": None, "delivered": False, "duration_ms": 5},
        )
        self.assertEqual(state["consecutiveErrors"], 0)
        self.assertEqual(state["consecutiveSkipped"], 2)
        self.assertEqual(state["lastDeliveryStatus"], "skipped")
        self.assertEqual(state["lastRunStatus"], "skipped")
        self.assertIsNone(state["lastError"])

    def test_ok_resets_both_counters(self) -> None:
        state = self._run(
            {"consecutiveErrors": 2, "consecutiveSkipped": 3},
            {"status": "ok", "error": None, "delivered": True, "duration_ms": 5},
        )
        self.assertEqual(state["consecutiveErrors"], 0)
        self.assertEqual(state["consecutiveSkipped"], 0)
        self.assertEqual(state["lastDeliveryStatus"], "delivered")


if __name__ == "__main__":
    unittest.main()
