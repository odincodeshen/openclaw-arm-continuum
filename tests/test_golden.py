"""L1 contract / golden tests.

Pins user-facing contracts that a unit test of a single function would not
catch: the bot command menu, its consistency with /help, fixed slug vectors,
the model-paused message, and the v1.6 "English-only user-facing strings"
guarantee. Intentional changes update the expected values here on purpose.
"""

import re
import unittest

import openclaw_telegram_gateway as gateway
from openclaw_runtime.categories import category_slug

# Hiragana/Katakana, CJK Unified (+ Ext A), and fullwidth forms.
_CJK = re.compile("[぀-ヿ㐀-鿿＀-￯]")

EXPECTED_MENU = [
    "help", "mem", "rag", "doc", "cat", "search",
    "cron", "new", "agents", "tasks", "review", "start",
]


class BotMenuContractTest(unittest.TestCase):
    def _captured_menu(self):
        captured = {}
        orig = gateway.telegram
        gateway.telegram = lambda method, payload=None, timeout=60: captured.setdefault(method, payload) or {}
        try:
            gateway.setup_bot_commands()
        finally:
            gateway.telegram = orig
        return captured["setMyCommands"]["commands"]

    def test_menu_command_list_is_stable(self):
        self.assertEqual([c["command"] for c in self._captured_menu()], EXPECTED_MENU)

    def test_menu_descriptions_are_english(self):
        for entry in self._captured_menu():
            self.assertIsNone(_CJK.search(entry["description"]), entry)

    def test_every_actionable_menu_command_is_documented_in_help(self):
        for cmd in EXPECTED_MENU:
            if cmd == "start":  # alias of /help, not separately documented
                continue
            self.assertIn(f"/{cmd}", gateway.HELP_TEXT, f"/{cmd} missing from HELP_TEXT")


class MessageContractTest(unittest.TestCase):
    def test_help_text_is_english(self):
        self.assertIsNone(_CJK.search(gateway.HELP_TEXT))

    def test_model_paused_message_names_the_fix_and_survivors(self):
        msg = gateway.MODEL_PAUSED_MESSAGE
        self.assertIn("bin/openclawctl start model", msg)
        for survivor in ("/mem", "/cat", "/doc", "/cron", "/tasks"):
            self.assertIn(survivor, msg)

    def test_ack_messages_are_english(self):
        for text in list(gateway.ACK_MESSAGES.values()) + [gateway.DEFAULT_ACK_MESSAGE]:
            self.assertIsNone(_CJK.search(text), text)


class SlugVectorTest(unittest.TestCase):
    # Category slugs are persisted in Qdrant collection names and the on-disk
    # registry; changing the derivation silently orphans every existing index.
    VECTORS = {
        "投資": "x_05bd10df",
        "Work Notes": "work-notes_9436bb3b",
        "工作筆記": "x_4217b4d6",
        "AI 應用": "ai_b653cf75",
        "機房": "x_25f5d7c0",
    }

    def test_fixed_slug_vectors(self):
        for name, slug in self.VECTORS.items():
            self.assertEqual(category_slug(name), slug, name)

    def test_slug_is_whitespace_and_case_normalised(self):
        self.assertEqual(category_slug("  Work   Notes "), category_slug("work notes"))


if __name__ == "__main__":
    unittest.main()
