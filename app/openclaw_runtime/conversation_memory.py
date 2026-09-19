"""Per-chat conversational memory for ordinary Telegram chat.

Ordinary chat (``ChatAgent``) is otherwise single-turn. This module persists
the recent user/assistant turns for a ``chat_id`` and replays a bounded window
of them on the next request. It is deliberately kept separate from:

* task history -- bounded observability metadata, never conversation bodies;
* Qdrant / the explicit ``/mem`` and ``/rag`` knowledge base -- ordinary chat is
  never promoted there automatically.

When the sliding window overflows, the turns falling out of it are folded
into a running LLM-generated summary instead of being silently dropped (see
``_fold_into_summary``). ``/keep`` lets the user pin a fact that always rides
along regardless of window size. Both degrade gracefully to today's
hard-truncation behaviour when no ``llm`` is supplied or a summarization call
fails -- summarization is a quality improvement, never a required dependency.

Storage: one JSON file per ``chat_id`` under
``settings.conversation_store_path``. Reset (``/new``) is a file delete --
it wipes turns, summary, and pinned facts together, since it means "start
over"; anything meant to survive that belongs in ``/mem`` instead. Retention
is an age check on ``updated_at``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from openclaw_runtime.config import Settings

_STORE_VERSION = 2

_SUMMARY_PROMPT_HEADER = (
    "You are compressing an ongoing chat's history into a running summary so "
    "older turns can be dropped without losing context."
)


class ConversationMemory:
    def __init__(self, settings: Settings, llm=None) -> None:
        self.enabled = bool(settings.conversation_memory_enabled)
        self._dir = Path(settings.conversation_store_path)
        self._max_turns = max(0, int(settings.conversation_history_turns))
        self._max_chars = max(0, int(settings.conversation_context_chars))
        self._retention_seconds = max(0, int(settings.conversation_retention_hours)) * 3600
        self.llm = llm if settings.conversation_summary_enabled else None
        self._summary_max_tokens = max(1, int(settings.conversation_summary_max_tokens))
        self._summary_max_chars = max(1, int(settings.conversation_summary_max_chars))
        self._keep_max = max(0, int(settings.conversation_keep_max_items))

    # -- paths / io ----------------------------------------------------------
    def _path(self, chat_id: int) -> Path:
        return self._dir / f"{int(chat_id)}.json"

    def _empty_store(self) -> dict:
        return {"version": _STORE_VERSION, "turns": [], "summary": "", "pinned": []}

    def _read(self, chat_id: int) -> dict:
        try:
            data = json.loads(self._path(chat_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_store()
        if not isinstance(data, dict) or not isinstance(data.get("turns"), list):
            return self._empty_store()
        data.setdefault("summary", "")
        data.setdefault("pinned", [])
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
        """OpenAI-style ``[{"role", "content"}]``, oldest first: an optional
        leading synthetic pair carrying pinned facts / the rolling summary,
        then the recent raw turns.

        ``None`` means "nothing to inject" (disabled, no history, or expired).
        """
        if not self.enabled or chat_id is None or self._max_turns == 0:
            return None
        data = self._read(chat_id)
        if self._expired(data):
            return None

        context_pair = self._context_pair(data)

        window = data.get("turns", [])[-(self._max_turns * 2):]
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

        combined = context_pair + cleaned
        return combined or None

    def record(self, chat_id: int | None, user_text: str, assistant_text: str) -> None:
        if not self.enabled or chat_id is None or self._max_turns == 0:
            return
        user_text = (user_text or "").strip()
        assistant_text = (assistant_text or "").strip()
        if not user_text or not assistant_text:
            return

        data = self._read(chat_id)
        if self._expired(data):
            data = self._empty_store()
        turns = data.get("turns", [])
        now = int(time.time())
        turns.append({"role": "user", "content": user_text, "ts": now})
        turns.append({"role": "assistant", "content": assistant_text, "ts": now})

        window = self._max_turns * 2
        overflow = turns[:-window] if len(turns) > window else []
        if overflow:
            data["summary"] = self._fold_into_summary(data.get("summary", ""), overflow)
            turns = turns[-window:]

        data["turns"] = turns
        data["updated_at"] = now
        data["version"] = _STORE_VERSION
        self._write(chat_id, data)

    def pin(self, chat_id: int | None, fact: str) -> bool:
        """Pin a fact so it always rides along in load(), regardless of the
        sliding window. Survives until /new clears the whole conversation."""
        if not self.enabled or chat_id is None or self._keep_max == 0:
            return False
        fact = (fact or "").strip()
        if not fact:
            return False
        data = self._read(chat_id)
        if self._expired(data):
            data = self._empty_store()
        pinned = list(data.get("pinned") or [])
        pinned.append(fact)
        data["pinned"] = pinned[-self._keep_max :]
        data["updated_at"] = int(time.time())
        data["version"] = _STORE_VERSION
        self._write(chat_id, data)
        return True

    def preview(self, chat_id: int | None) -> dict | None:
        """Read-only snapshot for /history: pinned facts, the rolling
        summary, and the raw turn window, without mutating anything.
        ``None`` means nothing to show (disabled, no data yet, or expired)."""
        if not self.enabled or chat_id is None:
            return None
        data = self._read(chat_id)
        if self._expired(data):
            return None
        pinned = list(data.get("pinned") or [])
        summary = data.get("summary") or ""
        turns = list(data.get("turns") or [])
        if not pinned and not summary and not turns:
            return None
        return {"pinned": pinned, "summary": summary, "turns": turns}

    def clear(self, chat_id: int | None) -> bool:
        """Delete a chat's stored conversation (turns, summary, pinned facts).

        Returns True if a file was removed."""
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

    # -- summary / pinned context -------------------------------------------
    def _context_pair(self, data: dict) -> list[dict]:
        pinned = data.get("pinned") or []
        summary = data.get("summary") or ""
        if not pinned and not summary:
            return []
        parts = []
        if pinned:
            parts.append("Pinned facts:\n" + "\n".join(f"- {fact}" for fact in pinned))
        if summary:
            parts.append("Summary of earlier conversation:\n" + summary)
        note = "(context, not a new message)\n\n" + "\n\n".join(parts)
        return [
            {"role": "user", "content": note},
            {"role": "assistant", "content": "Noted, I'll keep that in mind."},
        ]

    def _fold_into_summary(self, old_summary: str, overflow: list[dict]) -> str:
        if self.llm is None:
            return old_summary
        exchange_text = "\n".join(
            f"{turn.get('role', '?')}: {turn.get('content', '')}" for turn in overflow
        )
        prompt = (
            f"{_SUMMARY_PROMPT_HEADER}\n\n"
            + (f"Existing summary:\n{old_summary}\n\n" if old_summary else "There is no existing summary yet.\n\n")
            + f"New exchange to fold in:\n{exchange_text}\n\n"
            "Write the updated summary: concise, factual, third person, no more "
            "than 120 words. Output only the summary text, no preamble."
        )
        try:
            updated = self.llm.chat(prompt, max_tokens=self._summary_max_tokens)
        except Exception:
            return old_summary
        updated = (updated or "").strip()
        if not updated:
            return old_summary
        return updated[: self._summary_max_chars]
