#!/usr/bin/env python3
"""Host-side maintenance: nightly backup and a weekly L3 e2e run.

    python3 scripts/openclaw_maintenance.py backup
    python3 scripts/openclaw_maintenance.py e2e [--bot lc9-dgx2-apa | --container openclaw-telegram]

backup -- everything that exists only on this host, into
OPENCLAW_BACKUP_DIR/<YYYY-MM-DD_HHMM>/ (default ~/openclaw-backups):
  * a Qdrant snapshot of every collection (memory, knowledge, categories,
    the English bot's progress and word lists),
  * files.tar.gz: each profile's workspace (documents, category files, the
    night-ritual journal, schedules state...) minus what can be rebuilt or
    re-downloaded (the dictionary, the English bot's weekly audio, voice
    messages, staging), plus profiles/*/.env and the Gateway state.
  The .env files hold bot tokens, so the backup folder is private (0700).
  Keeps the newest OPENCLAW_BACKUP_KEEP (default 14) backups.

e2e -- runs scripts/e2e_run.py inside a bot's Telegram container and keeps
the report in .cache/e2e-latest.md.

Both write a status file under .cache/ that the watchdog's weekly summary
reports, and alert on Telegram (same sender as the watchdog) on failure.
Standard library only; suggested crontab:

    15 3 * * * cd /path/to/repo && /usr/bin/python3 scripts/openclaw_maintenance.py backup >> .cache/openclaw-maintenance.log 2>&1
    40 3 * * 1 cd /path/to/repo && /usr/bin/python3 scripts/openclaw_maintenance.py e2e >> .cache/openclaw-maintenance.log 2>&1
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import openclaw_watchdog as watchdog  # noqa: E402

BACKUP_DIR = Path(os.environ.get("OPENCLAW_BACKUP_DIR", str(Path.home() / "openclaw-backups")))
BACKUP_KEEP = int(os.environ.get("OPENCLAW_BACKUP_KEEP", "14"))
QDRANT_URL = os.environ.get("OPENCLAW_BACKUP_QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
BACKUP_STATUS = ROOT / ".cache" / "openclaw-backup-status.json"
E2E_STATUS = ROOT / ".cache" / "openclaw-e2e-status.json"
E2E_REPORT = ROOT / ".cache" / "e2e-latest.md"
BACKUP_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}$")

# Rebuildable or short-lived -- not worth a nightly copy.
EXCLUDE_PARTS = {"dictionary", "english_bot", "audio", ".staging", "exports", "tts", "__pycache__"}


def _write_status(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _alert(text: str) -> None:
    env = watchdog.find_sender_env()
    if env is None:
        return
    host = os.uname().nodename if hasattr(os, "uname") else "host"
    try:
        watchdog.send(env, f"OpenClaw maintenance · {host}\n{text}")
    except Exception as exc:  # noqa: BLE001
        print(f"could not send alert: {exc}", file=sys.stderr)


def _qdrant(method: str, path: str) -> dict:
    request = urllib.request.Request(f"{QDRANT_URL}{path}", method=method)
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read() or b"{}")


def snapshot_collections(target: Path) -> list[str]:
    target.mkdir(parents=True, exist_ok=True)
    names = [c["name"] for c in _qdrant("GET", "/collections")["result"]["collections"]]
    for name in names:
        snapshot = _qdrant("POST", f"/collections/{name}/snapshots?wait=true")["result"]["name"]
        try:
            with urllib.request.urlopen(f"{QDRANT_URL}/collections/{name}/snapshots/{snapshot}", timeout=600) as src, \
                    (target / f"{name}.snapshot").open("wb") as dst:
                shutil.copyfileobj(src, dst)
        finally:
            _qdrant("DELETE", f"/collections/{name}/snapshots/{snapshot}")
    return names


def _keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = set(Path(info.name).parts)
    if parts & EXCLUDE_PARTS or info.name.endswith(".tmp"):
        return None
    return info


def archive_files(target: Path, root: Path = ROOT) -> None:
    with tarfile.open(target, "w:gz") as tar:
        for profile in sorted((root / "profiles").glob("*")):
            if (profile / "workspace").is_dir():
                tar.add(profile / "workspace", arcname=f"profiles/{profile.name}/workspace", filter=_keep)
            if (profile / ".env").is_file():
                tar.add(profile / ".env", arcname=f"profiles/{profile.name}/.env")
        for extra in (root / ".env", root / "gateway-data" / "state"):
            if extra.exists():
                tar.add(extra, arcname=str(extra.relative_to(root)), filter=_keep)


def prune_backups(folder: Path, keep: int) -> list[str]:
    backups = sorted(p for p in folder.iterdir() if p.is_dir() and BACKUP_NAME.match(p.name)) if folder.is_dir() else []
    removed = []
    for old in backups[:-keep] if keep else backups:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    return removed


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def backup() -> int:
    started = time.time()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    target = BACKUP_DIR / time.strftime("%Y-%m-%d_%H%M")
    try:
        target.mkdir()
        collections = snapshot_collections(target / "qdrant")
        archive_files(target / "files.tar.gz")
        removed = prune_backups(BACKUP_DIR, BACKUP_KEEP)
    except Exception as exc:  # noqa: BLE001
        _write_status(BACKUP_STATUS, {"at": started, "ok": False, "error": str(exc)})
        _alert(f"The nightly backup failed: {exc}")
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    size = _size(target)
    _write_status(BACKUP_STATUS, {"at": started, "ok": True, "path": str(target), "bytes": size,
                                  "collections": len(collections), "seconds": round(time.time() - started, 1)})
    print(f"backup ok: {target} {size} bytes, {len(collections)} collections, pruned {removed}")
    return 0


def e2e(bot: str, container: str = "") -> int:
    container = container or f"openclaw-telegram-{bot}"
    started = time.time()
    with (ROOT / "scripts" / "e2e_run.py").open("rb") as script:
        run = subprocess.run(["docker", "exec", "-i", container, "python3", "-"], stdin=script,
                             capture_output=True, timeout=1200)
    report = run.stdout.decode("utf-8", errors="replace")
    E2E_REPORT.parent.mkdir(parents=True, exist_ok=True)
    E2E_REPORT.write_text(report, encoding="utf-8")
    match = re.search(r"(\d+)/(\d+) passed", report)
    passed, total = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    ok = run.returncode == 0 and total > 0
    _write_status(E2E_STATUS, {"at": started, "ok": ok, "bot": bot, "passed": passed, "total": total})
    if not ok:
        failed = [line.split("|")[1].strip() for line in report.splitlines() if "**FAIL**" in line]
        _alert(f"The weekly e2e run on {bot} failed ({passed}/{total}): {', '.join(failed) or 'no report'}")
    print(f"e2e {bot}: {passed}/{total} ({'ok' if ok else 'FAILED'})")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("backup", "e2e"):
        print(__doc__)
        return 2
    if argv[0] == "backup":
        return backup()
    bot = argv[argv.index("--bot") + 1] if "--bot" in argv else os.environ.get("OPENCLAW_E2E_BOT", "lc9-dgx2-apa")
    container = argv[argv.index("--container") + 1] if "--container" in argv else os.environ.get("OPENCLAW_E2E_CONTAINER", "")
    return e2e(bot, container)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
