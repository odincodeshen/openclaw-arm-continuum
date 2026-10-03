import dataclasses
import json
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import openclaw_telegram_gateway as gateway
from openclaw_runtime import checkins as ck
from tests.card_checks import assert_valid_telegram_html

PRESETS = Path(__file__).resolve().parent.parent / "app" / "openclaw_runtime" / "checkin_presets"
WORKLOG = ck.parse_spec((PRESETS / "worklog.toml").read_text(encoding="utf-8"))
LONDON = ZoneInfo("Europe/London")
OWNER = 77

MINIMAL = """
[checkin]
id = "reading"
title = "讀書心得"
[schedule]
days = ["sat"]
start = "10:00"
[[question]]
id = "book"
label = "Book"
prompt = "What are you reading?"
"""


class SpecTest(unittest.TestCase):
    def test_presets_parse(self) -> None:
        self.assertEqual(WORKLOG.steps, ["done", "blocked", "first"])
        self.assertEqual(sorted(WORKLOG.days), [0, 1, 2, 3, 4])
        self.assertEqual(WORKLOG.reports["week"].day, 4)  # Friday
        self.assertEqual(WORKLOG.reports["week"].period, "week_to_date")
        night = ck.parse_spec((PRESETS / "night.toml").read_text(encoding="utf-8"))
        self.assertEqual(night.steps, ["wins", "better", "adjust", "first"])

    def test_minimal_spec_has_sensible_defaults(self) -> None:
        spec = ck.parse_spec(MINIMAL)
        self.assertEqual((spec.command, spec.deadline, spec.follow_up, spec.morning, spec.reports), ("reading", "00:00", None, None, {}))
        self.assertEqual(spec.history_title, "讀書心得紀錄")

    def test_validation_errors(self) -> None:
        cases = {
            "id": MINIMAL.replace('id = "reading"', 'id = "Bad Id"'),
            "unknown question": MINIMAL + '\n[morning]\nfrom = "nope"\ntime = "08:00"\n',
            "reminder": MINIMAL.replace('start = "10:00"', 'start = "10:00"\nreminders = ["09:00"]'),
            "day": MINIMAL.replace('days = ["sat"]', 'days = ["someday"]'),
            "HH:MM": MINIMAL.replace('start = "10:00"', 'start = "25:00"'),
            "question": MINIMAL.split("[[question]]")[0],
            "toml": MINIMAL + "\n[broken",
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(ck.SpecError):
                ck.parse_spec(text)

    def test_load_reports_bad_files_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a.toml").write_text(MINIMAL, encoding="utf-8")
            (folder / "b.toml").write_text(MINIMAL, encoding="utf-8")
            (folder / "c.toml").write_text("[checkin]\nid='x'", encoding="utf-8")
            specs, problems = ck.load_specs(folder)
        self.assertEqual([s.id for s in specs], ["reading"])
        self.assertEqual(len(problems), 2)

    def test_deadline_and_periods(self) -> None:
        d = date(2026, 10, 2)
        self.assertFalse(ck.deadline_passed(WORKLOG, d, d, time(23, 59)))
        self.assertTrue(ck.deadline_passed(WORKLOG, d, date(2026, 10, 3), time(0, 1)))
        late = dataclasses.replace(WORKLOG, deadline="21:00")
        self.assertTrue(ck.deadline_passed(late, d, d, time(21, 0)))
        self.assertEqual(ck.report_period("week", date(2026, 10, 2), "week_to_date"), (date(2026, 9, 28), date(2026, 10, 2)))
        self.assertEqual(ck.reports_due(WORKLOG, date(2026, 10, 2)), ["week"])
        self.assertEqual(ck.reports_due(WORKLOG, date(2026, 10, 1)), ["month"])


class FakeLlm:
    def __init__(self):
        self.prompts = []

    def chat(self, prompt, max_tokens=None, history=None, persona=True):
        self.prompts.append(prompt)
        return "should not be used"

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None, persona=True):
        self.prompts.append(prompt)
        return json.dumps({"done_this_week": ["Shipped the O6 setup"], "recurring_blockers": "None."})


class WorklogFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        spec = dataclasses.replace(WORKLOG, data_dir=str(Path(self.tmp.name) / "worklog"), timezone="Europe/London")
        self.clock = datetime(2026, 10, 2, 18, 0, tzinfo=LONDON)  # a Friday
        p = patch.dict("os.environ", {"OPENCLAW_CHECKIN_OWNER": str(OWNER)})
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(gateway, "settings", dataclasses.replace(
            gateway.settings, pending_state_path=Path(self.tmp.name) / "pending.json", night_ritual_enabled=False,
            telegram_allowed_chat_ids=set()))
        p.start()
        self.addCleanup(p.stop)
        self.runtime = gateway.checkin_runtime(spec)
        self.runtime.now_fn = lambda: self.clock
        gateway.CHECKIN_RUNTIMES[:] = [self.runtime]
        self.addCleanup(gateway.CHECKIN_RUNTIMES.clear)
        self.addCleanup(gateway.CHECKIN_PENDING.clear)
        self.sent, self.api = [], []
        self.llm = FakeLlm()

        def fake_telegram(method, payload=None, timeout=60):
            self.api.append((method, payload or {}))
            if method == "sendMessage":
                self.sent.append(payload["text"])
            return {"ok": True, "result": {"message_id": 900 + len(self.api)}}

        class InlineThread:
            def __init__(inner, target=None, args=(), daemon=None):
                inner.target, inner.args = target, args

            def start(inner):
                inner.target(*inner.args)

        for name, value in [("telegram", fake_telegram), ("llm", self.llm),
                            ("send_message", lambda chat_id, text: self.sent.append(text)),
                            ("send_html", lambda chat_id, html: self.sent.append(html))]:
            q = patch.object(gateway, name, value)
            q.start()
            self.addCleanup(q.stop)
        q = patch.object(gateway.threading, "Thread", InlineThread)
        q.start()
        self.addCleanup(q.stop)

    def _say(self, text):
        self.assertTrue(gateway.handle_message({"chat": {"id": OWNER}, "text": text}) is None)

    def _tick(self, day, hh, mm, state):
        self.clock = datetime(2026, 10, day, hh, mm, tzinfo=LONDON)
        self.runtime.tick(self.clock, state)

    def test_a_working_day_and_the_week(self) -> None:
        state: dict = {}
        self._tick(1, 18, 0, state)  # Thursday
        self.assertTrue(self.sent[-1].startswith("<b>【收工紀錄 1/3】</b>· Done today"))
        saved = json.loads((Path(self.tmp.name) / "pending.json").read_text())
        self.assertEqual(saved["checkins"]["worklog"][str(OWNER)]["step"], "done")
        for answer in ["Wrote the spec\nFixed the fan", "none", "Deploy to the O6"]:
            self._say(answer)
        closing = [p for m, p in self.api if m == "sendMessage"][-1]
        assert_valid_telegram_html(self, closing["text"])
        self.assertTrue(closing["text"].startswith("<b>【今日收工】</b>· Thu 1 Oct"))
        self.assertIn("• Wrote the spec\n• Fixed the fan", closing["text"])
        self.assertNotIn("should not be used", closing["text"])  # model_line = "": no model call
        self.assertEqual(closing["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "ck:worklog:s:2026-10-01")

        self._tick(2, 7, 1, state)  # Friday morning
        self.assertIn("Deploy to the O6", self.sent[-1])
        self.assertIn("【今天的第一件事】", self.sent[-1])
        self._tick(2, 18, 0, state)  # Friday: the follow-up first
        check = [p for m, p in self.api if m == "sendMessage"][-1]
        self.assertIn("Did you start with it?", check["text"])
        data = check["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(data.startswith("ck:worklog:f:y:"))
        gateway.handle_callback_query({"id": "q", "data": data, "message": {"message_id": 1, "chat": {"id": OWNER}}})
        for answer in ["Set up the O6", "waiting on the board's fan", "Bots"]:
            self._say(answer)

        self._tick(2, 19, 30, state)  # the weekly report, same day
        report = self.sent[-1]
        assert_valid_telegram_html(self, report)
        self.assertTrue(report.startswith("<b>【工作週報】</b>· 28 Sep – 2 Oct"))
        self.assertIn("Mon ❌ Tue ❌ Wed ❌ Thu ✅ Fri ✅", report)
        self.assertIn("2 of 5 days", report)
        self.assertIn("<b>Done this week</b>\n• Shipped the O6 setup", report)
        self.assertIn("<b>First things</b>\n2 written · 1 done", report)
        self._tick(2, 19, 31, state)
        self.assertEqual(self.sent[-1], report)  # sent once

    def test_weekend_skipped_command_and_menu(self) -> None:
        state: dict = {}
        self._tick(3, 18, 0, state)  # Saturday
        self.assertEqual(self.sent, [])
        self._say("/worklog")
        self.assertIn("【工作紀錄】</b>· /worklog", self.sent[-1])
        self._say("/worklog nonsense")
        self.assertIn("收工紀錄 (18:00, Mon,Tue,Wed,Thu,Fri)", self.sent[-1])
        calls = []
        with patch.object(gateway, "telegram", lambda m, p=None, timeout=60: calls.append((m, p)) or {}):
            gateway.setup_bot_commands()
        self.assertIn("worklog", [c["command"] for c in calls[-1][1]["commands"]])
        self.assertIn("/worklog start", gateway.help_text())

    def test_other_chats_and_missing_owner(self) -> None:
        self.assertFalse(self.runtime.handle_reply(5, {"text": "hi"}))
        with patch.dict("os.environ", {"OPENCLAW_CHECKIN_OWNER": ""}):
            self.assertIsNone(gateway._checkin_owner(WORKLOG))


class LoadRuntimesTest(unittest.TestCase):
    def test_folder_with_presets_and_night_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "worklog.toml").write_text((PRESETS / "worklog.toml").read_text(), encoding="utf-8")
            (folder / "night.toml").write_text((PRESETS / "night.toml").read_text(), encoding="utf-8")
            settings = dataclasses.replace(gateway.settings, checkin_dir=folder, night_ritual_enabled=True)
            with patch.object(gateway, "settings", settings), \
                    patch.dict("os.environ", {"OPENCLAW_CHECKIN_OWNER": "1", "OPENCLAW_NIGHT_RITUAL_OWNER": "1"}):
                runtimes = gateway.load_checkin_runtimes()
        self.assertEqual([r.spec.id for r in runtimes], ["worklog"])  # night is the built-in one here


if __name__ == "__main__":
    unittest.main()
