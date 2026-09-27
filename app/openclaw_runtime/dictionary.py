"""Offline English-Chinese dictionary backed by a local SQLite file built from
ECDICT (https://github.com/skywind3000/ECDICT, MIT licensed).

The runtime only reads the SQLite file with the standard library -- the
gateway container has no extra packages. Building it (and converting
ECDICT's Simplified Chinese to Traditional) is a one-off host step, see
scripts/build_dictionary.py and docs/DICTIONARY.md.
"""

import csv
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SCHEMA = """
CREATE TABLE entries (
    word TEXT NOT NULL,
    lower_word TEXT NOT NULL,
    phonetic TEXT,
    translation TEXT,
    definition TEXT,
    collins INTEGER,
    oxford INTEGER,
    tag TEXT,
    exchange TEXT
);
CREATE INDEX entries_word ON entries (word);
CREATE INDEX entries_lower_word ON entries (lower_word);
"""

# ECDICT exam tags worth showing, in display order. Chinese school exams
# (zk/gk/ky) are left out -- they mean nothing to this bot's users.
TAG_LABELS = {"ielts": "IELTS", "toefl": "TOEFL", "gre": "GRE", "cet4": "CET-4", "cet6": "CET-6"}

MAX_QUERY_CHARS = 64


def _clean_multiline(value: str) -> str:
    # ECDICT stores line breaks inside a field as a literal backslash-n.
    return (value or "").replace("\\n", "\n").strip()


def build_dictionary_db(
    csv_path: Path, db_path: Path, convert: Callable[[str], str], *, batch_size: int = 5000
) -> int:
    """Import ECDICT's ecdict.csv into a fresh SQLite file, running every
    Chinese translation through ``convert`` (Simplified -> Traditional).
    Rows with neither a translation nor an English definition are skipped.
    Writes to a temp file first so a half-built dictionary never replaces a
    working one. Returns the number of entries written."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)
    connection = sqlite3.connect(tmp_path)
    count = 0
    try:
        connection.executescript(SCHEMA)
        batch: list[tuple] = []
        with open(csv_path, encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                word = (row.get("word") or "").strip()
                translation = _clean_multiline(row.get("translation", ""))
                definition = _clean_multiline(row.get("definition", ""))
                if not word or not (translation or definition):
                    continue
                batch.append(
                    (
                        word,
                        word.lower(),
                        (row.get("phonetic") or "").strip(),
                        convert(translation) if translation else "",
                        definition,
                        int(row.get("collins") or 0),
                        int(row.get("oxford") or 0),
                        (row.get("tag") or "").strip(),
                        (row.get("exchange") or "").strip(),
                    )
                )
                if len(batch) >= batch_size:
                    connection.executemany("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?)", batch)
                    count += len(batch)
                    batch = []
        if batch:
            connection.executemany("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?)", batch)
            count += len(batch)
        connection.commit()
    finally:
        connection.close()
    tmp_path.replace(db_path)
    return count


@dataclass(frozen=True)
class DictionaryEntry:
    word: str
    phonetic: str
    translation_lines: list[str]
    definition_lines: list[str]
    tags: list[str]
    base_form: str  # "" unless this word is an inflected form (ran -> run)


def normalize_query(text: str) -> str:
    """Trim surrounding quotes/punctuation and collapse whitespace, so a word
    copied out of a sentence ("resilient," or 'resilient') still matches."""
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    cleaned = cleaned.strip(" \"'“”‘’.,!?;:()[]")
    return cleaned[:MAX_QUERY_CHARS]


def _tags_for(collins: int, oxford: int, tag: str) -> list[str]:
    present = set((tag or "").split())
    labels = [label for key, label in TAG_LABELS.items() if key in present]
    if oxford:
        labels.append("Oxford 3000")
    if collins:
        labels.append(f"Collins {collins}/5")
    return labels


def _base_form(exchange: str) -> str:
    # ECDICT exchange field, e.g. "0:run/1:p" on "ran": 0 = lemma.
    for part in (exchange or "").split("/"):
        if part.startswith("0:"):
            return part[2:].strip()
    return ""


class LocalDictionary:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def available(self) -> bool:
        return self.db_path.is_file()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def _find_row(self, connection: sqlite3.Connection, word: str) -> tuple | None:
        columns = "word, phonetic, translation, definition, collins, oxford, tag, exchange"
        row = connection.execute(f"SELECT {columns} FROM entries WHERE word = ? LIMIT 1", (word,)).fetchone()
        if row:
            return row
        # Case-insensitive fallback, preferring the all-lowercase headword.
        return connection.execute(
            f"SELECT {columns} FROM entries WHERE lower_word = ? ORDER BY word != lower_word LIMIT 1",
            (word.lower(),),
        ).fetchone()

    def lookup(self, query: str) -> DictionaryEntry | None:
        word = normalize_query(query)
        if not word or not self.available():
            return None
        connection = self._connect()
        try:
            row = self._find_row(connection, word)
            if row is None:
                return None
            headword, phonetic, translation, definition, collins, oxford, tag, exchange = row
            base_form = _base_form(exchange)
            if base_form.lower() == headword.lower():
                base_form = ""
            if not translation and base_form:
                # An inflected form with no meaning of its own: show the base word's.
                base_row = self._find_row(connection, base_form)
                if base_row:
                    translation = base_row[2]
                    phonetic = phonetic or base_row[1]
        finally:
            connection.close()
        return DictionaryEntry(
            word=headword,
            phonetic=phonetic or "",
            translation_lines=[line.strip() for line in (translation or "").splitlines() if line.strip()],
            definition_lines=[line.strip() for line in (definition or "").splitlines() if line.strip()],
            tags=_tags_for(collins or 0, oxford or 0, tag),
            base_form=base_form,
        )
