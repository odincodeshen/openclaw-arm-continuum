import tempfile
import unittest
from pathlib import Path

import openclaw_telegram_gateway as gateway
from openclaw_runtime import categories

from tests.support import build_settings


class CategoryGatewayTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.inbox = root / "inbox"
        self.inbox.mkdir()
        self.settings = build_settings(
            inbox_path=self.inbox,
            category_registry_path=self.inbox / ".openclaw" / "categories.json",
            category_collection_prefix="oc_cat_",
            category_pending_ttl_seconds=100,
        )
        self._orig_settings = gateway.settings
        gateway.settings = self.settings
        self.addCleanup(setattr, gateway, "settings", self._orig_settings)

        self.sent: list[tuple[int, str]] = []
        self._orig_send = gateway.send_message
        gateway.send_message = lambda chat_id, text: self.sent.append((chat_id, text))
        self.addCleanup(setattr, gateway, "send_message", self._orig_send)

        with gateway.PENDING_CATEGORY_LOCK:
            gateway.PENDING_CATEGORY.clear()
        self.addCleanup(gateway.PENDING_CATEGORY.clear)

        self.addCleanup(self._clear_media_group_buffer)

    def _clear_media_group_buffer(self) -> None:
        with gateway.MEDIA_GROUP_LOCK:
            for group in gateway.MEDIA_GROUP_BUFFER.values():
                timer = group.get("timer")
                if timer is not None:
                    timer.cancel()
            gateway.MEDIA_GROUP_BUFFER.clear()


class PendingStateMachineTest(CategoryGatewayTestBase):
    def test_set_and_pop_pending(self) -> None:
        gateway.set_pending_category(42, {"path": "/x/a.pdf", "kind": "document", "note": ""})
        self.assertTrue(gateway.has_pending_category(42))
        popped = gateway.pop_pending_category(42)
        self.assertEqual(len(popped["items"]), 1)
        self.assertFalse(gateway.has_pending_category(42))

    def test_resolve_pending_routes_items_to_category(self) -> None:
        captured = {}

        def fake_ingest(chat_id, items, entry):
            captured["chat_id"] = chat_id
            captured["items"] = items
            captured["entry"] = entry

        self._orig_run = gateway._run_category_ingest
        gateway._run_category_ingest = fake_ingest
        self.addCleanup(setattr, gateway, "_run_category_ingest", self._orig_run)

        gateway.set_pending_category(7, {"path": "/x/spec.pdf", "kind": "document", "note": ""})
        handled = gateway.resolve_pending_with_category(7, "工作筆記")

        # give the worker thread a moment
        for _ in range(50):
            if "entry" in captured:
                break
            import time

            time.sleep(0.01)

        self.assertTrue(handled)
        self.assertEqual(captured["entry"]["display"], "工作筆記")
        self.assertEqual(captured["items"][0]["path"], "/x/spec.pdf")
        self.assertFalse(gateway.has_pending_category(7))

    def test_resolve_pending_strips_hash_prefix_and_note_reused_from_caption_habit(self) -> None:
        # Regression: a user replying to "reply with a category name" with
        # "#trip <note>" (habit from the caption syntax) used to become the
        # literal category "#trip <note>" -- a second, unrelated category
        # from the plain "trip" one the caption path would produce.
        captured = {}

        def fake_ingest(chat_id, items, entry):
            captured["entry"] = entry

        self._orig_run = gateway._run_category_ingest
        gateway._run_category_ingest = fake_ingest
        self.addCleanup(setattr, gateway, "_run_category_ingest", self._orig_run)

        gateway.set_pending_category(11, {"path": "/x/train.jpg", "kind": "photo", "note": ""})
        handled = gateway.resolve_pending_with_category(11, "#trip 週一的火車幾點發個車")

        for _ in range(50):
            if "entry" in captured:
                break
            import time

            time.sleep(0.01)

        self.assertTrue(handled)
        self.assertEqual(captured["entry"]["display"], "trip")

    def test_resolve_pending_full_width_hash_also_strips(self) -> None:
        captured = {}

        def fake_ingest(chat_id, items, entry):
            captured["entry"] = entry

        self._orig_run = gateway._run_category_ingest
        gateway._run_category_ingest = fake_ingest
        self.addCleanup(setattr, gateway, "_run_category_ingest", self._orig_run)

        gateway.set_pending_category(12, {"path": "/x/a.jpg", "kind": "photo", "note": ""})
        gateway.resolve_pending_with_category(12, "＃trip")

        for _ in range(50):
            if "entry" in captured:
                break
            import time

            time.sleep(0.01)

        self.assertEqual(captured["entry"]["display"], "trip")

    def test_resolve_pending_rejects_bad_name_and_keeps_items(self) -> None:
        gateway.set_pending_category(9, {"path": "/x/spec.pdf", "kind": "document", "note": ""})
        handled = gateway.resolve_pending_with_category(9, "/mem")
        self.assertTrue(handled)
        self.assertTrue(gateway.has_pending_category(9))
        self.assertTrue(any("will not work" in text for _, text in self.sent))

    def test_no_pending_returns_false(self) -> None:
        self.assertFalse(gateway.resolve_pending_with_category(1, "任何類別"))

    def test_expired_pending_document_falls_back_to_knowledge(self) -> None:
        staged_dir = self.inbox / ".staging" / "telegram"
        staged_dir.mkdir(parents=True)
        staged = staged_dir / "old.pdf"
        staged.write_bytes(b"%PDF-1.4")

        gateway.set_pending_category(5, {"path": str(staged), "kind": "document", "note": ""})
        with gateway.PENDING_CATEGORY_LOCK:
            gateway.PENDING_CATEGORY[5]["updated_at"] = 0  # force-expire

        gateway.sweep_expired_pending()

        self.assertFalse(gateway.has_pending_category(5))
        self.assertFalse(staged.exists())
        moved = list((self.inbox / "knowledge" / "telegram").glob("*.pdf"))
        self.assertEqual(len(moved), 1)


class CategoryIngestTest(CategoryGatewayTestBase):
    def test_ingest_document_moves_file_and_writes_sidecar(self) -> None:
        src_dir = self.inbox / ".staging" / "telegram"
        src_dir.mkdir(parents=True)
        src = src_dir / "20260101-report.md"
        src.write_text("quarterly report body", encoding="utf-8")

        entry = categories.upsert_registry_entry(self.settings, "報告")
        target = gateway.ingest_document_into_category(src, entry, note="Q1", original_name="第一季報告.pdf")

        self.assertTrue(target.exists())
        self.assertFalse(src.exists())
        self.assertEqual(
            target.parent, self.inbox / "categories" / entry["slug"]
        )
        import json as _json

        sidecar = _json.loads(target.with_name(target.name + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["category"], "報告")
        self.assertEqual(sidecar["original_file_name"], "第一季報告.pdf")

    def test_write_upload_meta_records_original_name(self) -> None:
        d = self.inbox / "knowledge" / "telegram"
        d.mkdir(parents=True)
        stored = d / "20260906-100000-pdf.pdf"
        stored.write_bytes(b"%PDF-1.4")
        gateway.write_upload_meta(stored, "我的中文檔名.pdf")
        import json as _json

        data = _json.loads(stored.with_name(stored.name + ".meta.json").read_text(encoding="utf-8"))
        self.assertEqual(data["original_file_name"], "我的中文檔名.pdf")

    def test_ingest_image_uses_vision_client_and_writes_markdown(self) -> None:
        media = self.inbox / "media" / "telegram"
        media.mkdir(parents=True)
        img = media / "rack.jpg"
        img.write_bytes(b"\xff\xd8\xff\xd9")

        class FakeVision:
            def describe_image(self, path, instruction=None, *, max_tokens=None):
                return "A server rack with three switches. Labels: SW1, SW2, SW3."

        self._orig_vision = gateway.vision
        gateway.vision = FakeVision()
        self.addCleanup(setattr, gateway, "vision", self._orig_vision)

        entry = categories.upsert_registry_entry(self.settings, "機房")
        doc = gateway.ingest_image_into_category(1, img, entry, note="機櫃照")

        self.assertTrue(doc.name.endswith(".md"))
        body = doc.read_text(encoding="utf-8")
        self.assertIn("A server rack", body)
        sidecar_data = doc.with_name(doc.name + ".meta.json").read_text(encoding="utf-8")
        self.assertIn("image_path", sidecar_data)
        self.assertTrue((self.inbox / "categories" / entry["slug"] / "media" / "rack.jpg").exists())


class ProcessImageMessageTest(CategoryGatewayTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._set_vision_enabled(True)
        self.img = self.inbox / "pic.jpg"
        self.img.write_bytes(b"\xff\xd8\xff\xd9")

    def _set_vision_enabled(self, value: bool) -> None:
        import dataclasses

        self.settings = dataclasses.replace(self.settings, vision_enabled=value)
        gateway.settings = self.settings

    def _install_vision(self, fake) -> None:
        orig = gateway.vision
        gateway.vision = fake
        self.addCleanup(setattr, gateway, "vision", orig)

    def test_routes_through_vision_client_with_caption(self) -> None:
        calls = []

        class FakeVision:
            endpoint_id = "vision"

            def describe_image(self, path, instruction=None, *, max_tokens=None):
                calls.append((path, instruction, max_tokens))
                return "A network switch, model X."

        self._install_vision(FakeVision())
        gateway.process_image_message(5, self.img, "what model is this?")

        self.assertEqual(calls[0][1], "what model is this?")
        self.assertEqual(calls[0][2], self.settings.vision_max_tokens)
        self.assertTrue(any("model X" in text for _, text in self.sent))

    def test_disabled_vision_skips_call(self) -> None:
        self._set_vision_enabled(False)
        self._install_vision(object())  # any attribute access would raise
        gateway.process_image_message(5, self.img, "")
        self.assertTrue(any("OPENCLAW_VISION_ENABLED=false" in t for _, t in self.sent))

    def test_vision_error_points_at_setup_docs(self) -> None:
        class FakeVision:
            endpoint_id = "vision"

            def describe_image(self, path, instruction=None, *, max_tokens=None):
                raise gateway.VisionError("endpoint down")

        self._install_vision(FakeVision())
        gateway.process_image_message(5, self.img, "")
        self.assertTrue(any("vision_smoke.py" in t for _, t in self.sent))


class MediaGroupBufferTest(CategoryGatewayTestBase):
    """Telegram attaches a multi-photo album's caption to only one message
    in the group. Regression coverage for the bug this caused: every photo
    was routed by its OWN (mostly empty) caption, splitting one #trip album
    across a real "trip" category and a garbage category made from whatever
    text the user sent next."""

    def setUp(self) -> None:
        super().setUp()
        self.paths = []
        for name in ("first.jpg", "second.jpg"):
            path = self.inbox / name
            path.write_bytes(b"\xff\xd8\xff\xd9")
            self.paths.append(path)

    def _install_run_category_ingest(self):
        captured = {}

        def fake_ingest(chat_id, items, entry):
            captured["chat_id"] = chat_id
            captured["items"] = items
            captured["entry"] = entry

        orig = gateway._run_category_ingest
        gateway._run_category_ingest = fake_ingest
        self.addCleanup(setattr, gateway, "_run_category_ingest", orig)
        return captured

    def test_buffer_accumulates_paths_from_both_calls(self) -> None:
        gateway._buffer_media_group_photo(1, "group-1", self.paths[0], "")
        gateway._buffer_media_group_photo(1, "group-1", self.paths[1], "#trip 週末小旅行")

        with gateway.MEDIA_GROUP_LOCK:
            group = gateway.MEDIA_GROUP_BUFFER[(1, "group-1")]
        self.assertEqual(group["paths"], [self.paths[0], self.paths[1]])
        self.assertEqual(group["caption"], "#trip 週末小旅行")

    def test_buffer_keeps_caption_seen_on_an_earlier_call(self) -> None:
        # Order isn't guaranteed -- the caption can arrive on the FIRST
        # message in the group just as easily as a later one.
        gateway._buffer_media_group_photo(1, "group-1", self.paths[0], "#trip 週末小旅行")
        gateway._buffer_media_group_photo(1, "group-1", self.paths[1], "")

        with gateway.MEDIA_GROUP_LOCK:
            group = gateway.MEDIA_GROUP_BUFFER[(1, "group-1")]
        self.assertEqual(group["caption"], "#trip 週末小旅行")

    def test_flush_routes_every_photo_to_the_shared_caption_category(self) -> None:
        captured = self._install_run_category_ingest()
        with gateway.MEDIA_GROUP_LOCK:
            gateway.MEDIA_GROUP_BUFFER[(1, "group-1")] = {
                "paths": list(self.paths),
                "caption": "#trip 週末小旅行",
            }

        gateway._flush_media_group(1, "group-1")

        self.assertEqual(captured["entry"]["display"], "trip")
        self.assertEqual([item["path"] for item in captured["items"]], [str(p) for p in self.paths])
        self.assertEqual(captured["items"][0]["note"], "週末小旅行")
        self.assertTrue(any('into category "trip"' in t for _, t in self.sent))
        with gateway.MEDIA_GROUP_LOCK:
            self.assertNotIn((1, "group-1"), gateway.MEDIA_GROUP_BUFFER)

    def test_flush_with_no_caption_anywhere_buffers_all_as_one_pending_batch(self) -> None:
        with gateway.MEDIA_GROUP_LOCK:
            gateway.MEDIA_GROUP_BUFFER[(1, "group-1")] = {"paths": list(self.paths), "caption": ""}

        gateway._flush_media_group(1, "group-1")

        pending = gateway.pop_pending_category(1)
        self.assertEqual(len(pending["items"]), 2)
        self.assertEqual({item["path"] for item in pending["items"]}, {str(p) for p in self.paths})
        # One combined prompt, not one per photo.
        prompts = [t for _, t in self.sent if "category name" in t]
        self.assertEqual(len(prompts), 1)
        self.assertIn("these 2 images", prompts[0])

    def test_flush_of_unknown_group_is_a_noop(self) -> None:
        gateway._flush_media_group(1, "never-buffered")
        self.assertEqual(self.sent, [])

    def test_handle_photo_message_buffers_instead_of_routing_immediately_when_grouped(self) -> None:
        self._orig_file_info = gateway.telegram_file_info
        gateway.telegram_file_info = lambda file_id: {"file_path": "photos/file.jpg"}
        self.addCleanup(setattr, gateway, "telegram_file_info", self._orig_file_info)

        self._orig_download = gateway.download_telegram_file
        gateway.download_telegram_file = lambda file_id, destination: (self.paths[0], 4)
        self.addCleanup(setattr, gateway, "download_telegram_file", self._orig_download)

        self._orig_process = gateway.process_image_message
        gateway.process_image_message = lambda *a, **k: None
        self.addCleanup(setattr, gateway, "process_image_message", self._orig_process)

        called_route = []
        self._orig_route = gateway._route_image_to_category
        gateway._route_image_to_category = lambda *a, **k: called_route.append(a)
        self.addCleanup(setattr, gateway, "_route_image_to_category", self._orig_route)

        message = {"photo": [{"file_id": "f1"}], "caption": "#trip", "media_group_id": "grp-42"}
        handled = gateway.handle_photo_message(1, message)

        self.assertTrue(handled)
        self.assertEqual(called_route, [])  # buffered, not routed immediately
        with gateway.MEDIA_GROUP_LOCK:
            group = gateway.MEDIA_GROUP_BUFFER[(1, "grp-42")]
        self.assertEqual(group["caption"], "#trip")
        group["timer"].cancel()

    def test_handle_photo_message_without_media_group_id_routes_immediately(self) -> None:
        self._orig_file_info = gateway.telegram_file_info
        gateway.telegram_file_info = lambda file_id: {"file_path": "photos/file.jpg"}
        self.addCleanup(setattr, gateway, "telegram_file_info", self._orig_file_info)

        self._orig_download = gateway.download_telegram_file
        gateway.download_telegram_file = lambda file_id, destination: (self.paths[0], 4)
        self.addCleanup(setattr, gateway, "download_telegram_file", self._orig_download)

        self._orig_process = gateway.process_image_message
        gateway.process_image_message = lambda *a, **k: None
        self.addCleanup(setattr, gateway, "process_image_message", self._orig_process)

        called_route = []
        self._orig_route = gateway._route_image_to_category
        gateway._route_image_to_category = lambda *a, **k: called_route.append(a)
        self.addCleanup(setattr, gateway, "_route_image_to_category", self._orig_route)

        message = {"photo": [{"file_id": "f1"}], "caption": "#trip"}
        gateway.handle_photo_message(1, message)

        self.assertEqual(len(called_route), 1)
        with gateway.MEDIA_GROUP_LOCK:
            self.assertEqual(gateway.MEDIA_GROUP_BUFFER, {})


class CategoryCommandTest(CategoryGatewayTestBase):
    def test_cat_list_empty(self) -> None:
        self.assertTrue(gateway.handle_category_command(100, "/cat list"))
        self.assertTrue(any("No categories yet" in t for _, t in self.sent))

    def test_cat_list_after_upsert(self) -> None:
        categories.upsert_registry_entry(self.settings, "工作筆記")
        gateway.handle_category_command(100, "/cat list")
        self.assertTrue(any("工作筆記" in t for _, t in self.sent))

    def test_non_cat_text_ignored(self) -> None:
        self.assertFalse(gateway.handle_category_command(100, "hello"))


class CategoryRenameCommandTest(CategoryGatewayTestBase):
    def test_rename_updates_display_and_keeps_collection(self) -> None:
        entry = categories.upsert_registry_entry(self.settings, "trip")
        self.assertTrue(gateway.handle_category_command(100, "/cat rename trip 旅行"))
        self.assertTrue(any('Renamed "trip" to "旅行"' in t for _, t in self.sent))
        updated = categories.resolve_category(self.settings, "旅行")
        self.assertEqual(updated["collection"], entry["collection"])

    def test_rename_with_bracketed_new_name(self) -> None:
        categories.upsert_registry_entry(self.settings, "trip")
        gateway.handle_category_command(100, "/cat rename trip [Work Notes]")
        self.assertTrue(any('to "Work Notes"' in t for _, t in self.sent))

    def test_rename_unknown_source(self) -> None:
        gateway.handle_category_command(100, "/cat rename ghost new-name")
        self.assertTrue(any('No category named "ghost"' in t for _, t in self.sent))

    def test_rename_missing_args_shows_usage(self) -> None:
        gateway.handle_category_command(100, "/cat rename trip")
        self.assertTrue(any("Usage: /cat rename" in t for _, t in self.sent))

    def test_rename_onto_a_different_existing_category_is_rejected(self) -> None:
        categories.upsert_registry_entry(self.settings, "trip")
        categories.upsert_registry_entry(self.settings, "投資")
        gateway.handle_category_command(100, "/cat rename trip 投資")
        self.assertTrue(any("already a different category" in t for _, t in self.sent))
        self.assertTrue(any("/cat merge trip 投資" in t for _, t in self.sent))
        # neither category was touched
        self.assertTrue(categories.resolve_category(self.settings, "trip")["known"])
        self.assertEqual(categories.resolve_category(self.settings, "投資")["display"], "投資")


class CategoryMergeCommandTest(CategoryGatewayTestBase):
    class FakeQdrant:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def points_count(self, collection: str):
            return None

        def delete_collection(self, collection: str) -> None:
            self.deleted.append(collection)

    def setUp(self) -> None:
        super().setUp()
        self.fake_qdrant = self.FakeQdrant()
        self._orig_qdrant = gateway.qdrant
        gateway.qdrant = self.fake_qdrant
        self.addCleanup(setattr, gateway, "qdrant", self._orig_qdrant)

    def _wait_for_send(self, needle: str, timeout: float = 2.0) -> bool:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(needle in t for _, t in self.sent):
                return True
            time.sleep(0.01)
        return False

    def _seed_file(self, category_name: str, filename: str, body: str):
        src_dir = self.inbox / ".staging" / "telegram"
        src_dir.mkdir(parents=True, exist_ok=True)
        src = src_dir / filename
        src.write_text(body, encoding="utf-8")
        entry = categories.upsert_registry_entry(self.settings, category_name)
        return entry, gateway.ingest_document_into_category(src, entry, note="", original_name=filename)

    def test_merge_moves_files_deletes_source_collection_and_registry_entry(self) -> None:
        from_entry, from_doc = self._seed_file("trip", "train.md", "train ticket body")
        into_entry, _ = self._seed_file("investing", "note.md", "investing note body")

        self.assertTrue(gateway.handle_category_command(100, "/cat merge trip investing"))
        self.assertTrue(self._wait_for_send("Merged"))

        self.assertFalse(from_doc.exists())
        moved = list((self.inbox / "categories" / into_entry["slug"]).glob(f"*{from_doc.name}"))
        self.assertEqual(len(moved), 1)
        sidecar_data = moved[0].with_name(moved[0].name + ".meta.json").read_text(encoding="utf-8")
        self.assertIn('"investing"', sidecar_data)
        self.assertIn(into_entry["slug"], sidecar_data)

        self.assertFalse((self.inbox / "categories" / from_entry["slug"]).exists())
        self.assertFalse(categories.resolve_category(self.settings, "trip")["known"])
        self.assertEqual(self.fake_qdrant.deleted, [from_entry["collection"]])

    def test_merge_unknown_source_or_target(self) -> None:
        categories.upsert_registry_entry(self.settings, "trip")
        gateway.handle_category_command(100, "/cat merge ghost trip")
        self.assertTrue(any('No category named "ghost"' in t for _, t in self.sent))

        self.sent.clear()
        gateway.handle_category_command(100, "/cat merge trip ghost2")
        self.assertTrue(any('No category named "ghost2"' in t for _, t in self.sent))

    def test_merge_same_category_is_rejected(self) -> None:
        categories.upsert_registry_entry(self.settings, "trip")
        gateway.handle_category_command(100, "/cat merge trip trip")
        self.assertTrue(any("Source and target are the same" in t for _, t in self.sent))
        self.assertEqual(self.fake_qdrant.deleted, [])

    def test_merge_missing_args_shows_usage(self) -> None:
        gateway.handle_category_command(100, "/cat merge trip")
        self.assertTrue(any("Usage: /cat merge" in t for _, t in self.sent))

    def test_merge_with_no_files_still_cleans_up_source(self) -> None:
        from_entry = categories.upsert_registry_entry(self.settings, "empty-cat")
        categories.upsert_registry_entry(self.settings, "target-cat")
        gateway.handle_category_command(100, "/cat merge empty-cat target-cat")
        self.assertTrue(self._wait_for_send("had no files to move"))
        self.assertFalse(categories.resolve_category(self.settings, "empty-cat")["known"])
        self.assertEqual(self.fake_qdrant.deleted, [from_entry["collection"]])
        self.assertTrue(categories.resolve_category(self.settings, "target-cat")["known"])


if __name__ == "__main__":
    unittest.main()
