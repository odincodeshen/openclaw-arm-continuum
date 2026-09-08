import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import vision_smoke  # noqa: E402


class SampleImageUnderstoodTest(unittest.TestCase):
    def test_real_description_passes(self):
        self.assertTrue(
            vision_smoke.sample_image_understood(
                "Three horizontal bands: the top is red, the middle green, the bottom blue."
            )
        )

    def test_chinese_description_passes(self):
        self.assertTrue(
            vision_smoke.sample_image_understood("這是一張三色橫條圖：紅、綠、藍。")
        )

    def test_single_incidental_colour_word_is_not_enough(self):
        self.assertFalse(
            vision_smoke.sample_image_understood("I cannot see the image you are describing.")
        )
        self.assertFalse(vision_smoke.sample_image_understood("The answer is red."))

    def test_empty_is_false(self):
        self.assertFalse(vision_smoke.sample_image_understood(""))
        self.assertFalse(vision_smoke.sample_image_understood(None))


if __name__ == "__main__":
    unittest.main()
