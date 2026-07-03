import unittest

from openclaw_runtime.llm_client import clean_model_content


class CleanModelContentTest(unittest.TestCase):
    def test_strips_response_wrapper(self) -> None:
        self.assertEqual(clean_model_content("<response>\nhello\n</response>"), "hello")

    def test_strips_think_block(self) -> None:
        self.assertEqual(clean_model_content("<think>hidden</think>\nfinal"), "final")


if __name__ == "__main__":
    unittest.main()
