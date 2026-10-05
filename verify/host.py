#!/usr/bin/env python3
"""bin/verify: check this checkout on this machine, without personal data.

    bin/verify                 # standard: unit tests, then every scenario
    bin/verify quick           # unit tests and ci_validate only (no services)
    bin/verify scenarios       # scenarios only
    bin/verify scenarios --only checkin_skip rag_keywords

Everything runs in throwaway containers from verify/Dockerfile (built once
per machine):

The sandbox gets a copy of the files git tracks (with uncommitted changes),
never the checkout itself. Gitignored files never reach it: profiles/, .env
files, .cache/ and the local settings.

- quick: the unit tests, with no services and Telegram unreachable.
- scenarios: each verify/scenarios/*.yaml in its own sandbox:
  - the real gateway code from this checkout, driven by a fake Telegram;
  - this machine's model, embeddings, Qdrant, Whisper and TTS;
  - a tmpfs workspace, and Qdrant collections named verify_<time>_<id>_,
    deleted afterwards;
  - api.telegram.org pointing at 127.0.0.1.

To find the services, it copies a fixed allowlist of service settings
(model, vision, embedding, Qdrant, Whisper and TTS addresses, context
sizes, /rag settings) and the Docker network from one running bot container.
That is VERIFY_REFERENCE in .verify.local, or the first openclaw-telegram-*
container. It never reads chat IDs, tokens, prompts or data.

Reports go to .cache/verify/. Exit code 0 = everything passed or was
skipped for a missing service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCENARIOS = REPO / "verify" / "scenarios"
REPORTS = REPO / ".cache" / "verify"
SERVICE_KEYS = {
    "OPENCLAW_VLLM_BASE_URL", "OPENCLAW_VLLM_MODEL", "OPENCLAW_VLM_BASE_URL", "OPENCLAW_VLM_MODEL",
    "OPENCLAW_VISION_ENABLED", "OPENCLAW_OLLAMA_BASE_URL", "OPENCLAW_EMBEDDING_MODEL",
    "OPENCLAW_EMBEDDING_VECTOR_SIZE", "OPENCLAW_QDRANT_BASE_URL", "OPENCLAW_WHISPER_BASE_URL",
    "OPENCLAW_WHISPER_ENABLED", "OPENCLAW_TTS_BASE_URL", "OPENCLAW_MODEL_CONTEXT_TOKENS",
    "OPENCLAW_VLM_CONTEXT_TOKENS", "OPENCLAW_REQUEST_TIMEOUT", "OPENCLAW_MAX_TOKENS",
    "OPENCLAW_RAG_CONTEXT_TOKENS", "OPENCLAW_RAG_PASSAGE_TOKENS", "OPENCLAW_RAG_RELEVANCE_MARGIN",
    "OPENCLAW_RAG_KEYWORD_SEARCH", "OPENCLAW_RAG_KEYWORD_HITS", "OPENCLAW_RAG_VECTOR_HITS",
    "OPENCLAW_RETRIEVAL_LIMIT", "OPENCLAW_IMAGE_OCR_MAX_TOKENS",
}


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def local_config() -> dict[str, str]:
    path = REPO / ".verify.local"
    config = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip('"')
    return config


def source_tar() -> Path:
    """The files git tracks or would track (working-tree versions): what the
    sandbox gets instead of the checkout."""
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=REPO, capture_output=True,
                           check=True).stdout.decode().split("\0")
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / "source.tar"
    with tarfile.open(path, "w") as tar:
        for name in files:
            if name and (REPO / name).is_file():
                tar.add(REPO / name, arcname=name)
    return path


UNPACK = "mkdir -p /src && tar -x -C /src && cd /src && "


def image() -> str:
    dockerfile = REPO / "verify" / "Dockerfile"
    tag = "openclaw-verify:" + hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:12]
    if sh(["docker", "image", "inspect", tag]).returncode != 0:
        print(f"building {tag} (first run on this machine) ...", flush=True)
        build = sh(["docker", "build", "-q", "-t", tag, "-f", str(dockerfile), str(dockerfile.parent)])
        if build.returncode != 0:
            raise SystemExit(f"building the verify image failed:\n{build.stderr[-1500:]}")
    return tag


def reference(config: dict[str, str]) -> tuple[str, dict[str, str], str, list[str]]:
    """(container, service settings, network, extra hosts) of a running bot."""
    name = config.get("VERIFY_REFERENCE", "")
    if not name:
        running = sh(["docker", "ps", "--format", "{{.Names}}"]).stdout.split()
        bots = sorted(n for n in running if n.startswith("openclaw-telegram"))
        if not bots:
            raise SystemExit("no running openclaw-telegram-* container to take the service settings from; "
                             "set VERIFY_REFERENCE in .verify.local")
        name = bots[0]
    info = json.loads(sh(["docker", "inspect", name]).stdout or "[]")
    if not info:
        raise SystemExit(f"container {name} not found")
    env = {}
    for item in info[0]["Config"].get("Env") or []:
        key, _, value = item.partition("=")
        if key in SERVICE_KEYS:
            env[key] = value
    networks = list((info[0]["NetworkSettings"].get("Networks") or {}).keys())
    hosts = info[0]["HostConfig"].get("ExtraHosts") or []
    return name, env, networks[0] if networks else "bridge", hosts


def run_quick(tag: str, source: Path) -> tuple[bool, str]:
    started = time.time()
    # a few tests resolve public host names, so the network stays on; Telegram is still unreachable
    with source.open("rb") as stdin:
        result = sh(["docker", "run", "--rm", "-i", "--add-host", "api.telegram.org:127.0.0.1",
                     "-e", "RUFF_NO_CACHE=true", tag, "sh", "-c",
                     UNPACK + "python -m pytest -q -p no:cacheprovider tests && python scripts/ci_validate.py"],
                    timeout=3600, stdin=stdin)
    out = (result.stdout + result.stderr).strip().splitlines()
    tail = [line for line in out if line.strip()][-3:]
    return result.returncode == 0, f"{'passed' if result.returncode == 0 else 'FAILED'} in {time.time() - started:.0f}s" \
        + ("" if result.returncode == 0 else ": " + " | ".join(tail))


def sandbox_cmd(tag: str, env: dict[str, str], network: str, hosts: list[str], args: list[str]) -> list[str]:
    cmd = ["docker", "run", "--rm", "-i", "--network", network, "--tmpfs", "/workspace:rw,size=512m",
           "--add-host", "api.telegram.org:127.0.0.1"]
    for host in hosts or ["host.docker.internal:host-gateway"]:
        cmd += ["--add-host", host]
    if not any(h.startswith("host.docker.internal") for h in hosts):
        cmd += ["--add-host", "host.docker.internal:host-gateway"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    return cmd + [tag, "sh", "-c", UNPACK + "exec python /src/verify/runner.py " + shlex.join(args)]


def scenario_timeout(path: Path) -> int:
    """A scenario's "timeout:" in seconds (default 900); read without YAML,
    which the host may not have."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("timeout:"):
            return int(line.split(":", 1)[1].strip())
    return 900


def run_scenario(tag, env, network, hosts, path: Path, source: Path) -> dict:
    prefix = f"verify_{int(time.time())}_{secrets.token_hex(3)}_"
    started = time.time()
    try:
        with source.open("rb") as stdin:
            result = sh(sandbox_cmd(tag, env, network, hosts, [f"/src/verify/scenarios/{path.name}", "--prefix", prefix]),
                        timeout=scenario_timeout(path), stdin=stdin)
    except subprocess.TimeoutExpired:
        limit = scenario_timeout(path)
        return {"name": path.stem, "status": "fail", "reason": f"timed out after {limit} s", "seconds": limit}
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    try:
        outcome = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        outcome = {"name": path.stem, "status": "fail",
                   "reason": "the sandbox gave no result:\n" + (result.stderr or result.stdout)[-1500:]}
    outcome.setdefault("seconds", round(time.time() - started, 1))
    outcome["file"] = path.name
    return outcome


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Verify this checkout here, with no personal data.")
    parser.add_argument("mode", nargs="?", default="standard", choices=["quick", "scenarios", "standard"])
    parser.add_argument("--only", nargs="*", default=[], help="scenario names (file stems)")
    args = parser.parse_args(argv)
    config = local_config()
    tag = image()
    source = source_tar()
    lines = [f"# OpenClaw verify -- {time.strftime('%Y-%m-%d %H:%M %Z')} -- {args.mode}", ""]
    ok = True
    if args.mode in ("quick", "standard"):
        print("== unit tests and ci_validate", flush=True)
        passed, detail = run_quick(tag, source)
        print(f"   {detail}", flush=True)
        lines += [f"Unit tests and ci_validate: {detail}", ""]
        ok &= passed
    if args.mode in ("scenarios", "standard") and (ok or args.mode == "scenarios"):
        name, env, network, hosts = reference(config)
        print(f"== scenarios (services from {name}, network {network})", flush=True)
        with source.open("rb") as stdin:
            sweep = sh(sandbox_cmd(tag, env, network, hosts, ["--sweep", "--prefix", "verify_"]), timeout=120, stdin=stdin)
        swept = next((json.loads(line)["swept"] for line in sweep.stdout.splitlines() if line.startswith("{")), 0)
        if swept:
            print(f"   removed {swept} leftover verify collection(s)", flush=True)
        paths = sorted(SCENARIOS.glob("*.yaml"))
        if args.only:
            paths = [p for p in paths if p.stem in args.only]
        lines += ["| Scenario | Result | Time | Detail |", "| --- | --- | --- | --- |"]
        for path in paths:
            outcome = run_scenario(tag, env, network, hosts, path, source)
            status = outcome.get("status")
            ok &= status in ("pass", "skip")
            retry = " (2nd try)" if outcome.get("attempts", 1) > 1 and status == "pass" else ""
            detail = "" if status == "pass" else (outcome.get("reason") or "").strip().splitlines()[0][:160] \
                if outcome.get("reason") else ""
            if status == "fail" and outcome.get("step"):
                detail = f"step {outcome['step']}: {detail}"
            print(f"   {status.upper():4} {path.stem}  {outcome.get('seconds', 0)}s{retry}  {detail}", flush=True)
            if status == "fail" and outcome.get("reason"):
                print("        " + outcome["reason"].replace("\n", "\n        ")[:1500], flush=True)
            lines.append(f"| {path.stem} | {'**FAIL**' if status == 'fail' else status}{retry} | "
                         f"{outcome.get('seconds', 0)}s | {detail.replace('|', '/')} |")
    REPORTS.mkdir(parents=True, exist_ok=True)
    report = REPORTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.mode}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{'All passed' if ok else 'FAILED'} -- report: {report.relative_to(REPO)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
