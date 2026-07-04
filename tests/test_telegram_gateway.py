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


if __name__ == "__main__":
    unittest.main()
