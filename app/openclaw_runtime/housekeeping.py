"""Daily cleanup of files that only matter for a short while.

Each bot's Telegram container runs this for its own workspace (the files
are created by the container, so it's the one allowed to delete them):

- voice messages kept after transcription (inbox/audio/telegram),
- the English bot's weekly audio folders, except the newest two weeks
  (episode download, window and clips -- the week's text lives in Qdrant),
- export files already sent (.openclaw/exports),
- uploads left in staging that were never filed (.staging/telegram).

Everything indexed -- documents, categories, memory, the night ritual --
is left alone. The result is written to .openclaw/housekeeping.json for the
watchdog's weekly summary.
"""

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

WEEK_DIR = re.compile(r"^wk(\d+)$")


@dataclass
class CleanupResult:
    files: int = 0
    bytes: int = 0
    by_area: dict[str, int] = field(default_factory=dict)

    def add(self, area: str, size: int) -> None:
        self.files += 1
        self.bytes += size
        self.by_area[area] = self.by_area.get(area, 0) + size


def _older_files(folder: Path, cutoff: float):
    if not folder.is_dir():
        return
    for path in folder.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            yield path


def _remove(path: Path, area: str, result: CleanupResult) -> None:
    size = path.stat().st_size
    path.unlink(missing_ok=True)
    result.add(area, size)


def run_cleanup(
    workspace: Path,
    *,
    audio_days: int = 14,
    export_days: int = 7,
    staging_days: int = 7,
    keep_weeks: int = 2,
    now: float | None = None,
) -> CleanupResult:
    """workspace is the bot's /workspace (the parent of its inbox)."""
    now = now or time.time()
    day = 86400
    inbox = workspace / "inbox"
    result = CleanupResult()
    for path in list(_older_files(inbox / "audio" / "telegram", now - audio_days * day)):
        _remove(path, "voice", result)
    for path in list(_older_files(workspace / ".openclaw" / "exports", now - export_days * day)):
        _remove(path, "exports", result)
    for path in list(_older_files(inbox / ".staging" / "telegram", now - staging_days * day)):
        _remove(path, "staging", result)

    english = inbox / "english_bot"
    if english.is_dir():
        weeks = sorted(
            (int(match.group(1)), path)
            for path in english.iterdir()
            if path.is_dir() and (match := WEEK_DIR.match(path.name))
        )
        for _, folder in weeks[:-keep_weeks] if keep_weeks else weeks:
            for path in [p for p in folder.rglob("*") if p.is_file()]:
                _remove(path, "english audio", result)
            shutil.rmtree(folder, ignore_errors=True)
    return result


def write_status(workspace: Path, result: CleanupResult, now: float | None = None) -> None:
    path = workspace / ".openclaw" / "housekeeping.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"at": now or time.time(), "files": result.files, "bytes": result.bytes, "by_area": result.by_area}
        ),
        encoding="utf-8",
    )
