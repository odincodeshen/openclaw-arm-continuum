import csv
import tempfile
import unittest
from pathlib import Path

from openclaw_runtime.dictionary import LocalDictionary, build_dictionary_db, normalize_query

ECDICT_HEADER = [
    "word", "phonetic", "definition", "translation", "pos", "collins", "oxford",
    "tag", "bnc", "frq", "exchange", "detail", "audio",
]

ROWS = [
    {"word": "resilient", "phonetic": "rɪ'zɪliənt", "definition": "a. recovering quickly",
     "translation": "a. 有弹性的\\n能复原的", "collins": "3", "oxford": "1", "tag": "cet6 ielts ky"},
    {"word": "run", "phonetic": "rʌn", "translation": "v. 跑", "exchange": "p:ran/d:run"},
    {"word": "ran", "phonetic": "", "translation": "", "definition": "", "exchange": "0:run/1:p"},
    {"word": "ranked", "phonetic": "", "translation": "a. 排名的", "exchange": "0:rank/1:p"},
    {"word": "Paris", "translation": "n. 巴黎"},
    {"word": "paris", "translation": "n. 巴黎(小寫)"},
    {"word": "emptyword"},
]


def _write_csv(path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ECDICT_HEADER)
        writer.writeheader()
        for row in ROWS:
            writer.writerow({key: row.get(key, "") for key in ECDICT_HEADER})


class BuildAndLookupTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        csv_path = Path(tmp.name) / "ecdict.csv"
        _write_csv(csv_path)
        self.db_path = Path(tmp.name) / "dictionary" / "ecdict.sqlite"
        # Stand-in for OpenCC: mark every converted string so the test can
        # see the conversion really ran on translations.
        self.count = build_dictionary_db(csv_path, self.db_path, lambda text: text.replace("弹", "彈"))
        self.dictionary = LocalDictionary(self.db_path)

    def test_skips_rows_with_no_meaning_and_counts_the_rest(self) -> None:
        self.assertEqual(self.count, 5)  # "ran" and "emptyword" have neither translation nor definition
        self.assertTrue(self.db_path.exists())
        self.assertFalse(self.db_path.with_suffix(".sqlite.tmp").exists())

    def test_lookup_returns_converted_multiline_translation_and_tags(self) -> None:
        entry = self.dictionary.lookup("resilient")
        self.assertEqual(entry.word, "resilient")
        self.assertEqual(entry.phonetic, "rɪ'zɪliənt")
        self.assertEqual(entry.translation_lines, ["a. 有彈性的", "能复原的"])
        self.assertEqual(entry.tags, ["IELTS", "CET-6", "Oxford 3000", "Collins 3/5"])
        self.assertEqual(entry.base_form, "")

    def test_punctuation_and_case_are_ignored(self) -> None:
        self.assertEqual(self.dictionary.lookup('"Resilient,"').word, "resilient")

    def test_exact_case_wins_then_lowercase_headword_is_preferred(self) -> None:
        self.assertEqual(self.dictionary.lookup("Paris").translation_lines, ["n. 巴黎"])
        self.assertEqual(self.dictionary.lookup("PARIS").translation_lines, ["n. 巴黎(小寫)"])

    def test_inflected_form_keeps_its_own_meaning_and_notes_the_base(self) -> None:
        entry = self.dictionary.lookup("ranked")
        self.assertEqual(entry.translation_lines, ["a. 排名的"])
        self.assertEqual(entry.base_form, "rank")

    def test_unknown_word_returns_none(self) -> None:
        self.assertIsNone(self.dictionary.lookup("xyzzyq"))
        self.assertIsNone(self.dictionary.lookup("   "))

    def test_missing_dictionary_file_returns_none(self) -> None:
        missing = LocalDictionary(self.db_path.parent / "nope.sqlite")
        self.assertFalse(missing.available())
        self.assertIsNone(missing.lookup("resilient"))


class InflectedFormWithoutMeaningTest(unittest.TestCase):
    def test_borrows_the_base_words_meaning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "ecdict.csv"
            with open(csv_path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=ECDICT_HEADER)
                writer.writeheader()
                writer.writerow({**dict.fromkeys(ECDICT_HEADER, ""), "word": "run", "translation": "v. 跑"})
                # an inflected form that only has an English definition, no Chinese
                writer.writerow(
                    {**dict.fromkeys(ECDICT_HEADER, ""), "word": "ran", "definition": "past of run",
                     "exchange": "0:run/1:p"}
                )
            db_path = Path(tmp) / "d.sqlite"
            build_dictionary_db(csv_path, db_path, lambda text: text)
            entry = LocalDictionary(db_path).lookup("ran")
        self.assertEqual(entry.translation_lines, ["v. 跑"])
        self.assertEqual(entry.base_form, "run")


class NormalizeQueryTest(unittest.TestCase):
    def test_strips_quotes_punctuation_and_collapses_spaces(self) -> None:
        self.assertEqual(normalize_query("  “take   off”! "), "take off")

    def test_caps_length(self) -> None:
        self.assertEqual(len(normalize_query("a" * 500)), 64)


if __name__ == "__main__":
    unittest.main()
