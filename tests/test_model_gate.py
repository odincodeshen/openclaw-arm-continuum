import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openclaw_runtime import model_gate


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now


class RequestsInFlightTest(unittest.TestCase):
    def test_vllm_metrics_running_plus_waiting(self):
        metrics = ('vllm:num_requests_running{engine="0",model_name="m"} 2.0\n'
                   'vllm:num_requests_waiting{engine="0",model_name="m"} 1.0\n# HELP x\n')
        with patch.object(model_gate, "_get", lambda url, timeout: metrics):
            self.assertEqual(model_gate.requests_in_flight("http://vllm:8000/v1"), 3)

    def test_llama_cpp_slots(self):
        def get(url, timeout):
            if url.endswith("/metrics"):
                raise OSError("no metrics")
            return '[{"id": 0, "is_processing": true}, {"id": 1, "is_processing": false}]'
        with patch.object(model_gate, "_get", get):
            self.assertEqual(model_gate.requests_in_flight("http://host:8080/v1"), 1)

    def test_unknown_server(self):
        def get(url, timeout):
            raise OSError("nothing here")
        with patch.object(model_gate, "_get", get):
            self.assertIsNone(model_gate.requests_in_flight("http://x/v1"))


class WaitTest(unittest.TestCase):
    def wait(self, busy_sequence, max_wait=600):
        fake, logs = FakeClock(), []
        values = iter(busy_sequence)
        with patch.object(model_gate, "requests_in_flight", lambda url: next(values)):
            waited = model_gate.wait_for_quiet_model("http://v/v1", "bot-a", max_wait=max_wait, poll=15, spread=0,
                                                     sleep=fake.sleep, clock=fake.clock, log=logs.append)
        return waited, logs

    def test_starts_after_two_quiet_checks(self):
        waited, logs = self.wait([3, 1, 0, 0])
        self.assertEqual(waited, 45)  # three polls of 15 s: busy, busy, quiet, then quiet again
        self.assertIn("waited 45s for a quiet model", logs[-1])

    def test_one_quiet_check_is_not_enough(self):
        waited, _ = self.wait([0, 2, 0, 0])
        self.assertEqual(waited, 45)

    def test_gives_up_after_max_wait(self):
        waited, logs = self.wait([1] * 100, max_wait=60)
        self.assertGreaterEqual(waited, 60)
        self.assertTrue(any("still busy after 1 min; going ahead" in line for line in logs))

    def test_unknown_server_doesnt_wait(self):
        waited, logs = self.wait([None])
        self.assertEqual((waited, logs), (0, []))

    def test_offsets_differ_per_bot_and_stay_put(self):
        a, b = model_gate.offset_seconds("apa_tracker_memory"), model_gate.offset_seconds("inv_tracker_memory")
        self.assertNotEqual(a, b)
        self.assertEqual(a, model_gate.offset_seconds("apa_tracker_memory"))
        self.assertTrue(0 <= a <= 120)

    def test_before_preparing_off_at_zero(self):
        with patch.object(model_gate, "wait_for_quiet_model") as wait:
            model_gate.before_preparing(SimpleNamespace(prepare_wait_minutes=0), "x")
            wait.assert_not_called()
            model_gate.before_preparing(SimpleNamespace(prepare_wait_minutes=30, vllm_base_url="http://v/v1",
                                                        tracker_collection="t"), "cron digest")
            self.assertEqual(wait.call_args.kwargs["max_wait"], 1800)


if __name__ == "__main__":
    unittest.main()
