import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from openclaw_runtime.llm_client import LlmClient, clean_model_content
from openclaw_runtime.model_catalog import ModelSpec
from tests.support import build_settings


class CleanModelContentTest(unittest.TestCase):
    def test_strips_response_wrapper(self) -> None:
        self.assertEqual(clean_model_content("<response>\nhello\n</response>"), "hello")

    def test_strips_think_block(self) -> None:
        self.assertEqual(clean_model_content("<think>hidden</think>\nfinal"), "final")


class LlmClientModelSpecTest(unittest.TestCase):
    @patch("openclaw_runtime.llm_client.request_json")
    def test_model_spec_controls_endpoint_model_and_timeout(self, request_json) -> None:
        request_json.return_value = {"choices": [{"message": {"content": "done"}}]}
        spec = ModelSpec("local_coder", "http://coder/v1", "coder-model", ("code_review",), 77)
        client = LlmClient(build_settings(), model_spec=spec)

        self.assertEqual(client.chat("review this"), "done")

        method, url, payload = request_json.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://coder/v1/chat/completions")
        self.assertEqual(payload["model"], "coder-model")
        self.assertEqual(request_json.call_args.kwargs["timeout"], 77)

    @patch("openclaw_runtime.llm_client.request_json")
    def test_chat_json_sends_openai_json_schema(self, request_json) -> None:
        request_json.return_value = {"choices": [{"message": {"content": '{"ok":true}'}}]}
        client = LlmClient(build_settings())
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}

        result = client.chat_json("return status", schema, schema_name="status", max_tokens=20)

        self.assertEqual(result, '{"ok":true}')
        payload = request_json.call_args.args[2]
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "status")
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], schema)


class ReplyLanguageTest(unittest.TestCase):
    @patch("openclaw_runtime.llm_client.request_json")
    def test_no_language_keeps_plain_directive(self, request_json) -> None:
        request_json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        LlmClient(build_settings(reply_language="")).chat("hi")
        user_msg = request_json.call_args.args[2]["messages"][-1]["content"]
        self.assertIn("Answer directly", user_msg)
        self.assertNotIn("Reply in", user_msg)

    @patch("openclaw_runtime.llm_client.request_json")
    def test_language_is_injected_into_chat_and_image_prompts(self, request_json) -> None:
        request_json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        client = LlmClient(build_settings(reply_language="Traditional Chinese"))

        client.chat("hi")
        chat_msg = request_json.call_args.args[2]["messages"][-1]["content"]
        self.assertIn("Reply in Traditional Chinese by default", chat_msg)
        self.assertIn("explicitly asks", chat_msg)

    @patch("openclaw_runtime.llm_client.request_json")
    def test_history_turns_are_not_rewritten(self, request_json) -> None:
        request_json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        client = LlmClient(build_settings(reply_language="Traditional Chinese"))
        client.chat("now", history=[{"role": "user", "content": "before"}, {"role": "assistant", "content": "reply"}])
        messages = request_json.call_args.args[2]["messages"]
        self.assertEqual(messages[1], {"role": "user", "content": "before"})
        self.assertIn("Reply in Traditional Chinese", messages[-1]["content"])


class DateGroundingTest(unittest.TestCase):
    """Without today's real date in the system prompt, the model has no way
    to know "now" and falls back to whatever its training data implies --
    real current-dated info (a 2026 booking, today's news) then reads as
    being years in the future. Regression coverage for that fix."""

    def _today_str(self) -> str:
        # UTC, matching _system_message()'s own clock -- local date can
        # differ from UTC date near midnight depending on the host's time
        # zone (this is exactly why the app uses UTC, not local time).
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @patch("openclaw_runtime.llm_client.request_json")
    def test_chat_system_message_includes_todays_date(self, request_json) -> None:
        request_json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        LlmClient(build_settings(system_prompt="base prompt")).chat("hi")
        system_msg = request_json.call_args.args[2]["messages"][0]
        self.assertEqual(system_msg["role"], "system")
        self.assertIn("base prompt", system_msg["content"])
        self.assertIn(self._today_str(), system_msg["content"])
        self.assertIn("do not assume your training cutoff is", system_msg["content"])

    @patch("openclaw_runtime.llm_client.request_json")
    def test_chat_json_system_message_includes_todays_date(self, request_json) -> None:
        request_json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        client = LlmClient(build_settings(system_prompt="base prompt"))
        client.chat_json("classify this", {"type": "object"}, schema_name="x")
        system_msg = request_json.call_args.args[2]["messages"][0]
        self.assertIn(self._today_str(), system_msg["content"])

    @patch("openclaw_runtime.llm_client.request_json")
    def test_chat_with_image_system_message_includes_todays_date(self, request_json) -> None:
        request_json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        client = LlmClient(build_settings(system_prompt="base prompt"))
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "pic.jpg"
            img.write_bytes(b"\xff\xd8\xff\xd9")
            client.chat_with_image(img, "describe this")
        system_msg = request_json.call_args.args[2]["messages"][0]
        self.assertIn(self._today_str(), system_msg["content"])

    @patch("openclaw_runtime.llm_client.datetime")
    @patch("openclaw_runtime.llm_client.request_json")
    def test_date_is_computed_fresh_each_call_not_cached(self, request_json, fake_datetime) -> None:
        import datetime as real_datetime

        request_json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        fake_datetime.now.side_effect = [
            real_datetime.datetime(2026, 1, 1, tzinfo=real_datetime.timezone.utc),
            real_datetime.datetime(2026, 6, 15, tzinfo=real_datetime.timezone.utc),
        ]
        client = LlmClient(build_settings(system_prompt="base prompt"))
        client.chat("first")
        client.chat("second")
        first_call, second_call = request_json.call_args_list
        self.assertIn("2026-01-01", first_call.args[2]["messages"][0]["content"])
        self.assertIn("2026-06-15", second_call.args[2]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
