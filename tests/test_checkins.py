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


class WorklogHarness:
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

    def _tap(self, data):
        gateway.handle_callback_query({"id": "q", "data": data, "message": {"message_id": 1, "chat": {"id": OWNER}}})

    def _buttons(self):
        """callback_data of the last message with buttons."""
        markup = [p for m, p in self.api if m == "sendMessage" and p.get("reply_markup")][-1]["reply_markup"]
        return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]

    def _entry(self, day):
        return self.runtime.store_fn().load(str(OWNER), date(2026, 10, day) if isinstance(day, int) else day)


class WorklogFlowTest(WorklogHarness, unittest.TestCase):
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


class SkipFillInTest(WorklogHarness, unittest.TestCase):
    """Skip today / ahead, holidays, filling in an earlier day, editing an
    answer, and the after-midnight question."""

    def test_skip_button_counts_as_skipped_not_missed(self) -> None:
        state: dict = {}
        self._tick(1, 18, 0, state)  # Thursday
        self.assertIn("ck:worklog:a:skip:2026-10-01", self._buttons())
        self._tap("ck:worklog:a:skip:2026-10-01")
        self.assertEqual(self._entry(1)["status"], "skipped")
        self.assertIsNone(self.runtime.peek(OWNER))
        sent = len(self.sent)
        self._tick(1, 19, 0, state)
        self.assertEqual(len(self.sent), sent)  # no reminder for a skipped day
        self._tick(2, 18, 0, state)  # Friday: nothing to follow up, straight to question 1
        self.assertIn("【收工紀錄 1/3】", self.sent[-1])
        for answer in ["Shipped", "none", "Bots"]:
            self._say(answer)
        self._tick(2, 19, 30, state)
        self.assertIn("Thu ⏸ Fri ✅", self.sent[-1])
        self.assertIn("1 of 4 days · 1 skipped", self.sent[-1])

    def test_reminder_has_skip_button(self) -> None:
        state: dict = {}
        self._tick(1, 18, 0, state)
        self._tick(1, 19, 0, state)
        self.assertIn("Last call", [p for m, p in self.api if m == "sendMessage"][-1]["text"])
        self.assertIn("ck:worklog:a:skip:2026-10-01", self._buttons())

    def test_skip_ahead_holidays_and_unskip(self) -> None:
        folder = Path(self.tmp.name) / "checkins"
        folder.mkdir()
        (folder / "holidays.txt").write_text("# bank holidays\n2026-10-06\nnot-a-date\n", encoding="utf-8")
        p = patch.object(gateway, "settings", dataclasses.replace(gateway.settings, checkin_dir=folder))
        p.start()
        self.addCleanup(p.stop)
        self.clock = datetime(2026, 10, 2, 9, 0, tzinfo=LONDON)
        self._say("/worklog skip 2026-10-05")
        self.assertIn("Mon 5 Oct skipped", self.sent[-1])
        state: dict = {}
        sent = len(self.sent)
        self._tick(5, 18, 0, state)  # skipped ahead
        self._tick(6, 18, 0, state)  # holiday from the file
        self.assertEqual(len(self.sent), sent)
        self._say("/worklog")
        self.assertIn("Mon 5 ⏸", self.sent[-1])
        self.assertIn("Tue 6 ⏸", self.sent[-1])
        self._say("/worklog unskip 2026-10-05")
        self.assertIn("no longer skipped", self.sent[-1])
        self.assertIsNone(self._entry(5))
        self.assertTrue(any("not-a-date" in problem for problem in gateway.checkin_holidays()[1]))

    def test_fill_in_an_earlier_day_and_edit_an_answer(self) -> None:
        state: dict = {}
        self._tick(1, 18, 0, state)  # Thursday, left unanswered
        self._tick(2, 0, 1, state)  # midnight: saved as missed
        self.assertEqual(self._entry(1)["status"], "missed")
        self.clock = datetime(2026, 10, 2, 9, 0, tzinfo=LONDON)
        self._say("/worklog start 2026-10-01")
        self.assertIn("Filling in Thu 1 Oct.", self.sent)
        self._tick(2, 10, 0, state)  # still open: a filled-in day has 3 hours
        self.assertEqual(self.runtime.peek(OWNER)["date"], "2026-10-01")
        for answer in ["Wrote the spec", "none", "Deploy"]:
            self._say(answer)
        entry = self._entry(1)
        self.assertEqual(entry["status"], "done")
        self.assertIn("backfilled_at", entry)
        self._say("/worklog edit 2026-10-01 2 Waiting on the Wi-Fi")
        self.assertEqual(self._entry(1)["answers"]["blocked"], "Waiting on the Wi-Fi")
        self.assertIn("Updated.", self.sent[-1])
        self._say("/worklog edit 2026-10-01 9 x")
        self.assertIn("Which question? 1=done, 2=blocked, 3=first", self.sent[-1])
        self._say("/worklog start 2026-09-20")
        self.assertIn("Only the last 7 days", self.sent[-1])

    def test_fill_in_survives_schedule_ticks_between_answers(self) -> None:
        state: dict = {}
        self._tick(1, 18, 0, state)
        self._tick(2, 0, 1, state)
        self.clock = datetime(2026, 10, 2, 8, 39, tzinfo=LONDON)
        self._say("/worklog start 2026-10-01")
        for minute, answer in ((40, "Wrote the spec"), (42, "none"), (44, "Deploy")):
            self._tick(2, 8, minute - 1, state)  # the 30-second schedule pass runs between replies
            self.clock = datetime(2026, 10, 2, 8, minute, tzinfo=LONDON)
            self._say(answer)
        self.assertEqual(self._entry(1)["status"], "done")
        self.assertEqual(len(self._entry(1)["answers"]), 3)

    def test_unfinished_fill_in_closes_after_three_hours(self) -> None:
        state: dict = {}
        self.clock = datetime(2026, 10, 2, 9, 0, tzinfo=LONDON)
        self._say("/worklog start 2026-10-01")
        self._say("Half an answer")
        self._tick(2, 12, 1, state)
        self.assertIsNone(self.runtime.peek(OWNER))
        self.assertEqual(self._entry(1)["status"], "partial")

    def test_after_midnight_asks_which_day(self) -> None:
        state: dict = {}
        self._tick(1, 18, 0, state)  # Thursday, unanswered until after midnight
        self._tick(2, 0, 1, state)
        self.clock = datetime(2026, 10, 2, 0, 8, tzinfo=LONDON)
        self._say("/worklog start")
        self.assertIn("Which day?", [p for m, p in self.api if m == "sendMessage"][-1]["text"])
        self.assertEqual(self._buttons(), ["ck:worklog:a:day:2026-10-01", "ck:worklog:a:day:2026-10-02"])
        self._tap("ck:worklog:a:day:2026-10-01")
        for answer in ["Late work", "none", "Sleep"]:
            self._say(answer)
        self.assertEqual(self._entry(1)["status"], "done")
        self.assertIsNone(self._entry(2))

    def test_after_midnight_choosing_today(self) -> None:
        self.clock = datetime(2026, 10, 2, 0, 8, tzinfo=LONDON)  # Friday; Thursday has nothing
        self._say("/worklog start")
        self._tap("ck:worklog:a:day:2026-10-02")
        self.assertEqual(self.runtime.peek(OWNER)["date"], "2026-10-02")

    def test_no_question_in_the_daytime(self) -> None:
        self.clock = datetime(2026, 10, 2, 9, 0, tzinfo=LONDON)
        self._say("/worklog start")
        self.assertEqual(self.runtime.peek(OWNER)["date"], "2026-10-02")


class SkipDatesEngineTest(unittest.TestCase):
    def test_parse_skip_dates_and_ranges(self) -> None:
        days = ck.parse_skip_dates(["2026-12-25", "2026-08-03..2026-08-05"], "x")
        self.assertEqual(len(days), 4)
        with self.assertRaises(ck.SpecError):
            ck.parse_skip_dates(["2026-08-05..2026-08-03"], "x")
        spec = ck.parse_spec(MINIMAL.replace('start = "10:00"', 'start = "10:00"\nskip_dates = ["2026-10-03"]'))
        self.assertIn(date(2026, 10, 3), spec.skip_dates)

    def test_streak_and_follow_up_pass_over_skipped_days(self) -> None:
        spec = dataclasses.replace(WORKLOG, skip_dates=frozenset({date(2026, 10, 1)}))
        with tempfile.TemporaryDirectory() as tmp:
            store = ck.CheckinStore(Path(tmp), follow_source="first", noun="day")
            for day, status in ((date(2026, 9, 29), "done"), (date(2026, 9, 30), "done"), (date(2026, 10, 2), "done")):
                store.save("1", {"date": day.isoformat(), "status": status, "answers": {"first": "x"}})
            self.assertEqual(ck.current_streak(spec, store, "1", date(2026, 10, 2)), 3)  # Thu is a holiday
            store.save("1", ck.skipped_entry(date(2026, 10, 2), "t"))
            found = store.previous_to_check("1", date(2026, 10, 5), 3, spec.skip_dates)
            self.assertEqual(found["date"], "2026-09-30")  # Fri skipped, Thu holiday, weekend empty

    def test_day_status(self) -> None:
        holiday = frozenset({date(2026, 10, 1)})
        self.assertEqual(ck.day_status(None, date(2026, 10, 1), {3}, date(2026, 10, 5), holiday), "skipped")
        self.assertEqual(ck.day_status({"status": "missed"}, date(2026, 10, 1), {3}, date(2026, 10, 5), holiday),
                         "skipped")
        self.assertEqual(ck.day_status(None, date(2026, 10, 1), {3}, date(2026, 10, 5)), "missed")


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



class PresetLibraryTest(unittest.TestCase):
    """Every shipped template parses and keeps the card style: Chinese titles,
    English prompts."""

    def test_all_presets_parse_with_unique_ids_and_commands(self) -> None:
        specs, problems = ck.load_specs(PRESETS)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(s.id for s in specs),
                         ["health", "mood", "night", "reading", "weekgoals", "worklog"])
        for spec in specs:
            self.assertNotIn(spec.command, gateway.RESERVED_COMMANDS - {"night"}, spec.id)
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in spec.title), spec.id)
            for q in spec.questions:
                self.assertFalse(any("\u4e00" <= ch <= "\u9fff" for ch in q.prompt), f"{spec.id}.{q.id}")
            for section in spec.sections:
                self.assertTrue(set(section.sources) <= {q.id for q in spec.questions})

    def test_weekly_template_reports_monthly_only(self) -> None:
        spec = ck.parse_spec((PRESETS / "weekgoals.toml").read_text(encoding="utf-8"))
        self.assertEqual(sorted(spec.days), [0])  # Monday
        self.assertEqual(list(spec.reports), ["month"])
        self.assertEqual(spec.unit, "week")


class FollowUpLookbackTest(unittest.TestCase):
    def spec(self, days):
        return ck.parse_spec(MINIMAL.replace('days = ["sat"]', f"days = {days}"))

    def test_daily_and_weekday_check_ins_keep_three_days(self) -> None:
        monday = date(2026, 10, 5)
        self.assertEqual(ck.follow_up_lookback(self.spec('["mon","tue","wed","thu","fri","sat","sun"]'), monday), 3)
        self.assertEqual(ck.follow_up_lookback(self.spec('["mon","tue","wed","thu","fri"]'), monday), 3)

    def test_weekly_check_in_reaches_last_week(self) -> None:
        self.assertEqual(ck.follow_up_lookback(self.spec('["mon"]'), date(2026, 10, 5)), 7)

    def test_store_finds_last_weeks_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ck.CheckinStore(Path(tmp), follow_source="book", noun="week")
            entry = ck.new_entry(date(2026, 9, 28), "x")
            entry["answers"] = {"book": "Dune"}
            store.save("1", entry)
            self.assertIsNone(store.previous_to_check("1", date(2026, 10, 5)))
            self.assertEqual(store.previous_to_check("1", date(2026, 10, 5), 7)["date"], "2026-09-28")


class CheckinsCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "checkins"
        self.folder.mkdir()
        (self.folder / "reading.toml").write_text((PRESETS / "reading.toml").read_text(), encoding="utf-8")
        settings = dataclasses.replace(gateway.settings, checkin_dir=self.folder, night_ritual_enabled=False,
                                       checkin_data_dir=Path(self.tmp.name) / "data")
        self.sent = []
        for name, value in [("settings", settings), ("send_message", lambda chat_id, text: self.sent.append(text)),
                            ("telegram", lambda *a, **k: {"ok": True})]:
            p = patch.object(gateway, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict("os.environ", {"OPENCLAW_CHECKIN_OWNER": "1"})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._stop_all)
        gateway.CHECKIN_RUNTIMES[:] = gateway.load_checkin_runtimes()

    def _stop_all(self) -> None:
        for runtime in gateway.CHECKIN_RUNTIMES:
            runtime.stopped.set()
        gateway.CHECKIN_RUNTIMES.clear()
        gateway.CHECKIN_PROBLEMS.clear()

    def test_list_shows_check_ins_and_skipped_files(self) -> None:
        (self.folder / "broken.toml").write_text("[checkin]\nid='x'", encoding="utf-8")
        gateway.load_checkin_runtimes()
        self.assertTrue(gateway.checkins_command(1, "/checkins"))
        self.assertIn("/reading -- 閱讀筆記", self.sent[-1])
        self.assertIn("broken.toml", self.sent[-1])
        self.assertFalse(gateway.checkins_command(1, "/checkinsx"))

    def test_reload_adds_and_removes_without_restart(self) -> None:
        (self.folder / "mood.toml").write_text((PRESETS / "mood.toml").read_text(), encoding="utf-8")
        (self.folder / "reading.toml").unlink()
        old = list(gateway.CHECKIN_RUNTIMES)
        with patch.object(gateway, "start_checkin_loop", lambda runtime: None):
            gateway.checkins_command(1, "/checkins reload")
        self.assertIn("added mood; removed reading", self.sent[-1])
        self.assertEqual([r.spec.id for r in gateway.CHECKIN_RUNTIMES], ["mood"])
        self.assertTrue(all(r.stopped.is_set() for r in old))

    def test_add_template_from_telegram(self) -> None:
        with patch.object(gateway, "start_checkin_loop", lambda runtime: None):
            gateway.checkins_command(1, "/checkins add mood")
        self.assertTrue((self.folder / "mood.toml").exists())
        self.assertIn("Added 心情紀錄 (/mood)", self.sent[-1])
        self.assertIn("mood", [r.spec.id for r in gateway.CHECKIN_RUNTIMES])
        gateway.checkins_command(1, "/checkins add mood")
        self.assertIn("already in this bot's check-ins", self.sent[-1])
        gateway.checkins_command(1, "/checkins add nope")
        self.assertIn("No template called 'nope'", self.sent[-1])

    def test_add_without_owner_explains_why_it_is_not_running(self) -> None:
        with patch.dict("os.environ", {"OPENCLAW_CHECKIN_OWNER": ""}), \
                patch.object(gateway, "start_checkin_loop", lambda runtime: None):
            gateway.checkins_command(1, "/checkins add health")
        self.assertIn("isn't running: health: OPENCLAW_CHECKIN_OWNER is not set", self.sent[-1])

    def test_remove_keeps_the_file_aside_and_overview_offers_templates(self) -> None:
        gateway.checkins_command(1, "/checkins")
        self.assertIn("Templates you can add: ", self.sent[-1])
        self.assertNotIn("reading,", self.sent[-1].split("Templates you can add: ")[1].split("\n")[0] + ",")
        with patch.object(gateway, "start_checkin_loop", lambda runtime: None):
            gateway.checkins_command(1, "/checkins remove reading")
        self.assertIn("Stopped reading", self.sent[-1])
        self.assertFalse((self.folder / "reading.toml").exists())
        self.assertEqual(len(list((self.folder / "removed").glob("reading-*.toml"))), 1)
        self.assertEqual(gateway.CHECKIN_RUNTIMES, [])
        gateway.checkins_command(1, "/checkins remove reading")
        self.assertIn("No check-in 'reading'", self.sent[-1])

    def test_reserved_command_is_refused(self) -> None:
        (self.folder / "mem.toml").write_text(MINIMAL.replace('id = "reading"', 'id = "memo"\ncommand = "mem"'),
                                              encoding="utf-8")
        ids = [r.spec.id for r in gateway.load_checkin_runtimes()]
        self.assertNotIn("memo", ids)
        self.assertTrue(any("/mem is already a bot command" in p for p in gateway.CHECKIN_PROBLEMS))


if __name__ == "__main__":
    unittest.main()
