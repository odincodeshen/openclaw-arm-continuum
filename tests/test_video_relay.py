import unittest
from unittest.mock import patch

import openclaw_telegram_gateway as gateway

from tests.support import build_settings


class GatewaySettingsTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_settings = gateway.settings
        self.addCleanup(setattr, gateway, "settings", self._orig_settings)

        self.sent: list[tuple[int, str]] = []
        self._orig_send = gateway.send_message
        gateway.send_message = lambda chat_id, text: self.sent.append((chat_id, text))
        self.addCleanup(setattr, gateway, "send_message", self._orig_send)

    def use_settings(self, **overrides) -> None:
        gateway.settings = build_settings(**overrides)


class RelayVideoSummaryTest(GatewaySettingsTestBase):
    """Direct tests of _relay_video_summary -- the background-thread body
    that actually calls the Apps Script relay."""

    def setUp(self) -> None:
        super().setUp()
        self.use_settings(
            video_summary_relay_url="https://script.google.com/macros/s/xyz/exec",
            video_summary_relay_secret="",
            video_summary_relay_timeout_seconds=360,
        )

    @patch("openclaw_telegram_gateway.request_json")
    def test_success_sends_no_extra_message(self, request_json) -> None:
        request_json.return_value = {"ok": True}
        gateway._relay_video_summary(1, "https://youtu.be/abc123")
        self.assertEqual(self.sent, [])

    @patch("openclaw_telegram_gateway.request_json")
    def test_ok_false_sends_error_message(self, request_json) -> None:
        request_json.return_value = {"ok": False, "error": "bad json"}
        gateway._relay_video_summary(1, "https://youtu.be/abc123")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("bad json", self.sent[0][1])

    @patch("openclaw_telegram_gateway.request_json")
    def test_ok_false_with_no_error_field_uses_fallback_text(self, request_json) -> None:
        request_json.return_value = {"ok": False}
        gateway._relay_video_summary(1, "https://youtu.be/abc123")
        self.assertIn("unknown error", self.sent[0][1])

    @patch("openclaw_telegram_gateway.request_json")
    def test_network_exception_sends_error_message(self, request_json) -> None:
        request_json.side_effect = TimeoutError("timed out")
        gateway._relay_video_summary(1, "https://youtu.be/abc123")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("timed out", self.sent[0][1])

    @patch("openclaw_telegram_gateway.request_json")
    def test_payload_and_url_and_timeout_passed_through(self, request_json) -> None:
        request_json.return_value = {"ok": True}
        gateway._relay_video_summary(1, "https://youtu.be/abc123")
        args, kwargs = request_json.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://script.google.com/macros/s/xyz/exec")
        self.assertEqual(args[2], {"video_url": "https://youtu.be/abc123"})
        self.assertEqual(kwargs["timeout"], 360)

    @patch("openclaw_telegram_gateway.request_json")
    def test_secret_included_when_configured(self, request_json) -> None:
        self.use_settings(
            video_summary_relay_url="https://script.google.com/macros/s/xyz/exec",
            video_summary_relay_secret="s3cr3t",
            video_summary_relay_timeout_seconds=360,
        )
        request_json.return_value = {"ok": True}
        gateway._relay_video_summary(1, "https://youtu.be/abc123")
        payload = request_json.call_args.args[2]
        self.assertEqual(payload["secret"], "s3cr3t")

    @patch("openclaw_telegram_gateway.request_json")
    def test_secret_omitted_when_not_configured(self, request_json) -> None:
        request_json.return_value = {"ok": True}
        gateway._relay_video_summary(1, "https://youtu.be/abc123")
        payload = request_json.call_args.args[2]
        self.assertNotIn("secret", payload)


class HandleVideoLinkMessageTest(GatewaySettingsTestBase):
    """Dispatch-layer tests: threading.Thread is mocked so the background
    call itself is never actually run here -- that logic is covered by
    RelayVideoSummaryTest above."""

    def setUp(self) -> None:
        super().setUp()
        self.use_settings(
            video_summary_relay_url="https://script.google.com/macros/s/xyz/exec",
            video_summary_relay_secret="",
            video_summary_relay_timeout_seconds=360,
        )

    @patch("openclaw_telegram_gateway.threading.Thread")
    def test_relay_disabled_returns_false_and_sends_nothing(self, thread_cls) -> None:
        self.use_settings(video_summary_relay_url="", video_summary_relay_secret="")
        handled = gateway.handle_video_link_message(1, "https://youtu.be/abc123")
        self.assertFalse(handled)
        self.assertEqual(self.sent, [])
        thread_cls.assert_not_called()

    @patch("openclaw_telegram_gateway.threading.Thread")
    def test_non_youtube_url_returns_false(self, thread_cls) -> None:
        handled = gateway.handle_video_link_message(1, "https://example.com/article")
        self.assertFalse(handled)
        self.assertEqual(self.sent, [])
        thread_cls.assert_not_called()

    @patch("openclaw_telegram_gateway.threading.Thread")
    def test_mixed_text_with_link_returns_false(self, thread_cls) -> None:
        handled = gateway.handle_video_link_message(1, "看看這個 https://youtu.be/abc123 值得看嗎")
        self.assertFalse(handled)
        self.assertEqual(self.sent, [])
        thread_cls.assert_not_called()

    @patch("openclaw_telegram_gateway.threading.Thread")
    def test_bare_watch_url_dispatches_relay_and_confirms(self, thread_cls) -> None:
        handled = gateway.handle_video_link_message(1, "https://www.youtube.com/watch?v=abc123")
        self.assertTrue(handled)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Gemini", self.sent[0][1])
        thread_cls.assert_called_once_with(
            target=gateway._relay_video_summary,
            args=(1, "https://www.youtube.com/watch?v=abc123"),
            daemon=True,
        )
        thread_cls.return_value.start.assert_called_once()

    @patch("openclaw_telegram_gateway.threading.Thread")
    def test_bare_short_url_dispatches_relay(self, thread_cls) -> None:
        handled = gateway.handle_video_link_message(1, "https://youtu.be/abc123?si=xyz")
        self.assertTrue(handled)
        thread_cls.assert_called_once()

    @patch("openclaw_telegram_gateway.threading.Thread")
    def test_confirmation_sent_before_thread_started(self, thread_cls) -> None:
        gateway.handle_video_link_message(1, "https://youtu.be/abc123")
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
