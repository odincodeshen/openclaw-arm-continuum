import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from openclaw_runtime.english_bot_scheduler import run_todays_push
from openclaw_runtime.english_weekly import (
    ChunkOutcome,
    build_weekly_recap,
    chunk_outcomes,
    practice_days,
    promote_unlearned_chunks,
    render_weekly_recap,
)
from openclaw_runtime.vocabulary import add_for_review
from tests.card_checks import assert_valid_telegram_html

NOW = datetime(2026, 10, 4, 7, 15, tzinfo=timezone.utc)  # a Sunday
CHUNKS = [
    {"phrase": "move on", "definition": "繼續前進", "context_sentence": "We had to move on."},
    {"phrase": "bust down the door", "definition": "強行闖入", "context_sentence": "They bust down the door."},
    {"phrase": "take a weight off", "definition": "如釋重負", "context_sentence": "It took a weight off."},
]


class FakeQdrant:
    """Answers the three kinds of scroll this module makes: weekly chunks,
    per-chunk progress, and daily-task records."""

    def __init__(self, progress: dict, tasks: dict) -> None:
        self.progress = progress  # phrase -> payload
        self.tasks = tasks  # day code -> list of payloads
        self.set_payload = MagicMock()
        self.upsert_text = MagicMock(return_value="new-id")

    def scroll_by_filters(self, collection, filters, limit=64, **kwargs):
        kind = filters.get("kind")
        if kind == "weekly_content":
            return [{"payload": chunk} for chunk in CHUNKS]
        if kind == "chunk_progress":
            payload = self.progress.get(filters["chunk"])
            return [{"id": f"p-{filters['chunk']}", "payload": payload}] if payload else []
        if kind == "daily_task":
            day = filters["tag"].rsplit("day", 1)[1]
            return [{"id": f"t-{day}", "payload": p} for p in self.tasks.get(day, [])]
        if kind == "vocab":
            return []
        return []


def _qdrant():
    return FakeQdrant(
        progress={
            "move on": {"needs_review": False, "user_sentence": "We still need to move on."},
            "bust down the door": {"needs_review": True},
            # "take a weight off": never practised
        },
        tasks={
            "mon": [{"completed": True}],
            "tue": [{"completed": False}, {"completed": True}],  # duplicate records: any completed counts
            "wed": [{"completed": False}],
        },
    )


class ChunkOutcomesTest(unittest.TestCase):
    def test_learned_needs_review_and_never_practised(self) -> None:
        outcomes = chunk_outcomes(_qdrant(), "coll", "owner-a", 5)
        self.assertEqual([o.learned for o in outcomes], [True, False, False])
        self.assertEqual(outcomes[0].sentence, "We still need to move on.")
        self.assertEqual(outcomes[1].sentence, "They bust down the door.")  # falls back to the example


class PracticeDaysTest(unittest.TestCase):
    def test_done_missed_and_not_pushed(self) -> None:
        days = practice_days(_qdrant(), "coll", "owner-a", 5)
        self.assertEqual(
            days, {"mon": "done", "tue": "done", "wed": "missed", "thu": "none", "fri": "none", "sat": "none"}
        )


class PromoteUnlearnedChunksTest(unittest.TestCase):
    def test_only_unlearned_chunks_go_to_review_with_their_sentence(self) -> None:
        with patch("openclaw_runtime.english_weekly.add_for_review") as add:
            promoted = promote_unlearned_chunks(
                MagicMock(), MagicMock(), "coll", "owner-a", chunk_outcomes(_qdrant(), "coll", "owner-a", 5), now=NOW
            )
        self.assertEqual(promoted, ["bust down the door", "take a weight off"])
        args, kwargs = add.call_args_list[0]
        self.assertEqual(args[3:7], ("owner-a", "bust down the door", "強行闖入", "They bust down the door."))
        self.assertEqual(kwargs["source"], "weekly_chunk")


class AddForReviewTest(unittest.TestCase):
    def test_new_word_is_due_tomorrow_and_not_counted_as_a_lookup(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        add_for_review(qdrant, embeddings, "coll", "owner-a", "move on", "繼續前進", "We moved on.",
                       source="weekly_chunk", now=NOW)
        payload = qdrant.upsert_text.call_args.args[3]
        self.assertEqual(payload["owner"], "owner-a")
        self.assertEqual(payload["next_review"], "2026-10-05")
        self.assertEqual(payload["review_box"], 0)
        self.assertEqual(payload["lookup_count"], 0)
        self.assertEqual(payload["source"], "weekly_chunk")

    def test_word_already_on_the_list_goes_back_to_box_zero(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"id": "v1", "payload": {"review_box": 3, "lookup_count": 2}}]
        add_for_review(qdrant, MagicMock(), "coll", "owner-a", "move on", "繼續前進", "", source="weekly_chunk", now=NOW)
        qdrant.upsert_text.assert_not_called()
        qdrant.set_payload.assert_called_once_with("coll", "v1", {"review_box": 0, "next_review": "2026-10-05"})


class RenderWeeklyRecapTest(unittest.TestCase):
    def _outcomes(self):
        return [
            ChunkOutcome("move on", "", "", True),
            ChunkOutcome("bust <down>", "", "", False),
            ChunkOutcome("take a weight off", "", "", False),
        ]

    def test_with_word_list(self) -> None:
        html = render_weekly_recap(
            5,
            {"mon": "done", "tue": "done", "wed": "missed", "thu": "none", "fri": "done", "sat": "done"},
            self._outcomes(),
            promoted=["bust <down>", "take a weight off"],
            words_this_week=3,
            words_total=12,
        )
        assert_valid_telegram_html(self, html)
        self.assertTrue(html.startswith("<b>週日｜本週回顧</b> · Week 5\n━"))
        self.assertIn("Mon ✅　Tue ✅　Wed ❌　Thu —　Fri ✅　Sat ✅\n4 of 6 days done", html)
        self.assertIn("✅ move on\n❌ bust &lt;down&gt; — added to your word review", html)
        self.assertIn("3 looked up this week · 12 in your list", html)
        self.assertIn("come back in /vocab review from tomorrow", html)

    def test_without_word_list(self) -> None:
        html = render_weekly_recap(5, {}, self._outcomes(), promoted=[])
        self.assertIn("❌ take a weight off — worth another look", html)
        self.assertNotIn("Words", html)
        self.assertNotIn("/vocab", html)


class BuildWeeklyRecapTest(unittest.TestCase):
    def test_vocab_disabled_promotes_nothing(self) -> None:
        qdrant = _qdrant()
        with patch("openclaw_runtime.english_weekly.add_for_review") as add:
            html = build_weekly_recap(qdrant, MagicMock(), "coll", "owner-a", 5, vocab_enabled=False, now=NOW)
        add.assert_not_called()
        self.assertIn("worth another look", html)

    def test_vocab_enabled_promotes_and_counts_this_weeks_lookups(self) -> None:
        entries = [
            {"word": "a", "added_at": "2026-10-01T10:00:00+00:00"},
            {"word": "b", "added_at": "2026-09-01T10:00:00+00:00"},
            {"word": "c", "added_at": "2026-10-03T10:00:00+00:00", "source": "weekly_chunk"},
        ]
        with patch("openclaw_runtime.english_weekly.add_for_review") as add, \
                patch("openclaw_runtime.english_weekly.list_word_list", return_value=entries):
            html = build_weekly_recap(_qdrant(), MagicMock(), "coll", "owner-a", 5, vocab_enabled=True, now=NOW)
        self.assertEqual(add.call_count, 2)
        self.assertIn("1 looked up this week · 3 in your list", html)


class SundayPushSendsRecapTest(unittest.TestCase):
    def _push(self, send_report, recap):
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"payload": {"week_number": 5}}]
        with patch("openclaw_runtime.english_bot_scheduler.run_sunday_task") as sunday, \
                patch("openclaw_runtime.english_bot_scheduler.build_weekly_recap", recap):
            run_todays_push(
                day_code="sun", llm=MagicMock(), qdrant=qdrant, embeddings=MagicMock(), collection="coll",
                owners=["a", "b"], send_message=MagicMock(), send_audio=MagicMock(),
                set_pending_answer=MagicMock(), clip_client=MagicMock(), transcription_client=MagicMock(),
                workspace_root=MagicMock(), send_report=send_report, vocab_enabled=True,
            )
        return sunday

    def test_recap_goes_to_every_owner_after_the_reading_card(self) -> None:
        sent = []
        recap = MagicMock(side_effect=lambda q, e, c, owner, week, vocab_enabled: f"recap {owner} wk{week}")
        sunday = self._push(lambda owner, html: sent.append((owner, html)), recap)
        sunday.assert_called_once()
        self.assertEqual(sent, [("a", "recap a wk5"), ("b", "recap b wk5")])
        self.assertTrue(recap.call_args.kwargs["vocab_enabled"])

    def test_one_owners_failure_does_not_block_the_others(self) -> None:
        sent = []

        def recap(q, e, c, owner, week, vocab_enabled):
            if owner == "a":
                raise RuntimeError("qdrant hiccup")
            return "ok"

        self._push(lambda owner, html: sent.append(owner), recap)
        self.assertEqual(sent, ["b"])


if __name__ == "__main__":
    unittest.main()
