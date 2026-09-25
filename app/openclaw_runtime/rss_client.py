from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

_ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


@dataclass(frozen=True)
class RssItem:
    title: str
    link: str
    guid: str
    enclosure_url: str
    pub_date: datetime | None
    pub_date_raw: str
    duration_seconds: float | None


def parse_rss_items(xml_text: str) -> list[RssItem]:
    """Parse an RSS 2.0 feed's <item> list into RssItem records, sorted
    newest-first by <pubDate>. An item needs at least a <link> or an
    <enclosure url="..."> to be addressable -- items with neither are
    skipped rather than raising, since a partially broken feed should still
    yield whatever items are usable. Malformed XML raises ValueError; the
    caller decides how to react (retry, fall back to another feed, etc.) --
    this module only parses, it never implements a specific show's fallback
    policy.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"could not parse RSS XML: {exc}") from exc

    items: list[RssItem] = []
    for item_el in root.iter("item"):
        title = (item_el.findtext("title") or "").strip()
        link = (item_el.findtext("link") or "").strip()
        guid = (item_el.findtext("guid") or "").strip()
        enclosure_el = item_el.find("enclosure")
        enclosure_url = ((enclosure_el.get("url") if enclosure_el is not None else "") or "").strip()
        pub_date_raw = (item_el.findtext("pubDate") or "").strip()
        pub_date = _parse_pub_date(pub_date_raw)
        duration_raw = (item_el.findtext(f"{_ITUNES_NS}duration") or "").strip()
        duration_seconds = _parse_itunes_duration(duration_raw)

        if not link and not enclosure_url:
            continue

        items.append(
            RssItem(
                title=title,
                link=link,
                guid=guid,
                enclosure_url=enclosure_url,
                pub_date=pub_date,
                pub_date_raw=pub_date_raw,
                duration_seconds=duration_seconds,
            )
        )

    items.sort(key=lambda item: item.pub_date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items


def _parse_itunes_duration(raw: str) -> float | None:
    """<itunes:duration> is spec'd as either a plain integer of seconds
    ("179") or a colon-separated HH:MM:SS / MM:SS clock ("51:20"). Real
    feeds use both forms, so both need handling."""
    if not raw:
        return None
    if ":" in raw:
        parts = raw.split(":")
        if not all(part.isdigit() for part in parts) or len(parts) > 3:
            return None
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + int(part)
        return seconds
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_pub_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
