import unittest
from pathlib import Path


FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "engineering-review" / "sanitized-design-package.md"


class EngineeringFixtureTest(unittest.TestCase):
    def test_fixture_is_present_and_explicitly_sanitized(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        self.assertIn("synthetic package", text)
        self.assertIn("contains no private source", text)
        self.assertIn("## Architecture", text)
        self.assertIn("## Implementation excerpt", text)
        self.assertIn("## Current tests", text)
        self.assertIn("## Sanitized failure log", text)


if __name__ == "__main__":
    unittest.main()
