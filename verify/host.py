#!/usr/bin/env python3
"""bin/verify: check this checkout on this machine, without personal data.

    bin/verify                 # standard: unit tests, platform check, every scenario
    bin/verify quick           # unit tests and ci_validate only (no services)
    bin/verify platform        # what this machine's services can do, against its profile
    bin/verify scenarios       # scenarios only
    bin/verify gold            # answer quality on the fixed gold set, against this machine's baseline
    bin/verify full            # standard, then gold (the weekly run)
    bin/verify gold --accept   # take this run as the new baseline (after an intended change)
    bin/verify platform --platform orion-o6   # pick the profile instead of detecting it
    bin/verify scenarios --only checkin_skip rag_keywords

Everything runs in throwaway containers from verify/Dockerfile (built once
per machine):

The sandbox gets a copy of the files git tracks (with uncommitted changes),
never the checkout itself. Gitignored files never reach it: profiles/, .env
files, .cache/ and the local settings.

- quick: the unit tests, with no services and Telegram unreachable.
- gold (verify/gold_check.py): files the made-up gold corpus as a bot would,
  then for 36 questions checks whether /rag sent the right document
  (retrieval) and whether the answer has an expected string (answers). It
  also scores 7 images character by character (ocr). It fails below the
  minimums in verify/gold/gold.yaml, or on a drop beyond the tolerances from
  this machine's baseline (.cache/verify/baseline-<platform>.json, written by
  the first passing run or --accept). Slower than 1.5x the baseline is a
  warning.
- platform (verify/platform_check.py): checks the model, its context window,
  JSON output, a long prompt, image text, embeddings, Qdrant, Whisper and
  TTS. Speeds are compared with the matching verify/platforms/*.toml
  profile, chosen from the board name, GPU and CPU architecture. Correctness
  failures fail the run; a slow number is a warning.
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
import os
import re
import json
import platform as host_platform
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
import tomllib
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
PLATFORMS = REPO / "verify" / "platforms"
# metric -> (threshold key, "min" or "max", unit)
LIMITS = {
    "generation_tokens_per_s": ("generation_tokens_per_s_min", "min", "tokens/s"),
    "prompt_tokens_per_s": ("prompt_tokens_per_s_min", "min", "tokens/s"),
    "long_prompt_seconds": ("long_prompt_seconds_max", "max", "s"),
    "vision_seconds": ("vision_seconds_max", "max", "s"),
    "embedding_ms": ("embedding_ms_max", "max", "ms"),
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


# Outside a git checkout (a deployed copy), only these are sent: code, tests,
# docs and examples -- never profiles/, .env files or data.
FALLBACK_DIRS = ("app", "verify", "tests", "scripts", "bin", "docs", "deploy", "runtime", "tts", "whisper", "scraper",
                 "examples", ".github")
FALLBACK_FILES = ("pyproject.toml", "README.md", "README.zh-TW.md", ".gitignore", ".env.example", "LICENSE", "VERSION",
                  "GITHUB.md")
MANIFEST = ".verify-files"  # written by --remote: the sender's exact git file list


def source_files() -> list[str]:
    """The files git tracks or would track (working-tree versions), or the
    fallback set outside a git checkout."""
    listed = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=REPO, capture_output=True)
    if listed.returncode == 0:
        return [name for name in listed.stdout.decode().split("\0") if name]
    manifest = REPO / MANIFEST
    if manifest.is_file():
        names = [line for line in manifest.read_text(encoding="utf-8").splitlines() if line]
        return [name for name in names if (REPO / name).is_file() and not name.startswith("profiles/")]
    files = [name for name in FALLBACK_FILES if (REPO / name).is_file()]
    files += [p.name for p in REPO.glob("compose*.yaml") if "persona" not in p.name or ".example." in p.name]
    files += [p.name for p in REPO.glob(".env*.example")]
    for folder in FALLBACK_DIRS:
        for path in (REPO / folder).rglob("*") if (REPO / folder).is_dir() else []:
            if path.is_file() and "__pycache__" not in path.parts and ".cache" not in path.parts:
                files.append(str(path.relative_to(REPO)))
    return sorted(set(files))


def source_tar() -> Path:
    """What the sandbox gets instead of the checkout (see source_files)."""
    files = source_files()
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / "source.tar"
    with tarfile.open(path, "w") as tar:
        for name in files:
            if name and (REPO / name).is_file():
                tar.add(REPO / name, arcname=name)
    return path


UNPACK = "mkdir -p /src && tar -x -C /src && cd /src && "


def host_facts() -> dict:
    """What the host itself says about its hardware (nothing personal)."""
    facts = {"arch": host_platform.machine(), "cpus": os.cpu_count() or 0, "board": "", "gpu": ""}
    for path in ("/proc/device-tree/model", "/sys/class/dmi/id/product_name"):
        try:
            facts["board"] = Path(path).read_text(errors="replace").replace("\0", "").strip()
            if facts["board"]:
                break
        except OSError:
            continue
    gpu = sh(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]) if shutil_which("nvidia-smi") else None
    if gpu and gpu.returncode == 0:
        facts["gpu"] = gpu.stdout.strip().splitlines()[0] if gpu.stdout.strip() else ""
    try:
        meminfo = Path("/proc/meminfo").read_text()
        facts["memory_gb"] = round(int(meminfo.split("MemTotal:")[1].split()[0]) / 1024 / 1024)
    except (OSError, IndexError, ValueError):
        pass
    return facts


def shutil_which(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


def profiles() -> list[dict]:
    found = []
    for path in sorted(PLATFORMS.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        data.setdefault("name", path.stem)
        found.append(data)
    return sorted(found, key=lambda p: -int(p.get("priority", 0)))


def pick_profile(facts: dict, wanted: str = "") -> dict:
    """The named profile, or the highest-priority one whose [match] fits:
    board / gpu are substrings (gpu = "none" means no GPU), arch is exact."""
    for profile in profiles():
        if wanted:
            if profile["name"] == wanted:
                return profile
            continue
        match = profile.get("match") or {}
        ok = True
        for key, value in match.items():
            have = str(facts.get(key, ""))
            if key == "gpu" and str(value).lower() == "none":
                ok &= not have
            elif key == "arch":
                ok &= have == str(value)
            else:
                ok &= str(value).lower() in have.lower()
        if ok:
            return profile
    raise SystemExit(f"no platform profile {wanted!r} in verify/platforms/" if wanted else "no platform profile matched")


def compare(metrics: dict, thresholds: dict) -> list[tuple[str, str, str]]:
    """(metric, value, verdict) with verdict ok / warn / no limit."""
    rows = []
    for metric, value in metrics.items():
        key, kind, unit = LIMITS.get(metric, ("", "", ""))
        limit = thresholds.get(key) if key else None
        if not limit:
            rows.append((metric, f"{value} {unit}".strip(), "no limit set"))
            continue
        ok = value >= limit if kind == "min" else value <= limit
        rows.append((metric, f"{value} {unit}".strip(),
                     f"ok ({'≥' if kind == 'min' else '≤'} {limit})" if ok else
                     f"**slow** (wants {'≥' if kind == 'min' else '≤'} {limit})"))
    return rows


# Commands a sandbox can't exercise, and why. Covered elsewhere: unit tests,
# or the e2e run inside a real bot (scripts/e2e_run.py).
COVERAGE_EXEMPT = {
    "doc": "imports a public web document (needs the internet)",
    "search": "searches the web (needs the internet)",
    "review": "a long multi-model engineering review",
    "w": "needs the English bot's local dictionary database",
    "vocab": "needs the English bot's local dictionary database",
    "say": "needs the dictionary and a recorded voice",
}


def coverage() -> tuple[list[str], list[str]]:
    """(bot commands, check-in templates) that no scenario uses: what a new
    feature still needs a scenario for. Read from the source, no YAML needed."""
    gateway = (REPO / "app" / "openclaw_telegram_gateway.py").read_text(encoding="utf-8")
    commands = sorted(set(re.findall(r'\{"command": "([a-z_]+)"', gateway)))
    used_text = "\n".join(p.read_text(encoding="utf-8") for p in SCENARIOS.glob("*.yaml"))
    used = set(re.findall(r"/([a-z_]+)", used_text))
    templates = sorted(p.stem for p in (REPO / "app" / "openclaw_runtime" / "checkin_presets").glob("*.toml"))
    used_templates = set(re.findall(r"checkins: \[([^\]]*)\]", used_text))
    named = {name.strip() for group in used_templates for name in group.split(",")}
    named |= set(re.findall(r"/checkins add ([a-z_]+)", used_text))
    named |= {"night"} if "OPENCLAW_NIGHT_RITUAL_ENABLED" in used_text else set()
    return [c for c in commands if c not in used and c not in COVERAGE_EXEMPT], [t for t in templates if t not in named]


def remote(target: str, argv: list[str]) -> int:
    """Copy this checkout's files to HOST:DIR and run bin/verify there."""
    host, _, folder = target.partition(":")
    if not host or not folder:
        raise SystemExit("--remote wants HOST:DIR, e.g. o6:openclaw-verify")
    source = source_tar()
    with tarfile.open(source, "a") as tar:  # the exact list, for the far side's sandboxes
        names = "\n".join(source_files()).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST)
        info.size = len(names)
        import io
        tar.addfile(info, io.BytesIO(names))
    quoted = shlex.quote(folder)
    # a plain copy (no .git): the remote side then sends its sandboxes the fallback file list
    unpack = (f"mkdir -p {quoted} && cd {quoted} && rm -rf .git && "
              "find . -path ./.cache -prune -o -type f -print0 | xargs -0 rm -f; tar -x -f -")
    with source.open("rb") as stdin:
        sent = subprocess.run(["ssh", host, unpack], stdin=stdin)
    if sent.returncode != 0:
        raise SystemExit(f"copying to {target} failed")
    print(f"== on {host} ({folder})", flush=True)
    return subprocess.run(["ssh", host, f"cd {quoted} && python3 verify/host.py {shlex.join(argv)}"]).returncode


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


def scenario_setting(path: Path, key: str, default: int) -> int:
    """A top-level number in a scenario ("timeout:", "retries:"), read
    without YAML, which the host may not have."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}:"):
            return int(line.split(":", 1)[1].split("#")[0].strip())
    return default


def scenario_timeout(path: Path) -> int:
    return scenario_setting(path, "timeout", 900)


def run_with_retries(tag, env, network, hosts, path: Path, source: Path) -> dict:
    """Each attempt in a fresh sandbox; earlier failures are kept in the result."""
    earlier = []
    for attempt in range(1, scenario_setting(path, "retries", 0) + 2):
        outcome = run_scenario(tag, env, network, hosts, path, source)
        outcome["attempts"] = attempt
        if outcome.get("status") != "fail":
            break
        if attempt <= scenario_setting(path, "retries", 0):
            earlier.append(f"attempt {attempt}, step {outcome.get('step', '?')}: {outcome.get('reason', '')}")
    outcome["earlier_failures"] = earlier
    return outcome


def run_platform(tag, env, network, hosts, source: Path, long_tokens: int) -> dict:
    cmd = sandbox_cmd(tag, env, network, hosts, [])
    cmd[-1] = UNPACK + f"exec python /src/verify/platform_check.py --long-prompt-tokens {int(long_tokens)}"
    with source.open("rb") as stdin:
        result = sh(cmd, timeout=3600, stdin=stdin)
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    try:
        return json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        return {"facts": {}, "metrics": {}, "checks": [{"name": "platform check", "status": "fail", "seconds": 0,
                                                       "detail": (result.stderr or result.stdout)[-800:]}]}


def run_gold(tag, env, network, hosts, source: Path) -> dict:
    prefix = f"verify_{int(time.time())}_{secrets.token_hex(3)}_"
    cmd = sandbox_cmd(tag, env, network, hosts, [])
    cmd[-1] = UNPACK + f"exec python /src/verify/gold_check.py --prefix {prefix}"
    with source.open("rb") as stdin:
        result = sh(cmd, timeout=7200, stdin=stdin)
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    try:
        return json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        return {"error": (result.stderr or result.stdout)[-1500:]}


def judge_gold(run: dict, baseline: dict | None) -> tuple[bool, list[str], list[str]]:
    """(passed, report lines, warnings) for a gold run against the minimums
    and this machine's baseline."""
    scores, minimums, tolerances = run["scores"], run["minimums"], run["tolerances"]
    failures, rows, warnings = [], [], []
    base_scores = (baseline or {}).get("scores") or {}
    for key in ("retrieval", "answers", "ocr"):
        value = scores.get(key)
        if value is None:
            rows.append(f"| {key} | -- | -- | not run here |")
            continue
        verdict = "ok"
        if value < minimums[key]:
            verdict = f"**below the minimum {minimums[key]:.0%}**"
            failures.append(key)
        before = base_scores.get(key)
        if before is not None and value < before - tolerances[key]:
            verdict = f"**dropped from {before:.0%}** (tolerance {tolerances[key]:.0%})"
            failures.append(key)
        rows.append(f"| {key} | {value:.0%} | {f'{before:.0%}' if before is not None else 'none yet'} | {verdict} |")
    for key in sorted(k for k in scores if k.startswith(("answers_", "retrieval_"))):
        before = base_scores.get(key)
        rows.append(f"| {key} | {scores[key]:.0%} | {f'{before:.0%}' if before is not None else '--'} | |")
    if baseline:
        for key, value in (run.get("timing") or {}).items():
            before = (baseline.get("timing") or {}).get(key)
            if value and before and value > before * tolerances.get("slower", 1.5):
                warnings.append(f"{key}: {value}s, baseline {before}s")
        old = {q["id"]: q for q in baseline.get("questions") or []}
        newly = [q["id"] for q in run["questions"] if not q["right"] and old.get(q["id"], {}).get("right")]
        if newly:
            warnings.append("newly wrong: " + ", ".join(newly))
        if baseline.get("model") != run.get("model"):
            warnings.append(f"the model changed ({baseline.get('model')} -> {run.get('model')}): "
                            "consider --accept once the new numbers look right")
    return not failures, rows, warnings


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
    parser.add_argument("mode", nargs="?", default="standard",
                        choices=["quick", "platform", "scenarios", "standard", "gold", "full", "coverage"])
    parser.add_argument("--remote", default="", help="HOST:DIR -- copy the files there and run bin/verify on that host")
    parser.add_argument("--accept", action="store_true", help="gold: save this run as the baseline")
    parser.add_argument("--platform", default="", help="a profile name in verify/platforms/ (default: detect)")
    parser.add_argument("--only", nargs="*", default=[], help="scenario names (file stems)")
    args = parser.parse_args(argv)
    if args.remote:
        forward = [a for a in argv if a != "--remote" and a != args.remote]
        return remote(args.remote, forward)
    if args.mode == "coverage":
        commands, templates = coverage()
        print("Bot commands without a scenario: " + (", ".join(f"/{c}" for c in commands) or "none"))
        print("Check-in templates without a scenario: " + (", ".join(templates) or "none"))
        print("Not run in a sandbox: " + "; ".join(f"/{c} ({why})" for c, why in COVERAGE_EXEMPT.items()))
        return 0
    config = local_config()
    tag = image()
    source = source_tar()
    lines = [f"# OpenClaw verify -- {time.strftime('%Y-%m-%d %H:%M %Z')} -- {args.mode}", ""]
    ok = True
    if args.mode in ("quick", "standard", "full"):
        print("== unit tests and ci_validate", flush=True)
        passed, detail = run_quick(tag, source)
        print(f"   {detail}", flush=True)
        lines += [f"Unit tests and ci_validate: {detail}", ""]
        ok &= passed
    if args.mode in ("platform", "standard", "full") and ok:
        facts = host_facts()
        profile = pick_profile(facts, args.platform or config.get("VERIFY_PLATFORM", ""))
        name, env, network, hosts = reference(config)
        print(f"== platform: {profile['name']} ({facts.get('board') or facts['arch']}"
              f"{', ' + facts['gpu'] if facts.get('gpu') else ''}; services from {name})", flush=True)
        outcome = run_platform(tag, env, network, hosts, source, (profile.get("run") or {}).get("long_prompt_tokens", 6000))
        lines += [f"## Platform: {profile['name']} -- {profile.get('description', '')}", "",
                  f"Host: {facts.get('board') or '?'} · {facts['arch']} · {facts['cpus']} CPUs"
                  f"{' · ' + str(facts['memory_gb']) + ' GB' if facts.get('memory_gb') else ''}"
                  f"{' · ' + facts['gpu'] if facts.get('gpu') else ''}",
                  f"Model: {outcome['facts'].get('model', '?')} on {outcome['facts'].get('model_server', '?')}, "
                  f"context {outcome['facts'].get('server_context') or '?'}", "",
                  "| Check | Result | Time | Detail |", "| --- | --- | --- | --- |"]
        for check in outcome["checks"]:
            ok &= check["status"] != "fail"
            print(f"   {check['status'].upper():4} {check['name']}  {check['seconds']}s  {check['detail'][:150]}", flush=True)
            lines.append(f"| {check['name']} | {'**FAIL**' if check['status'] == 'fail' else check['status']} | "
                         f"{check['seconds']}s | {check['detail'][:200].replace('|', '/')} |")
        lines += ["", "| Measure | Value | Against the profile |", "| --- | --- | --- |"]
        for metric, value, verdict in compare(outcome.get("metrics") or {}, profile.get("thresholds") or {}):
            print(f"   {metric}: {value} -- {verdict}", flush=True)
            lines.append(f"| {metric} | {value} | {verdict} |")
        lines.append("")
    if args.mode in ("scenarios", "standard", "full") and (ok or args.mode == "scenarios"):
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
            outcome = run_with_retries(tag, env, network, hosts, path, source)
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
            for note in outcome.get("earlier_failures") or []:
                print("        earlier: " + note.replace("\n", "\n        ")[:1200], flush=True)
                lines.append(f"|  | earlier attempt | | {note.splitlines()[0][:160].replace('|', '/')} |")
            lines.append(f"| {path.stem} | {'**FAIL**' if status == 'fail' else status}{retry} | "
                         f"{outcome.get('seconds', 0)}s | {detail.replace('|', '/')} |")
    if args.mode in ("gold", "full") and (ok or args.mode == "gold"):
        facts = host_facts()
        profile = pick_profile(facts, args.platform or config.get("VERIFY_PLATFORM", ""))
        name, env, network, hosts = reference(config)
        print(f"== gold set ({profile['name']}; services from {name}) -- this takes a while", flush=True)
        run = run_gold(tag, env, network, hosts, source)
        if "error" in run:
            print(f"   FAILED: {run['error'][-800:]}", flush=True)
            lines += ["## Gold set", "", "**failed to run**", ""]
            ok = False
        else:
            baseline_path = REPORTS / f"baseline-{profile['name']}.json"
            baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else None
            passed, rows, warnings = judge_gold(run, baseline)
            ok &= passed
            lines += [f"## Gold set ({profile['name']}, {run['model']})", "",
                      "| Score | This run | Baseline | |", "| --- | --- | --- | --- |", *rows, ""]
            for row in rows:
                print("   " + row.strip("| ").replace(" | ", "  "), flush=True)
            timing = run.get("timing") or {}
            print(f"   answer {timing.get('answer_seconds_median')}s median, image {timing.get('ocr_seconds_median')}s median",
                  flush=True)
            for warning in warnings:
                print(f"   warning: {warning}", flush=True)
                lines.append(f"- warning: {warning}")
            for q in run["questions"]:
                if not q["right"]:
                    lines.append(f"- wrong: {q['id']} ({'document sent' if q['retrieved'] else 'document not sent'})"
                                 f" -- {q['answer'][:120].replace(chr(10), ' ')}")
            REPORTS.mkdir(parents=True, exist_ok=True)
            (REPORTS / f"{time.strftime('%Y%m%d-%H%M%S')}-gold.json").write_text(
                json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
            if args.accept or (baseline is None and passed):
                baseline_path.write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"   saved as the baseline for {profile['name']}", flush=True)
                lines.append(f"- saved as the baseline for {profile['name']}")
    if args.mode in ("standard", "full"):
        commands, templates = coverage()
        if commands or templates:
            note = ("not covered by any scenario: " + ", ".join([f"/{c}" for c in commands] + templates))
            print(f"   coverage: {note}", flush=True)
            lines += ["", f"Coverage: {note}"]
    REPORTS.mkdir(parents=True, exist_ok=True)
    report = REPORTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.mode}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{'All passed' if ok else 'FAILED'} -- report: {report.relative_to(REPO)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
