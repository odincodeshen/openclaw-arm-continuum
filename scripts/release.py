#!/usr/bin/env python3
"""Cut a release only after the checks pass.

    bin/release v1.28 --title "Check-in skip days" --notes notes.md
    bin/release v1.28 --title "..." --notes notes.md --dry-run   # checks only

In order, stopping at the first failure:

1. git: on main, nothing uncommitted, in step with origin, tag not used yet.
2. Unit tests and scripts/ci_validate.py.
3. L3 e2e (scripts/e2e_run.py) in the bots named in .release.local:
   - required ones must pass;
   - optional ones only warn;
   - a failed run is retried once, as a CPU-only model's answer can vary.
4. Privacy scan of everything since the previous tag:
   - chat IDs and tokens found in profiles/*/.env;
   - home paths, e-mail addresses and private IPs;
   - tracked files that must stay local (.env, compose.persona.<bot>.yaml).
5. The version in README.md, README.zh-TW.md and docs/FUTURE_TODO.md, a
   "release: <tag> - <title>" commit, push, and a GitHub release whose notes
   end with the results of the checks above.

Which bots to test is local, so bot names and hosts stay out of the public
repo. .release.local (gitignored) holds KEY=VALUE lines:

    RELEASE_E2E=openclaw-telegram-bot-a                  # required, space-separated
    RELEASE_E2E_OPTIONAL=ssh:o6:openclaw-telegram-bot-b  # warn only; ssh:<host>:<container> runs remotely
    RELEASE_PYTHON=/path/to/venv/bin/python              # has pytest; default: python3
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VERSION_FILES = (
    ("README.md", r"Version: `(v[\d.]+)`", "Version: `{}`"),
    ("README.zh-TW.md", r"版本：`(v[\d.]+)`", "版本：`{}`"),
    ("docs/FUTURE_TODO.md", r"Current release: \*\*(v[\d.]+)\*\*\.", "Current release: **{}**."),
)
LOCAL_ONLY = (r"(^|/)\.env$", r"^profiles/[^/]+/\.env$", r"^compose\.persona\.(?!.*example)[^/]+\.ya?ml$",
              r"(^|/)\.release\.local$")
ALLOWED_EMAILS = ("noreply@anthropic.com", "noreply@github.com")
ALLOWED_IPS = ("172.17.0.1", "127.0.0.1", "0.0.0.0")


class Failed(Exception):
    pass


def run(cmd: list[str] | str, *, check: bool = True, capture: bool = True, stdin=None, shell: bool = False,
        timeout: int = 1800, env: dict | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=REPO, text=True, capture_output=capture, stdin=stdin, shell=shell,
                            timeout=timeout, env={**os.environ, **env} if env else None)
    if check and result.returncode != 0:
        tail = ((result.stdout or "") + (result.stderr or ""))[-1500:]
        raise Failed(f"`{cmd if isinstance(cmd, str) else ' '.join(cmd)}` failed:\n{tail}")
    return result


def git(*args: str) -> str:
    return run(["git", *args]).stdout.strip()


def local_config() -> dict[str, str]:
    path = REPO / ".release.local"
    config = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip('"')
    return config


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag.lstrip("v").split("."))


# --- 1. git -------------------------------------------------------------------
def check_git(tag: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+(\.\d+)?", tag):
        raise Failed(f"tag must look like v1.28, got {tag!r}")
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise Failed("not on main")
    if git("status", "--porcelain"):
        raise Failed("uncommitted changes; commit or stash them first")
    run(["git", "fetch", "--quiet", "--tags", "origin"])
    if git("rev-list", "--count", "HEAD..origin/main") != "0":
        raise Failed("origin/main has commits this checkout doesn't; pull first")
    if tag in git("tag", "--list").split():
        raise Failed(f"tag {tag} already exists")
    tags = sorted((t for t in git("tag", "--list", "v*").split() if re.fullmatch(r"v[\d.]+", t)), key=version_key)
    previous = tags[-1] if tags else ""
    if previous and version_key(tag) <= version_key(previous):
        raise Failed(f"{tag} is not after the latest release {previous}")
    return previous


# --- 2. tests -------------------------------------------------------------------
def check_tests(python: str) -> str:
    run([python, "-m", "pytest", "-q", "tests"], timeout=3600)
    count = run([python, "-m", "pytest", "--collect-only", "-q", "tests"], check=False).stdout
    collected = len([line for line in count.splitlines() if "::" in line])
    # no ruff cache: a container may have left a root-owned one in the checkout
    run([python, "scripts/ci_validate.py"], env={"RUFF_NO_CACHE": "true",
                                                  "PATH": f"{Path(python).parent}{os.pathsep}{os.environ['PATH']}"})
    return f"{collected} tests pass; ci_validate passes"


# --- 3. e2e ---------------------------------------------------------------------
def e2e_once(target: str) -> tuple[bool, str]:
    script = (REPO / "scripts" / "e2e_run.py").open("rb")
    if target.startswith("ssh:"):
        _, host, container = target.split(":", 2)
        cmd = ["ssh", host, f"docker exec -i {container} python3 -"]
    else:
        container = target
        cmd = ["docker", "exec", "-i", container, "python3", "-"]
    with script:
        result = subprocess.run(cmd, cwd=REPO, stdin=script, capture_output=True, timeout=1800)
    out = result.stdout.decode("utf-8", "replace")
    summary = next((line for line in out.splitlines() if re.search(r"\d+/\d+ passed", line)), "no report")
    failed = [line.split("|")[1].strip() for line in out.splitlines() if "**FAIL**" in line]
    detail = summary.split("·")[0].strip() + (f" (failed: {', '.join(failed)})" if failed else "")
    return result.returncode == 0, detail


def check_e2e(required: list[str], optional: list[str]) -> list[str]:
    lines = []
    for target, must_pass in [(t, True) for t in required] + [(t, False) for t in optional]:
        name = target.split(":")[-1]
        ok, detail = e2e_once(target)
        tries = 1
        if not ok:
            print(f"   {name}: {detail} -- retrying once", flush=True)
            ok, detail = e2e_once(target)
            tries = 2
        note = "" if tries == 1 else " on the second run"
        print(f"   {name}: {detail}{note}", flush=True)
        if not ok and must_pass:
            raise Failed(f"e2e failed in {name}: {detail}")
        lines.append(f"e2e {'(optional) ' if not must_pass else ''}{'passed' if ok else 'FAILED'}: "
                     f"{detail}{note}")
    if not lines:
        lines.append("e2e: no bots configured (RELEASE_E2E in .release.local)")
    return lines


# --- 4. privacy -----------------------------------------------------------------
def private_values() -> list[str]:
    values = set()
    for env in REPO.glob("profiles/*/.env"):
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            value = value.strip().strip('"')
            if not value:
                continue
            if re.search(r"CHAT_IDS|OWNER", key):
                values.update(v for v in re.findall(r"-?\d{6,}", value))
            elif re.search(r"TOKEN|SECRET|PASSWORD|API_KEY", key) and len(value) >= 12:
                values.add(value)
    return sorted(values)


def check_privacy(previous: str) -> str:
    base = previous or git("rev-list", "--max-parents=0", "HEAD").splitlines()[0]
    diff = git("diff", f"{base}..HEAD", "--unified=0", "--no-color")
    added = []
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            added.append((current, line[1:]))
    problems = []
    secrets = private_values()
    home = str(Path.home())
    for path, line in added:
        if any(secret in line for secret in secrets):
            problems.append(f"{path}: a chat ID or token from profiles/*/.env")
        if home in line:
            problems.append(f"{path}: a home directory path")
        for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", line):
            if email not in ALLOWED_EMAILS and not email.endswith(("@example.com", "@users.noreply.github.com")):
                problems.append(f"{path}: an e-mail address")
        for ip in re.findall(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b", line):
            if ip not in ALLOWED_IPS:
                problems.append(f"{path}: a private IP address")
    tracked = git("ls-files").splitlines()
    problems += [f"{path}: must stay local but is tracked" for path in tracked
                 if any(re.search(p, path) for p in LOCAL_ONLY)]
    if problems:
        raise Failed("privacy scan:\n" + "\n".join(f"  - {p}" for p in sorted(set(problems))))
    return f"privacy scan clean ({len(added)} added lines since {base[:12] if not previous else previous})"


# --- 5. release -----------------------------------------------------------------
def bump_version(tag: str, previous: str) -> list[str]:
    changed = []
    for name, pattern, template in VERSION_FILES:
        path = REPO / name
        text = path.read_text(encoding="utf-8")
        new = re.sub(pattern, template.format(tag), text, count=1)
        if previous:
            new = new.replace(f"re-baselined against {previous}:", f"re-baselined against {tag}:")
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(name)
    return changed


def publish(tag: str, title: str, notes: str, results: list[str], changed: list[str]) -> str:
    if changed:
        run(["git", "add", *changed])
        message = (f"release: {tag} - {title}\n\n"
                   "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n")
        run(["git", "commit", "-q", "-m", message])
    run(["git", "push", "-q", "origin", "main"])
    body = notes.rstrip() + "\n\n## Release checks\n\n" + "\n".join(f"- {line}" for line in results) + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        notes_path = handle.name
    url = run(["gh", "release", "create", tag, "--target", "main", "--title",
               f"OpenClaw Arm Continuum {tag} - {title}", "--notes-file", notes_path]).stdout.strip()
    Path(notes_path).unlink(missing_ok=True)
    return url


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Cut a release after tests, e2e and a privacy scan pass.")
    parser.add_argument("tag", help="e.g. v1.28")
    parser.add_argument("--title", required=True, help='e.g. "Check-in skip days"')
    parser.add_argument("--notes", required=True, help="Markdown release notes file")
    parser.add_argument("--dry-run", action="store_true", help="run the checks only")
    parser.add_argument("--skip-e2e", action="store_true", help="for a docs-only release")
    args = parser.parse_args(argv)
    config = local_config()
    notes_file = Path(args.notes)
    if not notes_file.exists():
        print(f"release: notes file {notes_file} not found", file=sys.stderr)
        return 2
    results = []
    try:
        step("git")
        previous = check_git(args.tag)
        print(f"   previous release: {previous or 'none'}")
        step("unit tests and ci_validate")
        results.append(check_tests(config.get("RELEASE_PYTHON", "python3")))
        print(f"   {results[-1]}")
        step("e2e")
        if args.skip_e2e:
            results.append("e2e skipped (--skip-e2e)")
            print("   skipped")
        else:
            results += check_e2e(config.get("RELEASE_E2E", "").split(), config.get("RELEASE_E2E_OPTIONAL", "").split())
        step("privacy scan")
        results.append(check_privacy(previous))
        print(f"   {results[-1]}")
    except Failed as exc:
        print(f"\nrelease stopped: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("\nAll checks passed (dry run: nothing changed).")
        return 0
    step(f"release {args.tag}")
    changed = bump_version(args.tag, previous)
    try:
        url = publish(args.tag, args.title, notes_file.read_text(encoding="utf-8"), results, changed)
    except Failed as exc:
        print(f"\nrelease stopped while publishing: {exc}", file=sys.stderr)
        return 1
    print(f"   {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
