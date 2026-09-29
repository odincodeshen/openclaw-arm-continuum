"""Host-side watchdog: alert on Telegram when an OpenClaw container restarts
unexpectedly -- something no process inside a container can see.

Every OpenClaw service runs with `restart: unless-stopped`, so a container
that crashes is restarted by Docker and its RestartCount goes up. That is
the signal used here. A restart or recreate through openclawctl / docker
compose leaves RestartCount alone (a recreated container starts a new
baseline), and a container stopped on purpose stays stopped, so neither
alerts.

Alerts: a container restarted unexpectedly (with its exit code and whether
it was OOM-killed), a container stuck restarting, a container whose health
check fails, and the host having rebooted. The same problem alerts at most
once per cooldown; stuck-restarting and unhealthy send "recovered" when they
clear.

Run from cron on the host, e.g. every 5 minutes:

    */5 * * * * cd /path/to/openclaw-arm-continuum && python3 scripts/openclaw_watchdog.py

It sends through the bot token and OPENCLAW_ALERT_CHAT_IDS of one profile
.env: OPENCLAW_WATCHDOG_ENV_FILE, or else the first profiles/*/.env (then
.env) that sets OPENCLAW_ALERT_CHAT_IDS. Standard library only.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = Path(os.environ.get("OPENCLAW_WATCHDOG_STATE", ROOT / ".cache" / "openclaw-watchdog.json"))
NAME_PREFIX = os.environ.get("OPENCLAW_WATCHDOG_PREFIX", "openclaw-")
COOLDOWN_SECONDS = int(os.environ.get("OPENCLAW_WATCHDOG_COOLDOWN_MINUTES", "360")) * 60


def read_env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def find_sender_env() -> dict[str, str] | None:
    explicit = os.environ.get("OPENCLAW_WATCHDOG_ENV_FILE")
    candidates = [Path(explicit)] if explicit else sorted(ROOT.glob("profiles/*/.env")) + [ROOT / ".env"]
    for path in candidates:
        if path.is_file():
            env = read_env_file(path)
            if env.get("OPENCLAW_TELEGRAM_BOT_TOKEN") and env.get("OPENCLAW_ALERT_CHAT_IDS"):
                return env
    return None


def snapshot_containers() -> dict[str, dict]:
    """name -> the few State fields the checks need, for every container
    (running or not) whose name starts with NAME_PREFIX."""
    names = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={NAME_PREFIX}", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    if not names:
        return {}
    raw = subprocess.run(["docker", "inspect", *names], capture_output=True, text=True, check=True).stdout
    result = {}
    for item in json.loads(raw):
        state = item.get("State") or {}
        result[item["Name"].lstrip("/")] = {
            "id": item.get("Id", ""),
            "status": state.get("Status", ""),
            "restart_count": int(item.get("RestartCount") or 0),
            "exit_code": state.get("ExitCode"),
            "oom_killed": bool(state.get("OOMKilled")),
            "health": (state.get("Health") or {}).get("Status", ""),
        }
    return result


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def evaluate(previous: dict, containers: dict[str, dict], current_boot: str, now: float) -> tuple[list[str], dict]:
    """Pure check: compare this run's snapshot with the saved state and
    return (messages to send, new state)."""
    alerted: dict[str, float] = dict(previous.get("alerted") or {})
    seen: dict[str, dict] = previous.get("containers") or {}
    messages: list[str] = []

    def alert(key: str, text: str) -> None:
        if now - alerted.get(key, -1e18) >= COOLDOWN_SECONDS:
            alerted[key] = now
            messages.append(text)

    def recover(key: str, text: str) -> None:
        if alerted.pop(key, None) is not None:
            messages.append(text)

    if previous.get("boot_id") and current_boot and previous["boot_id"] != current_boot:
        messages.append("The host rebooted; containers set to restart came back on their own.")

    for name, info in sorted(containers.items()):
        before = seen.get(name)
        if before and before.get("id") == info["id"] and info["restart_count"] > before.get("restart_count", 0):
            times = info["restart_count"] - before.get("restart_count", 0)
            cause = "it ran out of memory (OOM-killed)" if info["oom_killed"] else f"exit code {info['exit_code']}"
            alert(f"restart:{name}:{info['restart_count']}",
                  f"{name} crashed and was restarted by Docker ({times}x since the last check; {cause}).")
        if info["status"] == "restarting":
            alert(f"restarting:{name}", f"{name} keeps restarting -- it can't stay up.")
        else:
            recover(f"restarting:{name}", f"{name} is running again.")
        # a stopped container keeps its last health status; only a running one counts
        if info["status"] != "running":
            alerted.pop(f"unhealthy:{name}", None)
        elif info["health"] == "unhealthy":
            alert(f"unhealthy:{name}", f"{name} is running but its health check fails.")
        elif info["health"] == "healthy":
            recover(f"unhealthy:{name}", f"{name} is healthy again.")

    # restart:<name>:<count> keys are one-off; drop them once their cooldown is over
    alerted = {k: t for k, t in alerted.items() if not k.startswith("restart:") or now - t < COOLDOWN_SECONDS}
    new_state = {
        "boot_id": current_boot or previous.get("boot_id", ""),
        "containers": {n: {"id": i["id"], "restart_count": i["restart_count"]} for n, i in containers.items()},
        "alerted": alerted,
    }
    return messages, new_state


def send(env: dict[str, str], text: str) -> None:
    for chat_id in [c.strip() for c in env["OPENCLAW_ALERT_CHAT_IDS"].split(",") if c.strip()]:
        body = json.dumps({"chat_id": int(chat_id), "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{env['OPENCLAW_TELEGRAM_BOT_TOKEN']}/sendMessage",
            data=body, headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=20).read()


def main() -> int:
    env = find_sender_env()
    if env is None:
        print("openclaw_watchdog: no profile .env with OPENCLAW_ALERT_CHAT_IDS -- nothing to send to", file=sys.stderr)
        return 2
    previous = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    messages, state = evaluate(previous, snapshot_containers(), boot_id(), time.time())
    host = os.uname().nodename if hasattr(os, "uname") else "host"
    for message in messages:
        try:
            send(env, f"OpenClaw watchdog · {host}\n{message}")
        except Exception as exc:  # noqa: BLE001 - keep checking; a lost alert shouldn't lose the state
            print(f"openclaw_watchdog: could not send: {exc}", file=sys.stderr)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(STATE_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
