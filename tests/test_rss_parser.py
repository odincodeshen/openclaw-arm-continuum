import unittest

from openclaw_runtime.rss_client import parse_rss_items


BBC_STYLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Desert Island Discs</title>
<item>
<title>Episode Older</title>
<guid isPermaLink="false">urn:bbc:podcast:older</guid>
<enclosure url="https://podcasts.files.bbci.co.uk/older.mp3" type="audio/mpeg" />
<pubDate>Fri, 01 Aug 2026 09:00:00 GMT</pubDate>
</item>
<item>
<title>Episode Newest</title>
<guid isPermaLink="false">urn:bbc:podcast:newest</guid>
<enclosure url="https://podcasts.files.bbci.co.uk/newest.mp3" type="audio/mpeg" />
<pubDate>Fri, 22 Aug 2026 09:00:00 GMT</pubDate>
</item>
<item>
<title>Episode Middle</title>
<guid isPermaLink="false">urn:bbc:podcast:middle</guid>
<enclosure url="https://podcasts.files.bbci.co.uk/middle.mp3" type="audio/mpeg" />
<pubDate>Fri, 15 Aug 2026 09:00:00 GMT</pubDate>
</item>
</channel>
</rss>
"""

GUARDIAN_STYLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Tim Dowling column</title>
<item>
<title>The lawn is out of control</title>
<link>https://www.theguardian.com/lifeandstyle/2026/aug/20/tim-dowling-lawn</link>
<guid isPermaLink="true">https://www.theguardian.com/lifeandstyle/2026/aug/20/tim-dowling-lawn</guid>
<pubDate>Sat, 20 Aug 2026 06:00:00 GMT</pubDate>
</item>
</channel>
</rss>
"""

MISSING_FIELDS_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<item>
<title>Has nothing addressable</title>
<pubDate>Fri, 01 Aug 2026 09:00:00 GMT</pubDate>
</item>
<item>
<title>Has a link only</title>
<link>https://example.com/article</link>
<pubDate>Fri, 02 Aug 2026 09:00:00 GMT</pubDate>
</item>
</channel>
</rss>
"""


class ParseRssItemsTest(unittest.TestCase):
    def test_extracts_enclosure_guid_pub_date(self) -> None:
        items = parse_rss_items(BBC_STYLE_RSS)
        newest = items[0]
        self.assertEqual(newest.title, "Episode Newest")
        self.assertEqual(newest.guid, "urn:bbc:podcast:newest")
        self.assertEqual(newest.enclosure_url, "https://podcasts.files.bbci.co.uk/newest.mp3")
        self.assertIsNotNone(newest.pub_date)

    def test_sorted_newest_first_by_pub_date(self) -> None:
        items = parse_rss_items(BBC_STYLE_RSS)
        titles = [item.title for item in items]
        self.assertEqual(titles, ["Episode Newest", "Episode Middle", "Episode Older"])

    def test_guardian_style_link_only_item_is_kept(self) -> None:
        items = parse_rss_items(GUARDIAN_STYLE_RSS)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].link, "https://www.theguardian.com/lifeandstyle/2026/aug/20/tim-dowling-lawn")
        self.assertEqual(items[0].enclosure_url, "")

    def test_item_with_neither_link_nor_enclosure_is_skipped(self) -> None:
        items = parse_rss_items(MISSING_FIELDS_RSS)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Has a link only")

    def test_malformed_xml_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_rss_items("<rss><channel><item><title>unterminated")

    def test_empty_feed_returns_empty_list(self) -> None:
        items = parse_rss_items("<rss><channel></channel></rss>")
        self.assertEqual(items, [])

    def test_item_missing_pub_date_sorts_last(self) -> None:
        xml = """<rss><channel>
        <item><title>No date</title><link>https://example.com/a</link></item>
        <item><title>Has date</title><link>https://example.com/b</link>
        <pubDate>Fri, 01 Aug 2026 09:00:00 GMT</pubDate></item>
        </channel></rss>"""
        items = parse_rss_items(xml)
        self.assertEqual(items[0].title, "Has date")
        self.assertEqual(items[1].title, "No date")


if __name__ == "__main__":
    unittest.main()
