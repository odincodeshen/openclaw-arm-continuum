import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openclaw_runtime.skills.english_bot import (
    Chunk,
    WeeklyContent,
    build_monday_message,
    evaluate_monday_reply,
    extract_chunks,
    fetch_latest_episode,
    is_episode_processed,
    next_week_number,
    run_monday_task,
    select_guest_dominant_window,
    store_weekly_content,
)
from openclaw_runtime.transcription_client import TimestampedTranscript, TranscriptSegment


BBC_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
<title>Newest Episode</title>
<guid isPermaLink="false">urn:bbc:podcast:newest</guid>
<enclosure url="https://podcasts.files.bbci.co.uk/newest.mp3" type="audio/mpeg" />
<pubDate>Fri, 22 Aug 2026 09:00:00 GMT</pubDate>
</item>
</channel></rss>
"""


class FakeLlm:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.calls.append((prompt, schema_name))
        return self.responses.pop(0)


class FetchLatestEpisodeTest(unittest.TestCase):
    def test_returns_newest_item(self) -> None:
        episode = fetch_latest_episode(BBC_RSS)
        self.assertEqual(episode.guid, "urn:bbc:podcast:newest")
        self.assertEqual(episode.enclosure_url, "https://podcasts.files.bbci.co.uk/newest.mp3")

    def test_empty_feed_returns_none(self) -> None:
        self.assertIsNone(fetch_latest_episode("<rss><channel></channel></rss>"))


class IsEpisodeProcessedTest(unittest.TestCase):
    def test_true_when_guid_already_stored(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"id": "p1"}]
        self.assertTrue(is_episode_processed(qdrant, "coll", "urn:bbc:podcast:newest"))
        qdrant.scroll_by_filters.assert_called_once_with(
            "coll", {"kind": "weekly_content", "episode_guid": "urn:bbc:podcast:newest"}, limit=1
        )

    def test_false_when_no_match(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        self.assertFalse(is_episode_processed(qdrant, "coll", "urn:bbc:podcast:newest"))

    def test_false_for_empty_guid_without_calling_qdrant(self) -> None:
        qdrant = MagicMock()
        self.assertFalse(is_episode_processed(qdrant, "coll", ""))
        qdrant.scroll_by_filters.assert_not_called()


class NextWeekNumberTest(unittest.TestCase):
    def test_starts_at_one_when_nothing_stored(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        self.assertEqual(next_week_number(qdrant, "coll"), 1)

    def test_increments_past_highest_existing_week(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [
            {"payload": {"week_number": 3}},
            {"payload": {"week_number": 1}},
            {"payload": {"week_number": 5}},
        ]
        self.assertEqual(next_week_number(qdrant, "coll"), 6)


class SelectGuestDominantWindowTest(unittest.TestCase):
    def test_builds_prompt_and_parses_response(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "start_seconds": 120.0,
                        "end_seconds": 300.0,
                        "excerpt_text": "I remember walking down the street when...",
                    }
                )
            ]
        )
        segments = [
            TranscriptSegment(start=0.0, end=5.0, text="So tell me about your childhood."),
            TranscriptSegment(start=5.0, end=120.0, text="I remember walking down the street when..."),
        ]
        result = select_guest_dominant_window(llm, segments)
        self.assertEqual(result["start_seconds"], 120.0)
        self.assertEqual(result["end_seconds"], 300.0)
        self.assertIn("excerpt_text", result)
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "guest_dominant_window")
        self.assertIn("[0.0-5.0] So tell me about your childhood.", prompt)
        self.assertIn("backchannel", prompt.lower())
        self.assertIn("75%", prompt)


class ExtractChunksTest(unittest.TestCase):
    def test_parses_three_chunks(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "chunks": [
                            {"phrase": "take a gamble on", "definition": "冒險一試", "context_sentence": "x"},
                            {"phrase": "spread oneself too thin", "definition": "心力過度分散", "context_sentence": "y"},
                            {"phrase": "get to grips with", "definition": "掌握複雜事物", "context_sentence": "z"},
                        ]
                    }
                )
            ]
        )
        chunks = extract_chunks(llm, "some excerpt text")
        self.assertEqual(len(chunks), 3)
        self.assertIsInstance(chunks[0], Chunk)
        self.assertEqual(chunks[0].phrase, "take a gamble on")
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "chunk_extraction")


class StoreWeeklyContentTest(unittest.TestCase):
    def test_writes_three_points_shared_no_owner(self) -> None:
        qdrant = MagicMock()
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1, 0.2]

        content = WeeklyContent(
            week_number=7,
            episode_title="Some Guest",
            episode_guid="urn:bbc:podcast:x",
            segment_start=300.0,
            segment_end=480.0,
            transcript_excerpt="the full excerpt text",
            chunks=[
                Chunk("a phrase", "定義", "example a"),
                Chunk("b phrase", "定義", "example b"),
                Chunk("c phrase", "定義", "example c"),
            ],
            window_segments=[TranscriptSegment(start=0.0, end=2.0, text="hello")],
        )
        store_weekly_content(qdrant, embeddings, "coll", content)

        self.assertEqual(qdrant.upsert_text.call_count, 3)
        for call in qdrant.upsert_text.call_args_list:
            args, kwargs = call
            collection, text, vector, payload = args
            self.assertEqual(collection, "coll")
            self.assertNotIn("owner", payload)
            self.assertEqual(payload["tag"], "eng_wk7")
            self.assertEqual(payload["week_number"], 7)
            self.assertEqual(payload["episode_guid"], "urn:bbc:podcast:x")
            self.assertEqual(payload["transcript_excerpt"], "the full excerpt text")
            self.assertIn("window_segments_json", payload)
            segments_back = json.loads(payload["window_segments_json"])
            self.assertEqual(segments_back, [{"start": 0.0, "end": 2.0, "text": "hello"}])
            self.assertFalse(payload["mastered"])
            self.assertFalse(payload["needs_review"])


class BuildMondayMessageTest(unittest.TestCase):
    def test_message_includes_timestamps_and_chunks(self) -> None:
        content = WeeklyContent(
            week_number=1,
            episode_title="Demis Hassabis",
            episode_guid="urn:x",
            segment_start=300.0,
            segment_end=480.0,
            transcript_excerpt="excerpt",
            chunks=[Chunk("take a gamble on", "冒險一試", "We took a gamble on it.")],
            window_segments=[],
        )
        message = build_monday_message(content)
        self.assertIn("Demis Hassabis", message)
        self.assertIn("300s", message)
        self.assertIn("480s", message)
        self.assertIn("take a gamble on", message)
        self.assertIn("冒險一試", message)


class RunMondayTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace_dir = Path(self.tmp.name) / "wk"

        self.qdrant = MagicMock()
        self.qdrant.scroll_by_filters.return_value = []  # not processed, no prior weeks
        self.embeddings = MagicMock()
        self.embeddings.embed.return_value = [0.1, 0.2]

        self.clip_client = MagicMock()
        self.clip_client.clip.return_value = 1200.0

        self.transcription_client = MagicMock()
        self.transcription_client.transcribe_with_segments.return_value = TimestampedTranscript(
            text="full text",
            duration=1200.0,
            segments=[
                TranscriptSegment(start=0.0, end=10.0, text="Host: so tell me..."),
                TranscriptSegment(start=10.0, end=190.0, text="Guest: I remember..."),
            ],
        )

        self.llm = FakeLlm(
            [
                json.dumps(
                    {"start_seconds": 10.0, "end_seconds": 190.0, "excerpt_text": "Guest: I remember..."}
                ),
                json.dumps(
                    {
                        "chunks": [
                            {"phrase": "p1", "definition": "d1", "context_sentence": "s1"},
                            {"phrase": "p2", "definition": "d2", "context_sentence": "s2"},
                            {"phrase": "p3", "definition": "d3", "context_sentence": "s3"},
                        ]
                    }
                ),
            ]
        )

        self.sent: list[tuple[str, str]] = []

        def send_message(owner, text):
            self.sent.append((owner, text))

        self.send_message = send_message

    @patch("openclaw_runtime.skills.english_bot.get_bytes")
    @patch("openclaw_runtime.skills.english_bot.get_text")
    def test_full_pipeline_downloads_clips_transcribes_stores_and_sends(self, get_text, get_bytes) -> None:
        get_text.return_value = BBC_RSS
        get_bytes.return_value = b"fake mp3 bytes"

        content = run_monday_task(
            clip_client=self.clip_client,
            transcription_client=self.transcription_client,
            llm=self.llm,
            qdrant=self.qdrant,
            embeddings=self.embeddings,
            collection="coll",
            owners=["owner-a", "owner-b"],
            send_message=self.send_message,
            workspace_dir=self.workspace_dir,
        )

        self.assertIsNotNone(content)
        self.assertEqual(content.week_number, 1)
        self.assertEqual(len(content.chunks), 3)

        # episode + window mp3 both written to the workspace dir
        self.assertTrue((self.workspace_dir / "episode.mp3").exists())
        self.clip_client.clip.assert_called_once()
        clip_args = self.clip_client.clip.call_args.args
        self.assertEqual(clip_args[0], self.workspace_dir / "episode.mp3")
        self.assertEqual(clip_args[1], 180.0)  # INTRO_SKIP_SECONDS

        # sent to both owners
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[0][0], "owner-a")
        self.assertIn("Newest Episode", self.sent[0][1])

        # daily task tracking: mark_task_pushed writes an owned point per owner
        self.assertEqual(self.qdrant.upsert_text.call_count, 3 + 2)  # 3 chunks + 2 owners pushed

    @patch("openclaw_runtime.skills.english_bot.get_bytes")
    @patch("openclaw_runtime.skills.english_bot.get_text")
    def test_already_processed_episode_returns_none_and_sends_nothing(self, get_text, get_bytes) -> None:
        get_text.return_value = BBC_RSS
        self.qdrant.scroll_by_filters.return_value = [{"id": "already-there"}]

        result = run_monday_task(
            clip_client=self.clip_client,
            transcription_client=self.transcription_client,
            llm=self.llm,
            qdrant=self.qdrant,
            embeddings=self.embeddings,
            collection="coll",
            owners=["owner-a"],
            send_message=self.send_message,
            workspace_dir=self.workspace_dir,
        )

        self.assertIsNone(result)
        self.assertEqual(self.sent, [])
        get_bytes.assert_not_called()
        self.clip_client.clip.assert_not_called()

    @patch("openclaw_runtime.skills.english_bot.get_text")
    def test_feed_with_no_enclosure_raises(self, get_text) -> None:
        get_text.return_value = "<rss><channel><item><title>x</title><link>https://x</link></item></channel></rss>"
        with self.assertRaises(ValueError):
            run_monday_task(
                clip_client=self.clip_client,
                transcription_client=self.transcription_client,
                llm=self.llm,
                qdrant=self.qdrant,
                embeddings=self.embeddings,
                collection="coll",
                owners=["owner-a"],
                send_message=self.send_message,
                workspace_dir=self.workspace_dir,
            )


CHUNKS = [
    {"phrase": "take a gamble on", "definition": "冒險一試", "context_sentence": "We take a gamble on it."},
    {"phrase": "spread oneself too thin", "definition": "心力過度分散", "context_sentence": "I was spreading myself too thin."},
    {"phrase": "get to grips with", "definition": "掌握複雜事物", "context_sentence": "It took weeks to get to grips with it."},
]


class EvaluateMondayReplyTest(unittest.TestCase):
    """Monday's reply-with-one-example-sentence step had no evaluation
    function at all through v1.11-v1.14 -- added while wiring up full
    daily-completion tracking (v1.15), since without it Monday could never
    be marked completed."""

    def setUp(self) -> None:
        self.qdrant = MagicMock()

        def scroll_by_filters(collection, filters, limit=64):
            if filters.get("kind") == "daily_task":
                return [{"id": "pushed-point-1", "payload": {"completed": False}}]
            return []  # no existing chunk_progress record -> record_chunk_usage takes the "new" path

        self.qdrant.scroll_by_filters.side_effect = scroll_by_filters
        self.embeddings = MagicMock()
        self.embeddings.embed.return_value = [0.1]

    def test_writes_back_user_sentence_with_mon_reply_source(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "chunk_results": [
                            {
                                "phrase": "take a gamble on",
                                "used_correctly": True,
                                "user_sentence": "I take a gamble on the new job.",
                            },
                            {"phrase": "spread oneself too thin", "used_correctly": False, "user_sentence": ""},
                            {"phrase": "get to grips with", "used_correctly": False, "user_sentence": ""},
                        ]
                    }
                )
            ]
        )

        message = evaluate_monday_reply(
            self.qdrant, self.embeddings, llm, "coll", "owner-a", 1, CHUNKS, "I take a gamble on the new job."
        )

        self.assertIn("thanks", message.lower())
        self.qdrant.upsert_text.assert_called_once()
        payload = self.qdrant.upsert_text.call_args.args[3]
        self.assertEqual(payload["user_sentence"], "I take a gamble on the new job.")
        self.assertEqual(payload["user_sentence_source"], "mon_reply")

    def test_no_chunk_detected_still_marks_completed_with_gentle_message(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "chunk_results": [
                            {"phrase": c["phrase"], "used_correctly": False, "user_sentence": ""} for c in CHUNKS
                        ]
                    }
                )
            ]
        )

        message = evaluate_monday_reply(
            self.qdrant, self.embeddings, llm, "coll", "owner-a", 1, CHUNKS, "just rambling, no chunk here"
        )

        self.assertIn("couldn't spot", message)
        self.qdrant.upsert_text.assert_not_called()

    def test_marks_monday_task_completed(self) -> None:
        llm = FakeLlm(
            [
                json.dumps(
                    {
                        "chunk_results": [
                            {"phrase": c["phrase"], "used_correctly": False, "user_sentence": ""} for c in CHUNKS
                        ]
                    }
                )
            ]
        )
        evaluate_monday_reply(self.qdrant, self.embeddings, llm, "coll", "owner-a", 1, CHUNKS, "reply text")
        self.qdrant.set_payload.assert_called_once_with("coll", "pushed-point-1", {"completed": True})


if __name__ == "__main__":
    unittest.main()
