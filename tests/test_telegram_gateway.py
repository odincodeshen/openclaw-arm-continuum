import unittest

import openclaw_telegram_gateway as gateway


class DocumentDirectoryCaptionTest(unittest.TestCase):
    def test_tracker_caption_routes_to_tracker(self) -> None:
        directory = gateway.document_directory("/tracker")
        self.assertEqual(directory.parts[-2:], ("tracker", "telegram"))

    def test_mem_caption_routes_to_tracker(self) -> None:
        # Regression guard: README documents /mem as a tracker-memory
        # caption shortcut alongside /tracker, but document_directory()
        # used to only recognize /tracker and silently sent /mem uploads
        # to knowledge instead.
        directory = gateway.document_directory("/mem")
        self.assertEqual(directory.parts[-2:], ("tracker", "telegram"))

    def test_rag_caption_routes_to_knowledge(self) -> None:
        directory = gateway.document_directory("/rag")
        self.assertEqual(directory.parts[-2:], ("knowledge", "telegram"))

    def test_knowledge_caption_routes_to_knowledge(self) -> None:
        directory = gateway.document_directory("/knowledge")
        self.assertEqual(directory.parts[-2:], ("knowledge", "telegram"))

    def test_no_caption_defaults_to_knowledge(self) -> None:
        directory = gateway.document_directory("")
        self.assertEqual(directory.parts[-2:], ("knowledge", "telegram"))

    def test_caption_matching_is_case_insensitive(self) -> None:
        directory = gateway.document_directory("/MEM")
        self.assertEqual(directory.parts[-2:], ("tracker", "telegram"))


class SanitizeFilenameTest(unittest.TestCase):
    def test_normal_filename_passes_through(self) -> None:
        self.assertEqual(gateway.sanitize_filename("report.pdf", "fallback.bin"), "report.pdf")

    def test_path_traversal_is_reduced_to_basename(self) -> None:
        # Path(name).name strips any directory components first, so a
        # traversal attempt can only ever affect the basename, never escape
        # the target directory.
        self.assertEqual(gateway.sanitize_filename("../../etc/passwd", "fallback.bin"), "passwd")

    def test_special_characters_are_replaced_with_dash(self) -> None:
        self.assertEqual(gateway.sanitize_filename("my report (draft).pdf", "fallback.bin"), "my-report-draft-.pdf")

    def test_all_invalid_characters_falls_back(self) -> None:
        self.assertEqual(gateway.sanitize_filename("???", "fallback.bin"), "fallback.bin")

    def test_leading_and_trailing_separators_are_stripped(self) -> None:
        self.assertEqual(gateway.sanitize_filename("...name...", "fallback.bin"), "name")

    def test_non_ascii_name_without_ascii_characters_falls_back_to_extension_only(self) -> None:
        # SAFE_FILENAME only allows A-Za-z0-9._-, so a purely CJK stem is
        # replaced entirely and only the (also-ASCII) extension survives.
        self.assertEqual(gateway.sanitize_filename("中文檔名.pdf", "fallback.bin"), "pdf")


class ParseDocUrlArgsTest(unittest.TestCase):
    def test_url_only_defaults_to_knowledge(self) -> None:
        url, kind = gateway.parse_doc_url_args("https://docs.google.com/document/d/abc/edit")
        self.assertEqual(url, "https://docs.google.com/document/d/abc/edit")
        self.assertEqual(kind, "knowledge")

    def test_tracker_suffix_routes_to_tracker(self) -> None:
        _, kind = gateway.parse_doc_url_args("https://docs.google.com/document/d/abc/edit tracker")
        self.assertEqual(kind, "tracker")

    def test_mem_and_memory_aliases_route_to_tracker(self) -> None:
        for alias in ("mem", "memory", "MEM"):
            with self.subTest(alias=alias):
                _, kind = gateway.parse_doc_url_args(f"https://docs.google.com/document/d/abc/edit {alias}")
                self.assertEqual(kind, "tracker")

    def test_empty_input_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            gateway.parse_doc_url_args("")

    def test_whitespace_only_input_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            gateway.parse_doc_url_args("   ")


class AckMessageTest(unittest.TestCase):
    def setUp(self) -> None:
        # ack_message() reads the module-level agent_registry directly to
        # stay in sync with real routing. Swap in a deterministic registry
        # built from the real app/skills.json (no network calls needed for
        # routing decisions) so this test doesn't depend on whether
        # Qdrant/vLLM happen to be reachable in this environment -- when
        # they are not, SkillRouter silently drops memory_write/rag_retrieve
        # entirely, which would otherwise make these tests flaky.
        from tests.test_routing_integration import REPO_SKILLS_JSON, build_real_agent_registry
        from tests.support import build_settings

        self._original_registry = gateway.agent_registry
        settings = build_settings(skills_config_path=REPO_SKILLS_JSON)
        gateway.agent_registry = build_real_agent_registry(settings)
        self.addCleanup(self._restore_registry)

    def _restore_registry(self) -> None:
        gateway.agent_registry = self._original_registry

    def test_mem_command_acknowledges_memory_write(self) -> None:
        self.assertIn("memory", gateway.ack_message("/mem buy milk"))

    def test_remember_colon_form_acknowledges_memory_write(self) -> None:
        self.assertIn("memory", gateway.ack_message("remember: buy milk"))

    def test_rag_command_acknowledges_rag_lookup(self) -> None:
        self.assertIn("knowledge base", gateway.ack_message("/rag what did I save"))

    def test_search_command_acknowledges_web_search(self) -> None:
        self.assertIn("searching the web", gateway.ack_message("/search latest Arm news"))

    def test_natural_language_weather_query_acknowledges_weather(self) -> None:
        self.assertIn("weather", gateway.ack_message("英國明天天氣如何"))

    def test_unmatched_text_gets_generic_acknowledgement(self) -> None:
        self.assertIn("local reasoning model", gateway.ack_message("跟我聊聊你最近好嗎"))

    def test_ambiguous_keyword_overlap_matches_real_routing(self) -> None:
        # Regression guard: ack_message() used to hand-roll its own
        # keyword-priority list (search keywords checked before weather
        # keywords), which disagreed with the real skill order (weather is
        # checked before web_search), so this exact text acknowledged "web
        # search" while actually being routed to the weather agent.
        self.assertIn("weather", gateway.ack_message("查詢一下今天天氣如何"))


if __name__ == "__main__":
    unittest.main()
