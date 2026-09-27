import subprocess
import tempfile
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



class OpenclawctlProfileTest(unittest.TestCase):
    """--profile against a throwaway repo root holding two bots: bot_a and
    bot_b (a stopped one). Dry-run only -- nothing is started."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for bot in ("bot_a", "bot_b"):
            (self.root / f"compose.persona.{bot}.yaml").write_text("services: {}\n")
            (self.root / "profiles" / bot).mkdir(parents=True)
            (self.root / "profiles" / bot / ".env").write_text(
                f"OPENCLAW_HOST_GATEWAY_DATA=./profiles/{bot}/gateway-data\n"
            )
        (self.root / "compose.persona.example.yaml").write_text("services: {}\n")

    def ctl(self, *args, running="bot_a"):
        return run(*args, env_extra={"OPENCLAWCTL_ROOT": str(self.root), "OPENCLAWCTL_RUNNING_BOTS": running})

    def test_start_prepares_the_gateway_state_dir_then_starts_only_that_bots_services(self):
        out = self.ctl("--profile", "bot_a", "start")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = [ln for ln in out.stdout.splitlines() if ln.startswith("+ ")]
        self.assertEqual(lines[0], f"+ mkdir -p {self.root}/profiles/bot_a/gateway-data/state")
        self.assertIn(f"--env-file {self.root}/profiles/bot_a/.env", lines[1])
        self.assertIn(f"-f {self.root}/compose.persona.bot_a.yaml", lines[1])
        self.assertIn(
            "up -d --no-deps openclaw-telegram-bot-a openclaw-memory-watcher-bot-a "
            "openclaw-cron-bot-a openclaw-gateway-bot-a",
            lines[1],
        )
        self.assertNotIn("openclaw-vllm", out.stdout)

    def test_restart_and_stop_and_status(self):
        self.assertIn("restart openclaw-telegram-bot-a", self.ctl("--profile", "bot_a", "restart").stdout)
        self.assertIn("stop openclaw-telegram-bot-a", self.ctl("--profile", "bot_a", "stop").stdout)
        self.assertIn("ps openclaw-telegram-bot-a", self.ctl("--profile", "bot_a", "status").stdout)

    def test_logs_for_one_service(self):
        out = self.ctl("--profile", "bot_a", "logs", "cron")
        self.assertIn("logs --tail 50 openclaw-cron-bot-a", out.stdout)
        self.assertNotIn("openclaw-telegram-bot-a", out.stdout)
        self.assertEqual(self.ctl("--profile", "bot_a", "logs", "vllm").returncode, 2)

    def test_all_only_touches_running_bots(self):
        out = self.ctl("--profile", "all", "restart")
        self.assertIn("restart openclaw-telegram-bot-a", out.stdout)
        self.assertNotIn("bot-b", out.stdout)

    def test_all_refuses_start(self):
        self.assertEqual(self.ctl("--profile", "all", "start").returncode, 2)

    def test_unknown_or_malformed_bot_is_rejected_with_the_known_list(self):
        out = self.ctl("--profile", "nope", "status")
        self.assertEqual(out.returncode, 2)
        self.assertIn("known bots: bot_a bot_b", out.stderr)
        self.assertNotIn("example", out.stderr)
        self.assertEqual(self.ctl("--profile", "../etc", "status").returncode, 2)

    def test_profiles_lists_every_bot_including_stopped_ones(self):
        out = self.ctl("profiles")
        self.assertIn("== bot_a ==", out.stdout)
        self.assertIn("== bot_b ==", out.stdout)


if __name__ == "__main__":
    unittest.main()
