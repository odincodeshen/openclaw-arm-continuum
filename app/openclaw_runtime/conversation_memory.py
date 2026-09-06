"""Per-chat conversational memory for ordinary Telegram chat.

Ordinary chat (``ChatAgent``) is otherwise single-turn. This module persists
the recent user/assistant turns for a ``chat_id`` and replays a bounded window
of them on the next request. It is deliberately kept separate from:

* task history -- bounded observability metadata, never conversation bodies;
* Qdrant / the explicit ``/mem`` and ``/rag`` knowledge base -- ordinary chat is
  never promoted there automatically.

Storage: one JSON file per ``chat_id`` under
``settings.conversation_store_path``. Reset is a file delete; retention is an
age check on ``updated_at``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from openclaw_runtime.config import Settings

_STORE_VERSION = 1


class ConversationMemory:
    def __init__(self, settings: Settings) -> None:
        self.enabled = bool(settings.conversation_memory_enabled)
        self._dir = Path(settings.conversation_store_path)
        self._max_turns = max(0, int(settings.conversation_history_turns))
        self._max_chars = max(0, int(settings.conversation_context_chars))
        self._retention_seconds = max(0, int(settings.conversation_retention_hours)) * 3600

    # -- paths / io ----------------------------------------------------------
    def _path(self, chat_id: int) -> Path:
        return self._dir / f"{int(chat_id)}.json"

    def _read(self, chat_id: int) -> dict:
        try:
            data = json.loads(self._path(chat_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": _STORE_VERSION, "turns": []}
        if not isinstance(data, dict) or not isinstance(data.get("turns"), list):
            return {"version": _STORE_VERSION, "turns": []}
        return data

    def _write(self, chat_id: int, data: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(chat_id)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _expired(self, data: dict) -> bool:
        if not self._retention_seconds:
            return False
        return (time.time() - data.get("updated_at", 0)) > self._retention_seconds

    # -- public api --------------------------------------------------------
    def load(self, chat_id: int | None) -> list[dict] | None:
        """Recent turns as OpenAI-style ``[{"role", "content"}]``, oldest first.

        ``None`` means "nothing to inject" (disabled, no history, or expired).
        """
        if not self.enabled or chat_id is None or self._max_turns == 0:
            return None
        data = self._read(chat_id)
        turns = data.get("turns", [])
        if not turns or self._expired(data):
            return None

        window = turns[-(self._max_turns * 2):]
        if self._max_chars:
            while len(window) > 2 and sum(len(t.get("content", "")) for t in window) > self._max_chars:
                del window[0:2]

        cleaned = [
            {"role": t["role"], "content": t["content"]}
            for t in window
            if t.get("role") in ("user", "assistant") and t.get("content")
        ]
        # Only replay a clean alternating history that starts with a user turn.
        if cleaned and cleaned[0]["role"] != "user":
            cleaned = cleaned[1:]
        return cleaned or None

    def record(self, chat_id: int | None, user_text: str, assistant_text: str) -> None:
        if not self.enabled or chat_id is None or self._max_turns == 0:
            return
        user_text = (user_text or "").strip()
        assistant_text = (assistant_text or "").strip()
        if not user_text or not assistant_text:
            return

        data = self._read(chat_id)
        if self._expired(data):
            data = {"version": _STORE_VERSION, "turns": []}
        turns = data.get("turns", [])
        now = int(time.time())
        turns.append({"role": "user", "content": user_text, "ts": now})
        turns.append({"role": "assistant", "content": assistant_text, "ts": now})

        # Keep a bit more than we replay, so retention/debugging still has context.
        keep = max(self._max_turns * 4, 4)
        data["turns"] = turns[-keep:]
        data["updated_at"] = now
        data["version"] = _STORE_VERSION
        self._write(chat_id, data)

    def clear(self, chat_id: int | None) -> bool:
        """Delete a chat's stored conversation. Returns True if a file was removed."""
        if chat_id is None:
            return False
        try:
            self._path(chat_id).unlink()
            return True
        except (FileNotFoundError, OSError):
            return False

    def sweep(self) -> int:
        """Best-effort delete of expired per-chat files; returns count removed."""
        if not self._retention_seconds or not self._dir.is_dir():
            return 0
        removed = 0
        cutoff = time.time() - self._retention_seconds
        for path in self._dir.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
        return removed
