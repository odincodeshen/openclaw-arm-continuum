import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from openclaw_runtime.skills.english_bot import (
    GUARDIAN_GRACE_DENT_RSS,
    GUARDIAN_LIFESTYLE_RSS,
    GUARDIAN_TIM_DOWLING_RSS,
    SUNDAY_FEEDS,
    build_sunday_message,
    extract_article_text,
    is_article_pushed,
    mark_article_pushed,
    run_sunday_task,
    select_sunday_article,
    summarize_sunday_article,
)


class FakeLlm:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, prompt, schema, *, schema_name, max_tokens=None):
        self.calls.append((prompt, schema_name))
        return self.responses.pop(0)


def _rss(items: list[tuple[str, str, str]]) -> str:
    """items: list of (title, link, pub_date_rfc2822)."""
    entries = "".join(
        f"<item><title>{title}</title><link>{link}</link><guid>{link}</guid>"
        f"<pubDate>{pub_date}</pubDate></item>"
        for title, link, pub_date in items
    )
    return f"<rss><channel>{entries}</channel></rss>"


NOW = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)  # a Sunday


def _rfc2822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


class IsArticlePushedTest(unittest.TestCase):
    def test_empty_url_is_never_pushed(self) -> None:
        qdrant = MagicMock()
        self.assertFalse(is_article_pushed(qdrant, "coll", ""))
        qdrant.scroll_by_filters.assert_not_called()

    def test_checks_shared_no_owner_tag(self) -> None:
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"id": "p1"}]
        self.assertTrue(is_article_pushed(qdrant, "coll", "https://example.com/a"))
        qdrant.scroll_by_filters.assert_called_once_with(
            "coll", {"tag": "eng_sunday_pushed", "article_url": "https://example.com/a"}, limit=1
        )

    def test_mark_article_pushed_writes_shared_record(self) -> None:
        qdrant = MagicMock()
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        mark_article_pushed(qdrant, embeddings, "coll", "https://example.com/a")
        args, kwargs = qdrant.upsert_text.call_args
        collection, text, vector, payload = args
        self.assertEqual(payload["tag"], "eng_sunday_pushed")
        self.assertEqual(payload["article_url"], "https://example.com/a")
        self.assertNotIn("owner", payload)


class SelectSundayArticleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.qdrant = MagicMock()
        self.qdrant.scroll_by_filters.return_value = []  # nothing pushed yet, by default

    def test_tier1_uses_newest_item_when_fresh_and_unpushed(self) -> None:
        """Newest item published within the last 7 days -> use it directly."""
        rss_xml = _rss(
            [
                ("Fresh one", "https://guardian.com/fresh", _rfc2822(NOW - timedelta(days=1))),
                ("Older one", "https://guardian.com/older", _rfc2822(NOW - timedelta(days=20))),
            ]
        )
        chosen = select_sunday_article(self.qdrant, "coll", lambda url: rss_xml, now=NOW)
        self.assertEqual(chosen.link, "https://guardian.com/fresh")

    def test_tier2_falls_back_to_older_unpushed_item_in_same_feed(self) -> None:
        """Newest item is stale (>7 days) AND already pushed, but an older
        (<=60 days) item in the same feed's first 5 hasn't been pushed yet
        -> use that instead of jumping straight to a different feed."""
        rss_xml = _rss(
            [
                ("Stale and pushed", "https://guardian.com/stale", _rfc2822(NOW - timedelta(days=10))),
                ("Usable older", "https://guardian.com/older-ok", _rfc2822(NOW - timedelta(days=40))),
            ]
        )
        qdrant = MagicMock()
        qdrant.scroll_by_filters.side_effect = lambda collection, filters, limit=1: (
            [{"id": "already-pushed"}] if filters.get("article_url") == "https://guardian.com/stale" else []
        )
        chosen = select_sunday_article(qdrant, "coll", lambda url: rss_xml, now=NOW)
        self.assertEqual(chosen.link, "https://guardian.com/older-ok")

    def test_tier1_prefers_newest_over_tier2_when_both_fresh_and_unpushed(self) -> None:
        """Sanity check for the tier-2 fixture above: when the newest item
        is NOT already pushed, tier 2's looser 60-day window naturally
        re-selects it too (still within range) -- this is correct, not a
        bug, since tier 2's job is "any unpushed item in ~2 months", which
        legitimately includes an item that only failed tier 1's stricter
        7-day bar."""
        rss_xml = _rss(
            [
                ("Just outside 7 days", "https://guardian.com/stale", _rfc2822(NOW - timedelta(days=10))),
                ("Older", "https://guardian.com/older-ok", _rfc2822(NOW - timedelta(days=40))),
            ]
        )
        chosen = select_sunday_article(self.qdrant, "coll", lambda url: rss_xml, now=NOW)
        self.assertEqual(chosen.link, "https://guardian.com/stale")

    def test_tier3_switches_feed_when_primary_feed_has_nothing_usable(self) -> None:
        """Primary feed has only stale, already-old-beyond-60-days items ->
        must move on to the secondary feed entirely."""
        primary_xml = _rss(
            [("Too old", "https://guardian.com/too-old", _rfc2822(NOW - timedelta(days=200)))]
        )
        secondary_xml = _rss(
            [("Backup fresh", "https://guardian.com/backup", _rfc2822(NOW - timedelta(days=2)))]
        )

        def fetch(url: str) -> str:
            if url == GUARDIAN_TIM_DOWLING_RSS:
                return primary_xml
            if url == GUARDIAN_GRACE_DENT_RSS:
                return secondary_xml
            raise AssertionError(f"should not need the third feed: {url}")

        chosen = select_sunday_article(self.qdrant, "coll", fetch, now=NOW)
        self.assertEqual(chosen.link, "https://guardian.com/backup")

    def test_falls_through_all_three_feeds_to_the_lifestyle_catch_all(self) -> None:
        empty_xml = _rss([])
        lifestyle_xml = _rss(
            [("Catch all", "https://guardian.com/catch-all", _rfc2822(NOW - timedelta(days=1)))]
        )

        def fetch(url: str) -> str:
            if url == GUARDIAN_LIFESTYLE_RSS:
                return lifestyle_xml
            return empty_xml

        chosen = select_sunday_article(self.qdrant, "coll", fetch, now=NOW)
        self.assertEqual(chosen.link, "https://guardian.com/catch-all")
        self.assertEqual(SUNDAY_FEEDS[-1], GUARDIAN_LIFESTYLE_RSS)

    def test_raises_when_all_feeds_exhausted(self) -> None:
        with self.assertRaises(ValueError):
            select_sunday_article(self.qdrant, "coll", lambda url: _rss([]), now=NOW)

    def test_already_pushed_newest_item_is_skipped_even_if_fresh(self) -> None:
        rss_xml = _rss([("Fresh but pushed", "https://guardian.com/fresh", _rfc2822(NOW - timedelta(days=1)))])
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = [{"id": "already-pushed"}]
        with self.assertRaises(ValueError):
            select_sunday_article(qdrant, "coll", lambda url: rss_xml, now=NOW)


class ExtractArticleTextTest(unittest.TestCase):
    def test_extracts_paragraphs_inside_article_tag_only(self) -> None:
        html = (
            "<html><body>"
            "<nav><p>Should be ignored, outside article</p></nav>"
            "<article>"
            "<h1>Headline</h1>"
            "<p>First paragraph with <a href='#'>a link</a> inside it.</p>"
            "<p>Second paragraph.</p>"
            "</article>"
            "<footer><p>Also ignored</p></footer>"
            "</body></html>"
        )
        text = extract_article_text(html)
        self.assertIn("First paragraph with a link inside it.", text)
        self.assertIn("Second paragraph.", text)
        self.assertNotIn("Should be ignored", text)
        self.assertNotIn("Also ignored", text)

    def test_no_article_tag_returns_empty_string(self) -> None:
        self.assertEqual(extract_article_text("<html><body><p>No article wrapper</p></body></html>"), "")

    def test_empty_paragraphs_are_skipped(self) -> None:
        html = "<article><p>   </p><p>Real content.</p></article>"
        self.assertEqual(extract_article_text(html), "Real content.")


class SummarizeSundayArticleTest(unittest.TestCase):
    def test_grounds_summary_in_real_fetched_text_not_title_alone(self) -> None:
        llm = FakeLlm([json.dumps({"summary": "A witty column about garden chaos."})])
        summary = summarize_sunday_article(llm, "The full real article text about a chaotic garden.")
        self.assertEqual(summary, "A witty column about garden chaos.")
        prompt, schema_name = llm.calls[0]
        self.assertEqual(schema_name, "sunday_summary")
        self.assertIn("The full real article text about a chaotic garden.", prompt)


class BuildSundayMessageTest(unittest.TestCase):
    def test_includes_link_summary_and_gist_reminder(self) -> None:
        message = build_sunday_message("https://guardian.com/a", "A nice summary.")
        self.assertIn("https://guardian.com/a", message)
        self.assertIn("A nice summary.", message)
        self.assertIn("no dictionary, no notes", message.lower())


class RunSundayTaskTest(unittest.TestCase):
    def test_pushes_summary_marks_article_and_task_completed(self) -> None:
        rss_xml = _rss([("Piece", "https://guardian.com/piece", _rfc2822(NOW - timedelta(days=1)))])
        article_html = "<article><p>Real article content here.</p></article>"
        llm = FakeLlm([json.dumps({"summary": "Fifty word summary."})])
        qdrant = MagicMock()

        def scroll_side_effect(collection, filters, limit=64):
            if filters.get("tag") == "eng_sunday_pushed":
                return []  # not pushed yet -> article passes dedup
            if filters.get("kind") == "daily_task":
                # simulates mark_task_pushed's earlier write existing to find
                return [{"id": f"pushed-{filters.get('owner', 'x')}", "payload": {"completed": False}}]
            return []

        qdrant.scroll_by_filters.side_effect = scroll_side_effect
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]
        sent = []

        summary = run_sunday_task(
            llm=llm,
            qdrant=qdrant,
            embeddings=embeddings,
            collection="coll",
            week_number=5,
            owners=["owner-a", "owner-b"],
            send_message=lambda owner, text: sent.append((owner, text)),
            fetch_rss=lambda url: rss_xml,
            fetch_article_html=lambda url: article_html,
        )

        self.assertEqual(summary, "Fifty word summary.")
        self.assertEqual(len(sent), 2)
        self.assertTrue(all("https://guardian.com/piece" in text for _, text in sent))
        # article dedup mark + at least the two mark_task_pushed/mark_task_completed writes
        self.assertGreaterEqual(qdrant.upsert_text.call_count, 3)
        # Sunday has no reply to evaluate, so completion is marked immediately
        completed_calls = [
            c for c in qdrant.set_payload.call_args_list if c.args[2] == {"completed": True}
        ]
        self.assertEqual(len(completed_calls), 2)

    def test_raises_when_extracted_article_text_is_empty(self) -> None:
        rss_xml = _rss([("Piece", "https://guardian.com/piece", _rfc2822(NOW - timedelta(days=1)))])
        qdrant = MagicMock()
        qdrant.scroll_by_filters.return_value = []
        embeddings = MagicMock()
        embeddings.embed.return_value = [0.1]

        with self.assertRaises(ValueError):
            run_sunday_task(
                llm=FakeLlm([]),
                qdrant=qdrant,
                embeddings=embeddings,
                collection="coll",
                week_number=5,
                owners=["owner-a"],
                send_message=lambda owner, text: None,
                fetch_rss=lambda url: rss_xml,
                fetch_article_html=lambda url: "<html><body>no article tag here</body></html>",
            )


if __name__ == "__main__":
    unittest.main()
