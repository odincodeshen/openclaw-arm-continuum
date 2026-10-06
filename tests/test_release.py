import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("release", Path(__file__).resolve().parent.parent / "scripts" / "release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class FakeRepo:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(release, "REPO", self.root)
        p.start()
        test.addCleanup(p.stop)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class PrivacyScanTest(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo(self)
        self.repo.write("profiles/bot/.env", "OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS=111222333,444555666\n"
                                             "OPENCLAW_TELEGRAM_BOT_TOKEN=123:abcdefghijklmnop\n"
                                             "OPENCLAW_CHECKIN_OWNER=777888999\nOPENCLAW_MAX_TOKENS=512\n")

    def scan(self, added_lines, tracked=("app/x.py",)):
        diff = "+++ b/docs/x.md\n" + "\n".join("+" + line for line in added_lines)
        answers = {("diff",): diff, ("ls-files",): "\n".join(tracked)}

        def fake_git(*args):
            return answers[(args[0],)] if args[0] in ("diff", "ls-files") else "root"

        with patch.object(release, "git", fake_git):
            return release.check_privacy("v1.0")

    def test_private_values_come_from_profile_envs(self):
        values = release.private_values()
        self.assertEqual(values, ["111222333", "123:abcdefghijklmnop", "444555666", "777888999"])

    def test_marker_allows_fake_contact_data_but_never_real_secrets(self):
        marker = release.ALLOW_MARKER
        self.assertIn("clean", self.scan([f"mail me@somewhere.org  # {marker}"]))  # privacy-scan: fake data
        with self.assertRaises(release.Failed):
            self.scan([f"owner 777888999  # {marker}"])

    def test_clean_diff_passes(self):
        self.assertIn("privacy scan clean", self.scan(["Bind to 172.17.0.1", "noreply@anthropic.com", "chat_id=…175"]))

    def test_each_kind_of_leak_is_caught_without_printing_it(self):
        with self.assertRaises(release.Failed) as caught:
            self.scan(["owner 777888999", f"path {Path.home()}/x", "mail me@somewhere.org", "lan 192.168.1.20"],  # privacy-scan: fake data
                      tracked=("app/x.py", "profiles/bot/.env", "compose.persona.bot.yaml",
                               "compose.persona.o6.example.yaml"))
        text = str(caught.exception)
        for expected in ("a chat ID or token", "a home directory path", "an e-mail address", "a private IP address",
                         "profiles/bot/.env: must stay local", "compose.persona.bot.yaml: must stay local"):
            self.assertIn(expected, text)
        self.assertNotIn("example.yaml", text)
        self.assertNotIn("777888999", text)
        self.assertNotIn("me@somewhere.org", text)  # privacy-scan: fake data


class VersionBumpTest(unittest.TestCase):
    def test_bumps_all_three_files(self):
        repo = FakeRepo(self)
        repo.write("README.md", "# X\n\nVersion: `v1.27`\n")
        repo.write("README.zh-TW.md", "版本：`v1.27`\n")
        repo.write("docs/FUTURE_TODO.md", "Current release: **v1.27**.\n\nStill open, re-baselined against v1.27:\n")
        changed = release.bump_version("v1.28", "v1.27")
        self.assertEqual(changed, ["README.md", "README.zh-TW.md", "docs/FUTURE_TODO.md"])
        self.assertIn("Version: `v1.28`", (repo.root / "README.md").read_text())
        todo = (repo.root / "docs/FUTURE_TODO.md").read_text()
        self.assertIn("**v1.28**", todo)
        self.assertIn("re-baselined against v1.28:", todo)

    def test_version_order(self):
        self.assertLess(release.version_key("v1.9"), release.version_key("v1.10"))


class E2eTest(unittest.TestCase):
    REPORT = ("# OpenClaw L3 e2e -- bot\n\n{n}/10 passed · 2026-10-04 10:00 UTC\n\n| Check | Result |\n"
              "| --- | --- |\n| Category RAG | {r} | 5s | x |\n")

    def fake_run(self, outcomes):
        calls = []

        def run(cmd, cwd=None, stdin=None, capture_output=True, timeout=None):
            calls.append(cmd)
            ok = outcomes[len(calls) - 1]
            out = self.REPORT.format(n=10 if ok else 9, r="PASS" if ok else "**FAIL**").encode()
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, out, b"")

        return run, calls

    def test_retry_then_pass_and_remote_command(self):
        run, calls = self.fake_run([False, True])
        with patch.object(release.subprocess, "run", run):
            lines = release.check_e2e([], ["ssh:o6:bot-b"])
        self.assertEqual(calls[0][:2], ["ssh", "o6"])
        self.assertEqual(lines, ["e2e (optional) passed: 10/10 passed on the second run"])

    def test_required_failure_stops_the_release(self):
        run, _ = self.fake_run([False, False])
        with patch.object(release.subprocess, "run", run), self.assertRaises(release.Failed) as caught:
            release.check_e2e(["bot-a"], [])
        self.assertIn("9/10 passed (failed: Category RAG)", str(caught.exception))

    def test_optional_failure_only_warns(self):
        run, _ = self.fake_run([False, False])
        with patch.object(release.subprocess, "run", run):
            lines = release.check_e2e([], ["bot-b"])
        self.assertTrue(lines[0].startswith("e2e (optional) FAILED: 9/10 passed"))



class VerifyStepTest(unittest.TestCase):
    OUT_OK = ("== platform: gb10 (NVIDIA GB10; services from x)\n   PASS Text model and context  0.3s\n"
              "== scenarios (services from x)\n   PASS help_menu  0.0s\n   PASS mem  0.1s\n"
              "All passed -- report: r\n")
    OUT_FAIL = ("== platform: orion-o6 (Radxa)\n   FAIL photo_text  25s  step 1\n   PASS help_menu  0.0s\n"
                "   generation_tokens_per_s: 9 tokens/s -- **slow** (wants ≥ 11)\nFAILED -- report: r\n")

    def fake(self, outputs):
        calls = []

        def run(cmd, cwd=None, capture_output=True, text=True, timeout=None):
            calls.append(cmd)
            code, out = outputs[len(calls) - 1]
            return subprocess.CompletedProcess(cmd, code, out, "")

        return run, calls

    def test_local_must_pass_remote_only_warns(self):
        run, calls = self.fake([(0, self.OUT_OK), (1, self.OUT_FAIL)])
        with patch.object(release.subprocess, "run", run):
            lines = release.check_verify("standard", ["o6:openclaw-verify"])
        self.assertEqual(calls[1][-2:], ["--remote", "o6:openclaw-verify"])
        self.assertEqual(lines[0], "bin/verify standard passed on this machine (gb10): 2 scenarios passed")
        self.assertEqual(lines[1], "bin/verify standard FAILED (optional) on o6 (orion-o6): FAILED (photo_text), "
                                   "1 speed warning(s)")

    def test_local_failure_stops_the_release(self):
        run, _ = self.fake([(1, self.OUT_FAIL)])
        with patch.object(release.subprocess, "run", run), self.assertRaises(release.Failed):
            release.check_verify("standard", [])

    def test_none_skips_the_local_run(self):
        run, calls = self.fake([])
        with patch.object(release.subprocess, "run", run):
            self.assertEqual(release.check_verify("none", []), [])
        self.assertEqual(calls, [])

if __name__ == "__main__":
    unittest.main()
