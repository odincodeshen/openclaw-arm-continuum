import dataclasses
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import openclaw_telegram_gateway as gateway
from openclaw_runtime import night_ritual as nr
from tests.card_checks import assert_valid_telegram_html

LONDON = ZoneInfo("Europe/London")
OWNER = 42
TUE = date(2026, 9, 29)


class FakeLlm:
    def __init__(self, reply="Nice work today. Sleep well.", report=None) -> None:
        self.reply = reply
        self.report = report or {"highlights": ["Shipped buttons"], "growing": "Clearer specs.",
                                 "adjusting": "Late finishes (2 times).", "closing": "A steady week."}
        self.prompts = []

    def chat(self, prompt, max_tokens=None, history=None):
        self.prompts.append(prompt)
        return self.reply

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.prompts.append(prompt)
        return json.dumps(self.report)


class ModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = nr.NightStore(Path(self.tmp.name))

    def test_ritual_days_and_steps(self) -> None:
        self.assertEqual(nr.parse_ritual_days("sun,mon,tue,wed,thu,fri"), {6, 0, 1, 2, 3, 4})
        self.assertEqual(nr.next_step({"answers": {"wins": "a", "better": "b"}}), "adjust")
        self.assertIsNone(nr.next_step({"answers": dict.fromkeys(nr.STEPS, "x")}))

    def test_split_wins(self) -> None:
        self.assertEqual(nr.split_wins("fixed X\nwalked; 散步"), ["fixed X", "walked", "散步"])
        self.assertEqual(nr.split_wins("1. a 2. b 3) c"), ["a", "b", "c"])
        self.assertEqual(nr.split_wins("just one thing, really"), ["just one thing, really"])

    def test_cards_are_chinese_title_english_body(self) -> None:
        html = nr.render_question("wins")
        assert_valid_telegram_html(self, html)
        self.assertTrue(html.startswith("<b>【晚安儀式 1/4】</b>· Wins &amp; Gratitude\n━"))
        self.assertIn("One message, 1–3 things · text or voice", html)
        self.assertIn("【晚安儀式 4/4】</b>· Mental Shutdown", nr.render_question("first"))
        self.assertIn("pick up at 2/4", nr.render_reminder("better", last=False))
        self.assertIn("【最後提醒】", nr.render_reminder("check", last=True))

    def test_closing_card_escapes_and_bullets(self) -> None:
        entry = {"date": "2026-09-29", "answers": {"wins": "a <b>\nc", "better": "b", "adjust": "d", "first": "e"}}
        html = nr.render_closing(entry, "Rest well.")
        assert_valid_telegram_html(self, html)
        self.assertIn("【今日結案】</b>· Tue 29 Sep", html)
        self.assertIn("• a &lt;b&gt;\n• c", html)
        self.assertTrue(html.endswith("Rest well."))

    def test_previous_to_check_only_looks_at_the_latest_earlier_night(self) -> None:
        self.store.save("o", {"date": "2026-09-25", "answers": {"first": "old"}})  # Friday
        self.assertEqual(self.store.previous_to_check("o", date(2026, 9, 27))["date"], "2026-09-25")  # Sunday
        self.store.save("o", {"date": "2026-09-28", "answers": {"first": "x"}, "first_done": True})
        self.assertIsNone(self.store.previous_to_check("o", TUE))

    def test_closing_line_falls_back(self) -> None:
        class Broken:
            def chat(self, *a, **k):
                raise RuntimeError("down")

        self.assertEqual(nr.closing_line(Broken(), {"answers": {}}), nr.FALLBACK_CLOSING)
        llm = FakeLlm(reply='  "Well done."  ')
        self.assertEqual(nr.closing_line(llm, {"answers": {"wins": "x"}}), "Well done.")
        self.assertIn("English only", llm.prompts[0])

    def test_condense_voice_answer(self) -> None:
        short = "Fixed the bug."
        self.assertEqual(nr.condense_voice_answer(FakeLlm(), "wins", short), short)
        long = "um so today I I fixed that really annoying bug in the watchdog and then " * 3
        llm = FakeLlm(reply="Fixed the watchdog bug")
        self.assertEqual(nr.condense_voice_answer(llm, "wins", long), "Fixed the watchdog bug")
        self.assertIn("keeping the speaker's own language", llm.prompts[0])

        class Broken:
            def chat(self, *a, **k):
                raise RuntimeError("down")

        self.assertEqual(nr.condense_voice_answer(Broken(), "wins", long), " ".join(long.split()))

    def test_report_periods(self) -> None:
        self.assertEqual(nr.report_period("week", date(2026, 10, 5)), (date(2026, 9, 28), date(2026, 10, 4)))
        self.assertEqual(nr.report_period("month", date(2026, 10, 1)), (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(nr.reports_due(date(2026, 10, 5)), ["week"])
        self.assertEqual(nr.reports_due(date(2026, 6, 1)), ["week", "month"])
        self.assertEqual(nr.reports_due(TUE), [])

    def test_weekly_report_counts_and_markdown(self) -> None:
        days = nr.parse_ritual_days("sun,mon,tue,wed,thu,fri")
        self.store.save("o", {"date": "2026-09-28", "status": "done", "first_done": True,
                              "answers": {"wins": "w", "better": "b", "adjust": "a", "first": "f"}})
        self.store.save("o", {"date": "2026-09-29", "status": "partial", "answers": {"wins": "w2"}})
        self.store.save("o", {"date": "2026-09-30", "status": "done", "first_done": False,
                              "answers": {"wins": "w", "better": "b", "adjust": "a", "first": "g"}})
        html, markdown = nr.build_report(FakeLlm(), self.store, "o", "week", date(2026, 10, 5), days)
        assert_valid_telegram_html(self, html)
        self.assertTrue(html.startswith("<b>【晚安週報】</b>· 28 Sep – 4 Oct"))
        self.assertIn("Mon ✅ Tue ◐ Wed ✅ Thu ❌ Fri ❌ Sun ❌", html)  # Saturday isn't a ritual night
        self.assertIn("2 of 6 nights · 1 partly", html)
        self.assertIn("2 written · 1 done · 1 not yet", html)
        self.assertIn("• Shipped buttons", html)
        saved = Path(self.tmp.name) / "o" / "reports" / "week-2026-09-28.md"
        self.assertEqual(saved.read_text(encoding="utf-8"), markdown)
        self.assertIn("### 2026-09-29 (partial)", markdown)

    def test_trend_streak_and_comparison_with_the_week_before(self) -> None:
        days = nr.parse_ritual_days("sun,mon,tue,wed,thu,fri")
        full = {"wins": "w", "better": "b", "adjust": "a", "first": "f"}
        # week before (21-27 Sep): 3 of 6 done, one first thing checked and not done
        for d in ("2026-09-22", "2026-09-23", "2026-09-24"):
            self.store.save("o", {"date": d, "status": "done", "answers": full, "first_done": False})
        # this week (28 Sep - 4 Oct): Fri 2, Sun 4 done, Sat not a ritual night; Thu missed
        for d in ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-02", "2026-10-04"):
            self.store.save("o", {"date": d, "status": "done", "answers": full, "first_done": True})
        html, markdown = nr.build_report(FakeLlm(), self.store, "o", "week", date(2026, 10, 5), days)
        self.assertIn("<b>Trend</b>\nStreak: 2 nights in a row", html)
        self.assertIn("Nights done: 83% (last week 50%) ↑", html)
        self.assertIn("First things done: 100% (last week 0%) ↑", html)
        self.assertIn("- Streak: 2 nights in a row", markdown)

    def test_year_report_with_monthly_rates(self) -> None:
        days = nr.parse_ritual_days("sun,mon,tue,wed,thu,fri,sat")
        full = {"wins": "w", "better": "b", "adjust": "a", "first": "f"}
        for d in ("2026-01-01", "2026-01-02", "2026-12-31"):
            self.store.save("o", {"date": d, "status": "done", "answers": full})
        self.assertEqual(nr.reports_due(date(2027, 1, 1)), ["month", "year"])
        self.assertEqual(nr.report_period("year", date(2027, 1, 1)), (date(2026, 1, 1), date(2026, 12, 31)))
        html, markdown = nr.build_report(FakeLlm(), self.store, "o", "year", date(2027, 1, 1), days)
        assert_valid_telegram_html(self, html)
        self.assertTrue(html.startswith("<b>【晚安年報】</b>· 2026"))
        self.assertIn("Jan 6% · Feb 0%", html)  # 2 of 31 January nights
        self.assertIn("Dec 3%", html)
        self.assertIn("3 of 365 nights", html)
        saved = Path(self.tmp.name) / "o" / "reports" / "year-2026.md"
        self.assertTrue(saved.exists())
        self.assertIn("# Night ritual — 2026", markdown)

    def test_empty_month_skips_the_model(self) -> None:
        llm = FakeLlm()
        html, _ = nr.build_report(llm, self.store, "o", "month", date(2026, 10, 1), {0})
        self.assertIn("【晚安月報】</b>· September 2026", html)
        self.assertEqual(llm.prompts, [])


class GatewayNightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = patch.object(gateway, "settings", dataclasses.replace(
            gateway.settings, night_ritual_enabled=True, night_ritual_owner=OWNER,
            night_ritual_dir=root / "night", inbox_path=root / "inbox", pending_state_path=None,
            telegram_allowed_chat_ids=set(),
        ))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sent: list[str] = []
        self.api: list[tuple[str, dict]] = []

        def fake_telegram(method, payload=None, timeout=60):
            self.api.append((method, payload or {}))
            if method == "sendMessage":
                self.sent.append(payload["text"])
            return {"ok": True, "result": {"message_id": 500 + len(self.api)}}

        self.llm = FakeLlm()

        class InlineThread:
            def __init__(inner, target=None, args=(), daemon=None):
                inner.target, inner.args = target, args

            def start(inner):
                inner.target(*inner.args)

        for name, value in [("telegram", fake_telegram), ("llm", self.llm),
                            ("send_message", lambda chat_id, text: self.sent.append(text)),
                            ("send_html", lambda chat_id, html: self.sent.append(html))]:
            p = patch.object(gateway, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(gateway.threading, "Thread", InlineThread)
        p.start()
        self.addCleanup(p.stop)
        self.clock = datetime(2026, 9, 29, 22, 30, tzinfo=LONDON)
        p = patch.object(gateway, "_night_now", lambda: self.clock)
        p.start()
        self.addCleanup(p.stop)
        gateway.NIGHT_PENDING.clear()
        self.addCleanup(gateway.NIGHT_PENDING.clear)
        self.store = gateway.night_store()

    def _say(self, text: str) -> None:
        self.assertTrue(gateway.handle_night_reply(OWNER, {"chat": {"id": OWNER}, "text": text}))

    def _tick(self, hour: int, minute: int, state: dict, day: int = 29) -> None:
        self.clock = datetime(2026, 9, day, hour, minute, tzinfo=LONDON)
        gateway.night_tick(self.clock, state)

    def test_full_night_with_the_first_thing_check(self) -> None:
        self.store.save(str(OWNER), {"date": "2026-09-28", "status": "done", "answers": {"first": "Write spec"}})
        state: dict = {}
        self._tick(22, 30, state)
        check = [p for m, p in self.api if m == "sendMessage"][-1]
        self.assertIn("Write spec", check["text"])
        self.assertEqual(gateway.NIGHT_PENDING[OWNER]["step"], "check")
        self._say("typed too early")
        self.assertIn("Tap ✅ or ❌", self.sent[-1])

        data = check["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        gateway.handle_callback_query({"id": "q", "data": data, "message": {"message_id": 7, "chat": {"id": OWNER}}})
        self.assertTrue(self.store.load(str(OWNER), date(2026, 9, 28))["first_done"])
        self.assertIn("【晚安儀式 1/4】", self.sent[-1])

        for answer in ["fixed the bug\nwalked", "asked before building", "deploy earlier", "finish the spec"]:
            self._say(answer)
        entry = self.store.load(str(OWNER), TUE)
        self.assertEqual(entry["status"], "done")
        self.assertEqual(entry["answers"]["first"], "finish the spec")
        self.assertEqual(entry["closing"], "Nice work today. Sleep well.")
        self.assertIn("【今日結案】", self.sent[-1])
        self.assertNotIn(OWNER, gateway.NIGHT_PENDING)

        count = len(self.sent)
        self._tick(23, 0, state)
        self._tick(23, 30, state)
        self.assertEqual(len(self.sent), count)  # done: no reminders

        self._tick(7, 5, state, day=30)
        self.assertIn("finish the spec", self.sent[-1])
        self.assertIn("【今天的第一件事】", self.sent[-1])

    def test_a_long_voice_answer_is_tidied_and_the_transcript_kept(self) -> None:
        spoken = "well " * 30 + "I finished the spec"
        gateway.night_command(OWNER, "/night start")
        self.llm.reply = "Finished the spec"
        with patch.object(gateway, "telegram_file_info", lambda f: {"file_path": "v.oga"}), \
                patch.object(gateway, "download_telegram_file", lambda f, target: (target, 1)), \
                patch.object(gateway.transcriber, "transcribe", lambda path: spoken):
            self.assertTrue(gateway.handle_night_reply(OWNER, {"voice": {"file_id": "f"}}))
        entry = self.store.load(str(OWNER), TUE)
        self.assertEqual(entry["answers"]["wins"], "Finished the spec")
        self.assertEqual(entry["spoken"]["wins"], " ".join(spoken.split()))

    def test_reminders_then_partial_at_midnight(self) -> None:
        state: dict = {}
        self._tick(22, 30, state)
        self._say("one win")
        self._tick(23, 0, state)
        self.assertIn("pick up at 2/4", self.sent[-1])
        self._tick(23, 10, state)
        self._tick(23, 30, state)
        self.assertIn("【最後提醒】", self.sent[-1])
        reminders = [t for t in self.sent if "晚安提醒" in t or "最後提醒" in t]
        self.assertEqual(len(reminders), 2)
        self._tick(0, 1, state, day=30)
        self.assertEqual(self.store.load(str(OWNER), TUE)["status"], "partial")
        self.assertNotIn(OWNER, gateway.NIGHT_PENDING)

    def test_saturday_is_skipped_and_untouched_night_is_missed(self) -> None:
        state: dict = {}
        self._tick(22, 30, state, day=26)  # Saturday
        self.assertEqual(self.sent, [])
        self._tick(22, 30, state)
        self._tick(0, 1, state, day=30)
        self.assertEqual(self.store.load(str(OWNER), TUE)["status"], "missed")

    def test_manual_start_early_means_no_second_push(self) -> None:
        self.clock = datetime(2026, 9, 29, 21, 0, tzinfo=LONDON)
        self.assertTrue(gateway.night_command(OWNER, "/night start"))
        for answer in ["a", "b", "c", "d"]:
            self._say(answer)
        count = len(self.sent)
        self._tick(22, 30, {})
        self.assertEqual(len(self.sent), count)
        gateway.night_command(OWNER, "/night start")
        self.assertIn("already done", self.sent[-1])

    def test_commands_pass_through_and_other_chats_are_ignored(self) -> None:
        gateway.night_command(OWNER, "/night start")
        self.assertFalse(gateway.handle_night_reply(OWNER, {"text": "/rag something"}))
        self.assertFalse(gateway.handle_night_reply(7, {"text": "hello"}))
        gateway.night_command(7, "/night")
        self.assertIn("isn't set up", self.sent[-1])

    def test_history_and_single_day(self) -> None:
        self.store.save(str(OWNER), {"date": "2026-09-28", "status": "done",
                                     "answers": {"wins": "w1", "better": "b", "adjust": "a", "first": "f"}})
        gateway.night_command(OWNER, "/night")
        html = self.sent[-1]
        assert_valid_telegram_html(self, html)
        self.assertIn("Mon 28 ✅", html)
        self.assertIn("Sun 27 ❌", html)
        self.assertIn("Wins: w1", html)
        gateway.night_command(OWNER, "/night 2026-09-28")
        self.assertIn("First: f", self.sent[-1])

    def test_add_first_thing_to_tomorrows_schedule(self) -> None:
        runs = []

        class FakeSkill:
            def __init__(self, settings, config, embeddings, qdrant):
                self.collection = settings.tracker_collection

            def run(self, text):
                runs.append((self.collection, text))

        with patch.object(gateway, "MemoryWriteSkill", FakeSkill), patch.object(
            gateway, "settings", dataclasses.replace(gateway.settings, night_ritual_schedule_collection="bot1_memory")
        ):
            gateway.night_command(OWNER, "/night start")
            for answer in ["a", "b", "c", "Book the dentist"]:
                self._say(answer)
            closing = [p for m, p in self.api if m == "sendMessage"][-1]
            button = closing["reply_markup"]["inline_keyboard"][0][0]
            tap = {"id": "q", "data": button["callback_data"], "message": {"message_id": 8, "chat": {"id": OWNER}}}
            gateway.handle_callback_query(tap)
            gateway.handle_callback_query(tap)  # a second tap doesn't add it twice
        self.assertEqual(runs, [("bot1_memory", "/mem Book the dentist due:2026-09-30 tag:night")])
        self.assertTrue(self.store.load(str(OWNER), TUE)["scheduled"])
        self.assertIn("Added to tomorrow's schedule.", [p for m, p in self.api if m == "editMessageText"][-1]["text"])

    def test_move_a_night_to_yesterday(self) -> None:
        gateway.night_command(OWNER, "/night start")
        gateway.night_command(OWNER, "/night move 2026-09-29 2026-09-28")
        self.assertIn("still open", self.sent[-1])
        for answer in ["a", "b", "c", "d"]:
            self._say(answer)
        gateway.night_command(OWNER, "/night move 2026-09-29 2026-09-28")
        self.assertIn("Moved", self.sent[-1])
        self.assertIsNone(self.store.load(str(OWNER), TUE))
        moved = self.store.load(str(OWNER), date(2026, 9, 28))
        self.assertEqual((moved["date"], moved["moved_from"]), ("2026-09-28", "2026-09-29"))
        gateway.night_command(OWNER, "/night move 2026-09-28 2026-09-28")
        self.assertIn("already has a night", self.sent[-1])
        gateway.night_command(OWNER, "/night move yesterday")
        self.assertIn("Use /night move", self.sent[-1])
        self._tick(22, 30, {})  # tonight still starts, asking about the moved night's first thing
        self.assertIn("d", self.sent[-1])

    def test_weekly_report_is_prepared_off_peak_and_sent_at_report_time(self) -> None:
        self.store.save(str(OWNER), {"date": "2026-09-29", "status": "done",
                                     "answers": {"wins": "w", "better": "b", "adjust": "a", "first": "f"}})
        state: dict = {}
        self.clock = datetime(2026, 10, 5, 4, 0, tzinfo=LONDON)
        gateway.night_tick(self.clock, state)
        self.assertIn("week", state["reports"])
        self.assertEqual(self.sent, [])
        self.clock = datetime(2026, 10, 5, 7, 30, tzinfo=LONDON)
        gateway.night_tick(self.clock, state)
        self.assertTrue(self.sent[-1].startswith("<b>【晚安週報】</b>"))
        self.assertNotIn("reports", state)

    def test_pending_night_survives_a_restart(self) -> None:
        path = Path(self.tmp.name) / "pending.json"
        with patch.object(gateway, "settings", dataclasses.replace(gateway.settings, pending_state_path=path)):
            gateway._set_night_pending(OWNER, {"date": "2026-09-29", "step": "adjust"})
            gateway.NIGHT_PENDING.clear()
            gateway.restore_pending_state()
        self.assertEqual(gateway.NIGHT_PENDING[OWNER]["step"], "adjust")


if __name__ == "__main__":
    unittest.main()
