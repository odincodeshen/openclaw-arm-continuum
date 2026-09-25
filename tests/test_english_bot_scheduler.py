import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from openclaw_runtime.english_bot_scheduler import (
    day_code_for,
    dispatch_pending_reply,
    load_json,
    mark_pushed_today,
    mark_swept_today,
    run_todays_push,
    run_todays_sweep,
    should_push_today,
    should_sweep_today,
    write_json,
)


class DayCodeForTest(unittest.TestCase):
    def test_all_seven_weekdays(self) -> None:
        # 2026-09-21 is a Monday
        expected = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        for offset, code in enumerate(expected):
            self.assertEqual(day_code_for(datetime(2026, 9, 21 + offset, 7, 15)), code)


class LoadWriteJsonTest(unittest.TestCase):
    def test_missing_file_returns_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertEqual(load_json(path, {"x": 1}), {"x": 1})

    def test_write_then_load_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            write_json(path, {"last_push_date": "2026-09-21"})
            self.assertEqual(load_json(path, {}), {"last_push_date": "2026-09-21"})
            self.assertFalse(path.with_suffix(".json.tmp").exists())  # atomic write cleans up the tmp file


class ShouldPushTodayTest(unittest.TestCase):
    def test_before_due_time_does_not_push(self) -> None:
        self.assertFalse(should_push_today(datetime(2026, 9, 21, 7, 0), "07:15", {}))

    def test_at_due_time_pushes(self) -> None:
        self.assertTrue(should_push_today(datetime(2026, 9, 21, 7, 15), "07:15", {}))

    def test_within_window_pushes(self) -> None:
        self.assertTrue(should_push_today(datetime(2026, 9, 21, 7, 30), "07:15", {}))

    def test_past_window_does_not_push(self) -> None:
        self.assertFalse(should_push_today(datetime(2026, 9, 21, 20, 0), "07:15", {}))

    def test_does_not_push_twice_same_day(self) -> None:
        state = {"last_push_date": "2026-09-21"}
        self.assertFalse(should_push_today(datetime(2026, 9, 21, 7, 20), "07:15", state))

    def test_pushes_again_next_day(self) -> None:
        state = {"last_push_date": "2026-09-21"}
        self.assertTrue(should_push_today(datetime(2026, 9, 22, 7, 20), "07:15", state))

    def test_mark_pushed_sets_todays_date(self) -> None:
        state: dict = {}
        mark_pushed_today(datetime(2026, 9, 21, 7, 15), state)
        self.assertEqual(state["last_push_date"], "2026-09-21")


class ShouldSweepTodayTest(unittest.TestCase):
    def test_does_not_sweep_twice_same_day(self) -> None:
        state = {"last_sweep_date": "2026-09-21"}
        self.assertFalse(should_sweep_today(datetime(2026, 9, 21, 21, 5), "21:00", state))

    def test_sweeps_next_day(self) -> None:
        state = {"last_sweep_date": "2026-09-21"}
        self.assertTrue(should_sweep_today(datetime(2026, 9, 22, 21, 5), "21:00", state))

    def test_mark_swept_sets_todays_date(self) -> None:
        state: dict = {}
        mark_swept_today(datetime(2026, 9, 21, 21, 0), state)
        self.assertEqual(state["last_sweep_date"], "2026-09-21")

    def test_push_and_sweep_state_are_independent(self) -> None:
        state: dict = {}
        mark_pushed_today(datetime(2026, 9, 21, 7, 15), state)
        self.assertTrue(should_sweep_today(datetime(2026, 9, 21, 21, 5), "21:00", state))


def _push_kwargs(day_code: str, **overrides) -> dict:
    base = dict(
        day_code=day_code,
        llm=MagicMock(),
        qdrant=MagicMock(),
        embeddings=MagicMock(),
        collection="coll",
        owners=["owner-a"],
        send_message=MagicMock(),
        send_audio=MagicMock(),
        set_pending_answer=MagicMock(),
        clip_client=MagicMock(),
        transcription_client=MagicMock(),
        workspace_root=Path("/workspace"),
    )
    base.update(overrides)
    return base


class RunTodaysPushDispatchTest(unittest.TestCase):
    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=5)
    @patch("openclaw_runtime.english_bot_scheduler.run_monday_task")
    def test_monday_dispatches_and_sets_pending(self, run_monday_task, next_week_number) -> None:
        content = MagicMock(week_number=5)
        run_monday_task.return_value = content
        set_pending_answer = MagicMock()
        run_todays_push(**_push_kwargs("mon", set_pending_answer=set_pending_answer))
        run_monday_task.assert_called_once()
        set_pending_answer.assert_called_once_with("owner-a", {"kind": "eng_mon", "week_number": 5})

    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=5)
    @patch("openclaw_runtime.english_bot_scheduler.run_monday_task", return_value=None)
    def test_monday_already_processed_sets_no_pending(self, run_monday_task, next_week_number) -> None:
        set_pending_answer = MagicMock()
        run_todays_push(**_push_kwargs("mon", set_pending_answer=set_pending_answer))
        set_pending_answer.assert_not_called()

    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=1)
    def test_tue_wed_thu_fri_sat_sun_no_op_before_monday_has_run(self, next_week_number) -> None:
        for day in ["tue", "wed", "thu", "fri", "sat", "sun"]:
            set_pending_answer = MagicMock()
            run_todays_push(**_push_kwargs(day, set_pending_answer=set_pending_answer))
            set_pending_answer.assert_not_called()

    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=6)
    @patch("openclaw_runtime.english_bot_scheduler.run_tuesday_task")
    def test_tuesday_dispatches_with_reference_text(self, run_tuesday_task, next_week_number) -> None:
        run_tuesday_task.return_value = MagicMock(stretch_text="the raw stretch")
        set_pending_answer = MagicMock()
        run_todays_push(**_push_kwargs("tue", set_pending_answer=set_pending_answer))
        run_tuesday_task.assert_called_once()
        self.assertEqual(run_tuesday_task.call_args.kwargs["week_number"], 5)
        set_pending_answer.assert_called_once_with(
            "owner-a", {"kind": "eng_tue", "week_number": 5, "reference_text": "the raw stretch"}
        )

    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=6)
    @patch("openclaw_runtime.english_bot_scheduler.run_wednesday_task")
    def test_wednesday_combo_week_includes_part3(self, run_wednesday_task, next_week_number) -> None:
        run_wednesday_task.return_value = {"cue_card": "Describe...", "part3_question": "Does AI...?"}
        set_pending_answer = MagicMock()
        run_todays_push(**_push_kwargs("wed", set_pending_answer=set_pending_answer))
        set_pending_answer.assert_called_once_with(
            "owner-a",
            {"kind": "eng_wed", "week_number": 5, "cue_card": "Describe...", "part3_question": "Does AI...?"},
        )

    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=6)
    @patch("openclaw_runtime.english_bot_scheduler.run_wednesday_task")
    def test_wednesday_non_combo_week_omits_part3(self, run_wednesday_task, next_week_number) -> None:
        run_wednesday_task.return_value = {"cue_card": "Describe..."}
        set_pending_answer = MagicMock()
        run_todays_push(**_push_kwargs("wed", set_pending_answer=set_pending_answer))
        set_pending_answer.assert_called_once_with(
            "owner-a", {"kind": "eng_wed", "week_number": 5, "cue_card": "Describe..."}
        )

    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=6)
    @patch("openclaw_runtime.english_bot_scheduler.run_thursday_task")
    def test_thursday_dispatches_with_opener(self, run_thursday_task, next_week_number) -> None:
        run_thursday_task.return_value = "So, typical British weather..."
        set_pending_answer = MagicMock()
        run_todays_push(**_push_kwargs("thu", set_pending_answer=set_pending_answer))
        set_pending_answer.assert_called_once_with(
            "owner-a", {"kind": "eng_thu", "week_number": 5, "opener": "So, typical British weather..."}
        )

    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=6)
    @patch("openclaw_runtime.english_bot_scheduler.run_friday_task")
    def test_friday_dispatches(self, run_friday_task, next_week_number) -> None:
        set_pending_answer = MagicMock()
        run_todays_push(**_push_kwargs("fri", set_pending_answer=set_pending_answer))
        run_friday_task.assert_called_once()
        set_pending_answer.assert_called_once_with("owner-a", {"kind": "eng_fri", "week_number": 5})

    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=6)
    @patch("openclaw_runtime.english_bot_scheduler.run_saturday_task")
    def test_saturday_delegates_its_own_pending_answer(self, run_saturday_task, next_week_number) -> None:
        set_pending_answer = MagicMock()
        run_todays_push(**_push_kwargs("sat", set_pending_answer=set_pending_answer))
        run_saturday_task.assert_called_once()
        self.assertIs(run_saturday_task.call_args.kwargs["set_pending_answer"], set_pending_answer)
        set_pending_answer.assert_not_called()  # run_saturday_task itself would call it -- it's mocked here

    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=6)
    @patch("openclaw_runtime.english_bot_scheduler.run_sunday_task")
    def test_sunday_dispatches_with_no_pending_answer(self, run_sunday_task, next_week_number) -> None:
        set_pending_answer = MagicMock()
        run_todays_push(**_push_kwargs("sun", set_pending_answer=set_pending_answer))
        run_sunday_task.assert_called_once()
        set_pending_answer.assert_not_called()


class RunTodaysSweepTest(unittest.TestCase):
    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=1)
    def test_no_op_before_any_week_exists(self, next_week_number) -> None:
        self.assertEqual(run_todays_sweep(MagicMock(), "coll", ["owner-a"]), {})

    @patch("openclaw_runtime.english_bot_scheduler.next_week_number", return_value=3)
    @patch("openclaw_runtime.english_bot_scheduler.run_daily_completion_sweep")
    def test_sweeps_the_current_week(self, run_daily_completion_sweep, next_week_number) -> None:
        run_daily_completion_sweep.return_value = {"sat": ["owner-a"]}
        result = run_todays_sweep(MagicMock(), "coll", ["owner-a"])
        run_daily_completion_sweep.assert_called_once_with(unittest.mock.ANY, "coll", 2, ["owner-a"])
        self.assertEqual(result, {"sat": ["owner-a"]})


class DispatchPendingReplyTest(unittest.TestCase):
    @patch("openclaw_runtime.english_bot_scheduler.read_this_week_chunks", return_value=["chunk"])
    @patch("openclaw_runtime.english_bot_scheduler.evaluate_monday_reply", return_value="mon feedback")
    def test_eng_mon_dispatches(self, evaluate_monday_reply, read_this_week_chunks) -> None:
        result = dispatch_pending_reply(
            pending={"kind": "eng_mon", "week_number": 5},
            transcribed_reply="I took a gamble on it",
            reply_duration_seconds=0.0,
            llm=MagicMock(),
            qdrant=MagicMock(),
            embeddings=MagicMock(),
            collection="coll",
            owner="owner-a",
        )
        self.assertEqual(result, "mon feedback")
        evaluate_monday_reply.assert_called_once()

    @patch("openclaw_runtime.english_bot_scheduler.evaluate_tuesday_reply", return_value="tue feedback")
    def test_eng_tue_dispatches_with_reference_text(self, evaluate_tuesday_reply) -> None:
        llm = MagicMock()
        result = dispatch_pending_reply(
            pending={"kind": "eng_tue", "week_number": 5, "reference_text": "raw stretch"},
            transcribed_reply="echoed stretch",
            reply_duration_seconds=12.5,
            llm=llm,
            qdrant=MagicMock(),
            embeddings=MagicMock(),
            collection="coll",
            owner="owner-a",
        )
        self.assertEqual(result, "tue feedback")
        args, kwargs = evaluate_tuesday_reply.call_args
        self.assertEqual(args[0], llm)
        self.assertEqual(args[1], "raw stretch")
        self.assertEqual(args[2], "echoed stretch")
        self.assertEqual(args[3], 12.5)

    @patch("openclaw_runtime.english_bot_scheduler.evaluate_wednesday_reply", return_value="wed feedback")
    def test_eng_wed_dispatches_with_cue_card_and_part3(self, evaluate_wednesday_reply) -> None:
        dispatch_pending_reply(
            pending={"kind": "eng_wed", "week_number": 18, "cue_card": "Describe...", "part3_question": "Does AI?"},
            transcribed_reply="my answer",
            reply_duration_seconds=0.0,
            llm=MagicMock(),
            qdrant=MagicMock(),
            embeddings=MagicMock(),
            collection="coll",
            owner="owner-a",
        )
        self.assertEqual(evaluate_wednesday_reply.call_args.kwargs["part3_question"], "Does AI?")

    @patch("openclaw_runtime.english_bot_scheduler.evaluate_thursday_reply", return_value="thu feedback")
    def test_eng_thu_dispatches_with_opener(self, evaluate_thursday_reply) -> None:
        dispatch_pending_reply(
            pending={"kind": "eng_thu", "week_number": 5, "opener": "typical weather..."},
            transcribed_reply="my answer",
            reply_duration_seconds=0.0,
            llm=MagicMock(),
            qdrant=MagicMock(),
            embeddings=MagicMock(),
            collection="coll",
            owner="owner-a",
        )
        args, kwargs = evaluate_thursday_reply.call_args
        self.assertEqual(args[1], "typical weather...")

    @patch("openclaw_runtime.english_bot_scheduler.read_this_week_chunks", return_value=["chunk"])
    @patch("openclaw_runtime.english_bot_scheduler.evaluate_friday_reply", return_value="fri feedback")
    def test_eng_fri_dispatches(self, evaluate_friday_reply, read_this_week_chunks) -> None:
        result = dispatch_pending_reply(
            pending={"kind": "eng_fri", "week_number": 5},
            transcribed_reply="my ramble",
            reply_duration_seconds=0.0,
            llm=MagicMock(),
            qdrant=MagicMock(),
            embeddings=MagicMock(),
            collection="coll",
            owner="owner-a",
        )
        self.assertEqual(result, "fri feedback")

    @patch("openclaw_runtime.english_bot_scheduler.evaluate_saturday_answers", return_value="sat feedback")
    def test_eng_saturday_quiz_dispatches_with_phrases(self, evaluate_saturday_answers) -> None:
        dispatch_pending_reply(
            pending={"kind": "eng_saturday_quiz", "week_number": 5, "phrases": ["take a gamble on"]},
            transcribed_reply="take a gamble on",
            reply_duration_seconds=0.0,
            llm=MagicMock(),
            qdrant=MagicMock(),
            embeddings=MagicMock(),
            collection="coll",
            owner="owner-a",
        )
        args, kwargs = evaluate_saturday_answers.call_args
        self.assertEqual(args[5], ["take a gamble on"])

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            dispatch_pending_reply(
                pending={"kind": "eng_bogus", "week_number": 1},
                transcribed_reply="x",
                reply_duration_seconds=0.0,
                llm=MagicMock(),
                qdrant=MagicMock(),
                embeddings=MagicMock(),
                collection="coll",
                owner="owner-a",
            )


if __name__ == "__main__":
    unittest.main()
