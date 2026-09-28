import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import openclaw_cron_worker as cron_worker
import openclaw_memory_watcher as watcher
from openclaw_runtime.alerts import Alerter
from openclaw_runtime.skills.base import SkillResult
from tests.support import build_settings


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _alerter(chat_ids=frozenset({7}), cooldown=60):
    sent = []
    clock = Clock()
    settings = build_settings(alert_chat_ids=set(chat_ids), alert_cooldown_minutes=cooldown, runtime_label="bot_a")
    alerter = Alerter(settings, send=lambda chat_id, text: sent.append((chat_id, text)), clock=clock, log=lambda m: None)
    return alerter, sent, clock


class AlerterTest(unittest.TestCase):
    def test_off_without_chat_ids(self) -> None:
        alerter, sent, _ = _alerter(chat_ids=())
        self.assertFalse(alerter.alert("k", "broken"))
        self.assertEqual(sent, [])

    def test_first_alert_then_quiet_until_the_cooldown_passes(self) -> None:
        alerter, sent, clock = _alerter()
        self.assertTrue(alerter.alert("k", "broken", "details"))
        self.assertEqual(sent, [(7, "OpenClaw alert · bot_a\nbroken\n\ndetails")])
        clock.now += 59 * 60
        self.assertFalse(alerter.alert("k", "broken"))
        clock.now += 2 * 60
        self.assertTrue(alerter.alert("k", "broken"))
        self.assertEqual(len(sent), 2)

    def test_keys_are_independent(self) -> None:
        alerter, sent, _ = _alerter()
        alerter.alert("a", "one")
        alerter.alert("b", "two")
        self.assertEqual(len(sent), 2)

    def test_recovered_only_after_an_alert_and_only_once(self) -> None:
        alerter, sent, _ = _alerter()
        self.assertFalse(alerter.resolve("k", "fine"))
        alerter.alert("k", "broken")
        self.assertTrue(alerter.resolve("k", "fine again"))
        self.assertFalse(alerter.resolve("k", "fine again"))
        self.assertEqual(sent[-1], (7, "OpenClaw recovered · bot_a\nfine again"))
        # after recovering, a new failure alerts straight away
        self.assertTrue(alerter.alert("k", "broken again"))

    def test_a_failing_send_never_raises(self) -> None:
        settings = build_settings(alert_chat_ids={7})

        def boom(chat_id, text):
            raise RuntimeError("telegram down")

        self.assertTrue(Alerter(settings, send=boom, log=lambda m: None).alert("k", "broken"))

    def test_long_details_are_cut(self) -> None:
        alerter, sent, _ = _alerter()
        alerter.alert("k", "broken", "x" * 5000)
        self.assertLess(len(sent[0][1]), 700)


class _Router:
    def __init__(self, outcome) -> None:
        self.outcome = outcome

    def route(self, prompt):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class CronAlertsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.alerter, self.sent, self.clock = _alerter()
        patches = [
            patch.object(cron_worker, "ALERTER", self.alerter),
            patch.object(cron_worker, "send_message", lambda settings, chat_id, text: None),
            patch.object(cron_worker, "log", lambda message: None),
            patch.object(cron_worker, "_gateway_down_since", None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.settings = build_settings(cron_chat_ids={1})
        self.job = {"id": "j1", "name": "Digest", "prompt": "/rag digest", "chat_id": 1}

    def test_failed_job_alerts_and_recovery_is_reported(self) -> None:
        now = datetime(2026, 9, 29, 7, 5)
        cron_worker.run_dynamic_job(self.settings, _Router(RuntimeError("vLLM down")), now, self.job)
        self.assertIn('Cron job "Digest" failed.', self.sent[0][1])
        self.assertIn("vLLM down", self.sent[0][1])
        cron_worker.run_dynamic_job(self.settings, _Router(SkillResult("rag", "ok")), now, self.job)
        self.assertIn('Cron job "Digest" is working again.', self.sent[1][1])

    def test_gateway_outage_alerts_only_after_it_lasts(self) -> None:
        settings = build_settings(cron_chat_ids={1}, alert_gateway_outage_minutes=10)
        times = iter([0.0, 5 * 60.0, 11 * 60.0])
        with patch.object(cron_worker, "list_gateway_jobs", side_effect=RuntimeError("HTTP 404")), \
                patch.object(cron_worker, "load_jobs", lambda path: {"jobs": []}), \
                patch.object(cron_worker.time, "time", lambda: next(times)):
            for _ in range(3):
                cron_worker.load_dynamic_jobs(settings)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Can't reach this bot's Gateway for 10+ minutes", self.sent[0][1])
        with patch.object(cron_worker, "list_gateway_jobs", return_value=[]):
            cron_worker.load_dynamic_jobs(settings)
        self.assertIn("The Gateway is reachable again", self.sent[1][1])


class WatcherAlertsTest(unittest.TestCase):
    def test_failed_files_alert_and_a_clean_scan_resolves(self) -> None:
        alerter, sent, _ = _alerter()
        failed = SimpleNamespace(path="/inbox/a.md", skipped=True, reason="failed:ConnectionError: ollama")
        watcher.check_ingest_health([failed], alerter)
        self.assertIn("can't index 1 file(s)", sent[0][1])
        self.assertIn("ollama", sent[0][1])
        watcher.check_ingest_health([SimpleNamespace(path="/inbox/b.md", skipped=True, reason="unchanged")], alerter)
        self.assertIn("indexing files again", sent[1][1])


if __name__ == "__main__":
    unittest.main()
