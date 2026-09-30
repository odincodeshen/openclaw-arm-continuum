"""A minimal Anki package (.apkg) writer -- standard library only.

An .apkg is a zip with a SQLite collection ("collection.anki2", the classic
schema 11 every Anki version still imports), a "media" JSON map and the
media files named "0", "1", ... . One note type, one deck, one card per
note. Note ids and guids come from the word, so importing a newer export
updates the same notes instead of adding duplicates.
"""

import hashlib
import json
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE col (id integer primary key, crt integer not null, mod integer not null, scm integer not null,
  ver integer not null, dty integer not null, usn integer not null, ls integer not null, conf text not null,
  models text not null, decks text not null, dconf text not null, tags text not null);
CREATE TABLE notes (id integer primary key, guid text not null, mid integer not null, mod integer not null,
  usn integer not null, tags text not null, flds text not null, sfld integer not null, csum integer not null,
  flags integer not null, data text not null);
CREATE TABLE cards (id integer primary key, nid integer not null, did integer not null, ord integer not null,
  mod integer not null, usn integer not null, type integer not null, queue integer not null, due integer not null,
  ivl integer not null, factor integer not null, reps integer not null, lapses integer not null,
  left integer not null, odue integer not null, odid integer not null, flags integer not null, data text not null);
CREATE TABLE revlog (id integer primary key, cid integer not null, usn integer not null, ease integer not null,
  ivl integer not null, lastIvl integer not null, factor integer not null, time integer not null,
  type integer not null);
CREATE TABLE graves (usn integer not null, oid integer not null, type integer not null);
CREATE INDEX ix_notes_usn on notes (usn);
CREATE INDEX ix_cards_usn on cards (usn);
CREATE INDEX ix_revlog_usn on revlog (usn);
CREATE INDEX ix_cards_nid on cards (nid);
CREATE INDEX ix_cards_sched on cards (did, queue, due);
CREATE INDEX ix_revlog_cid on revlog (cid);
CREATE INDEX ix_notes_csum on notes (csum);
"""

DECK_CONF = {
    "1": {
        "id": 1, "name": "Default", "mod": 0, "usn": 0, "maxTaken": 60, "autoplay": True, "timer": 0,
        "replayq": True, "dyn": False,
        "new": {"bury": True, "delays": [1, 10], "initialFactor": 2500, "ints": [1, 4, 7], "order": 1,
                "perDay": 20, "separate": True},
        "lapse": {"delays": [10], "leechAction": 0, "leechFails": 8, "minInt": 1, "mult": 0},
        "rev": {"bury": True, "ease4": 1.3, "fuzz": 0.05, "ivlFct": 1, "maxIvl": 36500, "minSpace": 1,
                "perDay": 100},
    }
}

FIELDS = ["Word", "Phonetic", "Meaning", "Sentence", "AudioUK", "AudioUS"]
FRONT = "<div class=word>{{Word}}</div>{{AudioUK}}"
BACK = (
    "{{FrontSide}}<hr id=answer>"
    "{{#Phonetic}}<div class=ph>/{{Phonetic}}/</div>{{/Phonetic}}"
    "<div>{{Meaning}}</div>"
    "{{#Sentence}}<div class=ex><i>{{Sentence}}</i></div>{{/Sentence}}"
    "{{#AudioUS}}<div class=us>US {{AudioUS}}</div>{{/AudioUS}}"
)
CSS = (
    ".card{font-family:arial;font-size:22px;text-align:center;color:black;background-color:white}"
    ".word{font-size:32px;font-weight:bold}.ph{color:#666}.ex{margin-top:12px;color:#333}.us{margin-top:8px;font-size:16px}"
)


def _stable_id(text: str, digits: int = 13) -> int:
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest(), 16) % (10 ** digits)


def _checksum(text: str) -> int:
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16)


@dataclass
class AnkiNote:
    word: str
    phonetic: str = ""
    meaning: str = ""
    sentence: str = ""
    audio_uk: bytes | None = None
    audio_us: bytes | None = None
    tags: list[str] = field(default_factory=list)


def write_apkg(path: Path, deck_name: str, notes: list[AnkiNote], *, now: float | None = None) -> int:
    """Write the package; returns the number of notes."""
    now = int(now or time.time())
    deck_id = _stable_id("deck:" + deck_name)
    model_id = _stable_id("model:OpenClaw word")
    model = {
        str(model_id): {
            "id": model_id, "name": "OpenClaw word", "type": 0, "mod": now, "usn": -1, "sortf": 0,
            "did": deck_id, "tags": [], "vers": [], "css": CSS,
            "latexPre": "", "latexPost": "", "latexsvg": False, "req": [[0, "any", [0]]],
            "flds": [
                {"name": name, "ord": index, "sticky": False, "rtl": False, "font": "Arial", "size": 20,
                 "media": []}
                for index, name in enumerate(FIELDS)
            ],
            "tmpls": [{"name": "Card 1", "ord": 0, "qfmt": FRONT, "afmt": BACK, "did": None,
                       "bqfmt": "", "bafmt": ""}],
        }
    }
    deck_template = {"mod": now, "usn": -1, "lrnToday": [0, 0], "revToday": [0, 0], "newToday": [0, 0],
                     "timeToday": [0, 0], "collapsed": False, "browserCollapsed": False, "desc": "",
                     "dyn": 0, "conf": 1, "extendNew": 0, "extendRev": 0}
    decks = {
        "1": {**deck_template, "id": 1, "name": "Default"},
        str(deck_id): {**deck_template, "id": deck_id, "name": deck_name},
    }
    conf = {"activeDecks": [1], "curDeck": 1, "newSpread": 0, "collapseTime": 1200, "timeLim": 0,
            "estTimes": True, "dueCounts": True, "curModel": None, "nextPos": 1, "sortType": "noteFld",
            "sortBackwards": False, "addToCur": True}

    media: dict[str, str] = {}
    media_files: list[tuple[str, bytes]] = []

    def add_media(data: bytes | None, filename: str) -> str:
        if not data:
            return ""
        key = str(len(media_files))
        media[key] = filename
        media_files.append((key, data))
        return f"[sound:{filename}]"

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "collection.anki2"
        con = sqlite3.connect(db_path)
        con.executescript(SCHEMA)
        con.execute(
            "INSERT INTO col VALUES (1, ?, ?, ?, 11, 0, 0, 0, ?, ?, ?, ?, '{}')",
            (now, now * 1000, now * 1000, json.dumps(conf), json.dumps(model), json.dumps(decks),
             json.dumps(DECK_CONF)),
        )
        seen: set[int] = set()
        for position, note in enumerate(notes):
            note_id = _stable_id("note:" + note.word.lower())
            if not note.word or note_id in seen:
                continue
            seen.add(note_id)
            slug = hashlib.sha1(note.word.lower().encode("utf-8")).hexdigest()[:10]
            fields = [
                note.word, note.phonetic, note.meaning, note.sentence,
                add_media(note.audio_uk, f"openclaw_{slug}_uk.mp3"),
                add_media(note.audio_us, f"openclaw_{slug}_us.mp3"),
            ]
            con.execute(
                "INSERT INTO notes VALUES (?, ?, ?, ?, -1, ?, ?, ?, ?, 0, '')",
                (note_id, f"oc{note_id}", model_id, now, " " + " ".join(note.tags) + " " if note.tags else "",
                 "\x1f".join(fields), note.word, _checksum(note.word)),
            )
            con.execute(
                "INSERT INTO cards VALUES (?, ?, ?, 0, ?, -1, 0, 0, ?, 0, 0, 0, 0, 0, 0, 0, 0, '')",
                (note_id + 1, note_id, deck_id, now, position + 1),
            )
        con.commit()
        con.close()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
            package.write(db_path, "collection.anki2")
            package.writestr("media", json.dumps(media))
            for key, data in media_files:
                package.writestr(key, data)
    return len(seen)
