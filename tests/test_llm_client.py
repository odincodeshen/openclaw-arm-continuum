import unittest
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


if __name__ == "__main__":
    unittest.main()
