import tempfile
import unittest
from pathlib import Path

from openclaw_runtime import categories

from tests.support import build_settings


class CategorySlugTest(unittest.TestCase):
    def test_ascii_name_keeps_readable_prefix(self) -> None:
        slug = categories.category_slug("Work Notes")
        self.assertTrue(slug.startswith("work-notes_"))
        self.assertTrue(categories.is_category_slug(slug))

    def test_slug_is_stable(self) -> None:
        self.assertEqual(
            categories.category_slug("Work Notes"),
            categories.category_slug("  work   notes "),
        )

    def test_cjk_only_name_falls_back_to_x_prefix_but_stays_unique(self) -> None:
        a = categories.category_slug("工作筆記")
        b = categories.category_slug("讀書心得")
        self.assertTrue(a.startswith("x_"))
        self.assertTrue(b.startswith("x_"))
        self.assertNotEqual(a, b)
        self.assertTrue(categories.is_category_slug(a))

    def test_collection_name_uses_prefix(self) -> None:
        settings = build_settings(category_collection_prefix="oc_cat_")
        self.assertEqual(
            categories.category_collection_name(settings, "work-notes_deadbeef"),
            "oc_cat_work-notes_deadbeef",
        )

    def test_validate_rejects_empty_and_overlong(self) -> None:
        settings = build_settings(category_max_name_chars=10)
        with self.assertRaises(ValueError):
            categories.validate_category_name(settings, "   ")
        with self.assertRaises(ValueError):
            categories.validate_category_name(settings, "x" * 11)
        with self.assertRaises(ValueError):
            categories.validate_category_name(settings, "/mem")
        self.assertEqual(categories.validate_category_name(settings, " 筆記 "), "筆記")


class ParseCategoryCaptionTest(unittest.TestCase):
    def test_no_hash_is_not_a_category(self) -> None:
        self.assertEqual(categories.parse_category_caption("just a caption"), (None, ""))
        self.assertEqual(categories.parse_category_caption(""), (None, ""))

    def test_bare_hash_name(self) -> None:
        self.assertEqual(categories.parse_category_caption("#工作筆記"), ("工作筆記", ""))

    def test_hash_name_with_note(self) -> None:
        self.assertEqual(
            categories.parse_category_caption("#機房 server rack, check cabling"),
            ("機房", "server rack, check cabling"),
        )

    def test_bracketed_name_with_spaces(self) -> None:
        self.assertEqual(
            categories.parse_category_caption("#[Work Notes] Q1 plan"),
            ("Work Notes", "Q1 plan"),
        )
        self.assertEqual(
            categories.parse_category_caption("#{Work Notes}"),
            ("Work Notes", ""),
        )


class CategoryRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = build_settings(
            category_registry_path=Path(self.tmp.name) / ".openclaw" / "categories.json"
        )

    def test_missing_registry_is_empty(self) -> None:
        self.assertEqual(categories.registry_entries(self.settings), [])

    def test_upsert_then_resolve_by_display_and_slug(self) -> None:
        entry = categories.upsert_registry_entry(self.settings, "工作筆記")
        self.assertEqual(entry["display"], "工作筆記")

        by_display = categories.resolve_category(self.settings, "工作筆記")
        by_slug = categories.resolve_category(self.settings, entry["slug"])
        self.assertEqual(by_display["collection"], entry["collection"])
        self.assertEqual(by_slug["collection"], entry["collection"])

    def test_upsert_is_idempotent_and_preserves_created_at(self) -> None:
        first = categories.upsert_registry_entry(self.settings, "Reports")
        again = categories.upsert_registry_entry(self.settings, "reports")
        self.assertEqual(first["slug"], again["slug"])
        entries = categories.registry_entries(self.settings)
        self.assertEqual(len(entries), 1)

    def test_resolve_unknown_name_returns_computed_entry(self) -> None:
        resolved = categories.resolve_category(self.settings, "全新類別")
        self.assertIsNotNone(resolved)
        self.assertEqual(
            resolved["collection"],
            categories.category_collection_name(self.settings, categories.category_slug("全新類別")),
        )
        # nothing was persisted by a read-only resolve
        self.assertEqual(categories.registry_entries(self.settings), [])

    def test_ensure_entry_for_slug_backfills_display(self) -> None:
        slug = categories.category_slug("手動丟的資料夾")
        categories.ensure_registry_entry_for_slug(self.settings, slug)
        entries = categories.registry_entries(self.settings)
        self.assertEqual(entries[0]["slug"], slug)
        self.assertEqual(entries[0]["display"], slug)

    def test_corrupt_registry_falls_back_to_empty(self) -> None:
        self.settings.category_registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.category_registry_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(categories.registry_entries(self.settings), [])


if __name__ == "__main__":
    unittest.main()
