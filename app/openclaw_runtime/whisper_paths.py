from pathlib import Path

from openclaw_runtime.config import Settings


LOCAL_WORKSPACE_ROOT = "/workspace"


def to_whisper_path(local_path: Path, settings: Settings) -> Path:
    """Translate a path as this gateway container sees it (always mounted
    at /workspace, regardless of persona) into the equivalent path as the
    shared openclaw-whisper container sees it. One whisper container serves
    every bot persona, but each persona's gateway mounts a DIFFERENT host
    directory at its own /workspace -- whisper only sees a persona's files
    under a namespaced path (OPENCLAW_WHISPER_WORKSPACE_ROOT, e.g.
    /profiles/<persona>/workspace, see compose.yaml). Personas that haven't
    set whisper_workspace_root are left untranslated -- same behaviour as
    before this existed, not a regression for anyone not yet configured."""
    if not settings.whisper_workspace_root:
        return local_path
    local_str = str(local_path)
    if not local_str.startswith(LOCAL_WORKSPACE_ROOT):
        return local_path
    return Path(settings.whisper_workspace_root + local_str[len(LOCAL_WORKSPACE_ROOT):])
