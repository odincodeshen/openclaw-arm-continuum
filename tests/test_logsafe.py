import unittest

from openclaw_runtime.logsafe import mask_id, redact_ids


class RedactIdsTest(unittest.TestCase):
    def test_masks_id_fields(self):
        self.assertEqual(redact_ids("[runtime] start chat_id=9000000321 active=1"), "[runtime] start chat_id=…321 active=1")
        self.assertEqual(redact_ids("pushed owners=[9000000321, 123456789] (live)"), "pushed owners=[…321, …789] (live)")
        self.assertEqual(redact_ids("[cron] started recipients={-1002233445566}"), "[cron] started recipients={…566}")
        self.assertEqual(redact_ids("quiz offer skipped owner=55512345: boom"), "quiz offer skipped owner=…345: boom")

    def test_leaves_other_numbers_alone(self):
        line = "saved photo chat_id=42 path=/x/1234567.jpg bytes=9876543 task_id=17800000001"
        self.assertEqual(redact_ids(line), line)

    def test_rejected_line_keeps_the_full_id(self):
        line = "[telegram] rejected chat_id=9000000321"
        self.assertEqual(redact_ids(line), line)

    def test_mask_id(self):
        self.assertEqual(mask_id(-1002233445566), "…566")
        self.assertEqual(mask_id(1234), "1234")


if __name__ == "__main__":
    unittest.main()
