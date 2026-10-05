import importlib.util
import unittest
from pathlib import Path

_path = Path(__file__).resolve().parent.parent / "verify" / "host.py"
_spec = importlib.util.spec_from_file_location("verify_host", _path)
host = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host)


class PickProfileTest(unittest.TestCase):
    def pick(self, **facts):
        base = {"arch": "aarch64", "cpus": 8, "board": "", "gpu": ""}
        return host.pick_profile({**base, **facts})["name"]

    def test_known_machines(self):
        self.assertEqual(self.pick(board="Radxa Orion O6"), "orion-o6")
        self.assertEqual(self.pick(board="NVIDIA_DGX_Spark", gpu="NVIDIA GB10"), "gb10")

    def test_fallbacks(self):
        self.assertEqual(self.pick(arch="x86_64", gpu="NVIDIA GeForce RTX 4090"), "nvidia-gpu")
        self.assertEqual(self.pick(board="Raspberry Pi 5 Model B"), "cpu-only")

    def test_named_profile_wins(self):
        self.assertEqual(host.pick_profile({"arch": "x86_64", "gpu": ""}, "orion-o6")["name"], "orion-o6")
        with self.assertRaises(SystemExit):
            host.pick_profile({}, "no-such-profile")

    def test_every_profile_has_run_and_thresholds(self):
        for profile in host.profiles():
            self.assertIn("long_prompt_tokens", profile.get("run", {}), profile["name"])
            for key in profile.get("thresholds", {}):
                self.assertIn(key, {k for k, _, _ in host.LIMITS.values()}, f"{profile['name']}: {key}")


class CompareTest(unittest.TestCase):
    def test_min_max_and_unset(self):
        rows = dict((m, v) for m, _, v in host.compare(
            {"generation_tokens_per_s": 4.0, "vision_seconds": 10.0, "prompt_tokens_per_s": 50.0, "other": 1},
            {"generation_tokens_per_s_min": 5, "vision_seconds_max": 15, "prompt_tokens_per_s_min": 0}))
        self.assertTrue(rows["generation_tokens_per_s"].startswith("**slow**"))
        self.assertTrue(rows["vision_seconds"].startswith("ok"))
        self.assertEqual(rows["prompt_tokens_per_s"], "no limit set")
        self.assertEqual(rows["other"], "no limit set")



class JudgeGoldTest(unittest.TestCase):
    MINIMUMS = {"retrieval": 0.8, "answers": 0.7, "ocr": 0.9}
    TOLERANCES = {"retrieval": 0.06, "answers": 0.12, "ocr": 0.03, "slower": 1.5}

    def run_result(self, retrieval=0.92, answers=0.92, ocr=1.0, seconds=5.0, model="m", wrong=()):
        return {"model": model, "minimums": self.MINIMUMS, "tolerances": self.TOLERANCES,
                "scores": {"retrieval": retrieval, "answers": answers, "ocr": ocr, "answers_cross": 0.7},
                "timing": {"answer_seconds_median": seconds, "ocr_seconds_median": 4.0},
                "questions": [{"id": q, "right": q not in wrong, "retrieved": True} for q in ("a", "b", "c")]}

    def test_first_run_needs_only_the_minimums(self):
        passed, rows, warnings = host.judge_gold(self.run_result(), None)
        self.assertTrue(passed)
        self.assertEqual(warnings, [])
        self.assertTrue(any("none yet" in row for row in rows))

    def test_below_minimum_fails(self):
        passed, rows, _ = host.judge_gold(self.run_result(answers=0.6), None)
        self.assertFalse(passed)
        self.assertTrue(any("below the minimum" in row for row in rows))

    def test_drop_from_baseline_fails_within_tolerance_passes(self):
        baseline = self.run_result(retrieval=0.97)
        self.assertTrue(host.judge_gold(self.run_result(retrieval=0.92), baseline)[0])   # -5 points: within 6
        passed, rows, _ = host.judge_gold(self.run_result(retrieval=0.89), baseline)     # -8 points
        self.assertFalse(passed)
        self.assertTrue(any("dropped from 97%" in row for row in rows))

    def test_warnings_for_slower_newly_wrong_and_model_change(self):
        baseline = self.run_result()
        _, _, warnings = host.judge_gold(self.run_result(seconds=9.0, wrong=("b",), model="m2"), baseline)
        text = " ".join(warnings)
        self.assertIn("answer_seconds_median: 9.0s, baseline 5.0s", text)
        self.assertIn("newly wrong: b", text)
        self.assertIn("the model changed", text)


if __name__ == "__main__":
    unittest.main()
