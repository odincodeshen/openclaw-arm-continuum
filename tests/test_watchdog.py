import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "openclaw_watchdog", Path(__file__).resolve().parent.parent / "scripts" / "openclaw_watchdog.py"
)
watchdog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watchdog)

HOUR = 3600.0


def _c(cid="a", status="running", restarts=0, exit_code=0, oom=False, health=""):
    return {"id": cid, "status": status, "restart_count": restarts, "exit_code": exit_code,
            "oom_killed": oom, "health": health}


class WatchdogEvaluateTest(unittest.TestCase):
    def test_first_run_only_records_a_baseline(self) -> None:
        messages, state = watchdog.evaluate({}, {"openclaw-cron-x": _c(restarts=3)}, "boot1", 0.0)
        self.assertEqual(messages, [])
        self.assertEqual(state["containers"]["openclaw-cron-x"], {"id": "a", "restart_count": 3})

    def test_a_crash_restart_alerts_with_its_cause(self) -> None:
        _, state = watchdog.evaluate({}, {"openclaw-cron-x": _c()}, "boot1", 0.0)
        messages, state = watchdog.evaluate(state, {"openclaw-cron-x": _c(restarts=1, exit_code=1)}, "boot1", 300.0)
        self.assertEqual(messages, ["openclaw-cron-x crashed and was restarted by Docker (1x since the last check; exit code 1)."])
        messages, _ = watchdog.evaluate(state, {"openclaw-cron-x": _c(restarts=2, oom=True, exit_code=137)}, "boot1", 600.0)
        self.assertIn("ran out of memory (OOM-killed)", messages[0])

    def test_restart_or_recreate_by_hand_does_not_alert(self) -> None:
        _, state = watchdog.evaluate({}, {"openclaw-cron-x": _c(restarts=2)}, "boot1", 0.0)
        # docker restart / openclawctl restart: same id, same count
        self.assertEqual(watchdog.evaluate(state, {"openclaw-cron-x": _c(restarts=2)}, "boot1", 300.0)[0], [])
        # recreated: a new container id restarts the baseline
        self.assertEqual(watchdog.evaluate(state, {"openclaw-cron-x": _c(cid="b", restarts=5)}, "boot1", 300.0)[0], [])

    def test_a_stopped_container_does_not_alert(self) -> None:
        _, state = watchdog.evaluate({}, {"openclaw-telegram-old": _c()}, "boot1", 0.0)
        messages, _ = watchdog.evaluate(state, {"openclaw-telegram-old": _c(status="exited", exit_code=137)}, "boot1", 300.0)
        self.assertEqual(messages, [])

    def test_a_stopped_container_left_unhealthy_does_not_alert(self) -> None:
        messages, state = watchdog.evaluate({}, {"old": _c(status="exited", health="unhealthy")}, "b", 0.0)
        self.assertEqual((messages, state["alerted"]), ([], {}))

    def test_crash_loop_alerts_once_then_recovers(self) -> None:
        state = {}
        messages, state = watchdog.evaluate(state, {"x": _c(status="restarting")}, "b", 0.0)
        self.assertEqual(messages, ["x keeps restarting -- it can't stay up."])
        self.assertEqual(watchdog.evaluate(state, {"x": _c(status="restarting")}, "b", HOUR)[0], [])
        messages, _ = watchdog.evaluate(state, {"x": _c(status="running")}, "b", 2 * HOUR)
        self.assertEqual(messages, ["x is running again."])

    def test_unhealthy_alerts_and_recovers(self) -> None:
        messages, state = watchdog.evaluate({}, {"gw": _c(health="unhealthy")}, "b", 0.0)
        self.assertEqual(messages, ["gw is running but its health check fails."])
        messages, _ = watchdog.evaluate(state, {"gw": _c(health="healthy")}, "b", 300.0)
        self.assertEqual(messages, ["gw is healthy again."])

    def test_host_reboot(self) -> None:
        _, state = watchdog.evaluate({}, {}, "boot1", 0.0)
        messages, state = watchdog.evaluate(state, {}, "boot2", 300.0)
        self.assertEqual(messages, ["The host rebooted; containers set to restart came back on their own."])
        self.assertEqual(state["boot_id"], "boot2")


if __name__ == "__main__":
    unittest.main()
