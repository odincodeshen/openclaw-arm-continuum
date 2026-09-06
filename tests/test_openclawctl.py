import subprocess
import unittest
from pathlib import Path

CTL = Path(__file__).resolve().parent.parent / "bin" / "openclawctl"


def run(*args, env_extra=None):
    env = {"PATH": "/usr/bin:/bin", "OPENCLAWCTL_DRY_RUN": "1"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["sh", str(CTL), *args], capture_output=True, text=True, env=env
    )


class OpenclawctlTest(unittest.TestCase):
    def test_help_lists_groups(self):
        out = run("--help")
        self.assertEqual(out.returncode, 0)
        self.assertIn("core|model|full", out.stdout + out.stderr)

    def test_no_args_is_usage_error(self):
        self.assertEqual(run().returncode, 2)

    def test_start_core_targets_core_services_only(self):
        out = run("start", "core")
        self.assertEqual(out.returncode, 0)
        self.assertIn("up -d openclaw-gateway openclaw-telegram", out.stdout)
        self.assertNotIn("openclaw-vllm", out.stdout)

    def test_stop_model_targets_the_engine_only(self):
        out = run("stop", "model")
        self.assertIn("stop openclaw-vllm", out.stdout)
        self.assertNotIn("openclaw-telegram", out.stdout)

    def test_restart_full_stops_then_starts(self):
        out = run("restart", "full")
        lines = [ln for ln in out.stdout.splitlines() if ln.startswith("+ ")]
        self.assertIn("stop", lines[0])
        self.assertIn("up -d", lines[1])
        self.assertIn("openclaw-vllm", lines[1])

    def test_boot_core_mode(self):
        out = run("boot", env_extra={"OPENCLAW_BOOT_MODE": "core"})
        self.assertIn("boot mode: core", out.stdout)
        self.assertIn("up -d openclaw-gateway", out.stdout)
        self.assertNotIn("openclaw-vllm", out.stdout)

    def test_boot_manual_starts_nothing(self):
        out = run("boot", env_extra={"OPENCLAW_BOOT_MODE": "manual"})
        self.assertEqual(out.returncode, 0)
        self.assertNotIn("+ docker compose up", out.stdout)

    def test_boot_default_is_full(self):
        out = run("boot")
        self.assertIn("boot mode: full", out.stdout)
        self.assertIn("openclaw-vllm", out.stdout)

    def test_unknown_group_is_rejected(self):
        out = run("start", "bogus")
        self.assertEqual(out.returncode, 2)
        self.assertNotIn("docker compose up -d\n", out.stdout)

    def test_custom_service_lists_are_honoured(self):
        out = run("start", "model", env_extra={"OPENCLAWCTL_MODEL_SERVICES": "my-engine"})
        self.assertIn("up -d my-engine", out.stdout)

    def test_compose_command_override(self):
        out = run("status", env_extra={"OPENCLAWCTL_COMPOSE": "podman-compose"})
        self.assertIn("+ podman-compose ps", out.stdout)


if __name__ == "__main__":
    unittest.main()
