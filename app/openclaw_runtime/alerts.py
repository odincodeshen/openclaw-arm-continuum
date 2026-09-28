"""Operator alerts: tell the admin on Telegram when something goes wrong that
would otherwise only reach a log -- a failed /cron job, the Gateway going
away, an off-peak preparation or English-bot push failing, the memory
watcher unable to index.

Each problem has a key. The first failure for a key sends an alert; repeats
within OPENCLAW_ALERT_COOLDOWN_MINUTES stay quiet; when the key's check
passes again, a single "recovered" message follows. Sent by the bot whose
process hit the problem (its own token) to OPENCLAW_ALERT_CHAT_IDS, labelled
with OPENCLAW_RUNTIME_LABEL. No chat IDs = alerts off. State is per process
and in memory: a restart starts clean, and a restart on its own never
alerts.
"""

import threading
import time
from typing import Callable

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import request_json

MAX_DETAIL_CHARS = 600


def _telegram_sender(settings: Settings) -> Callable[[int, str], None]:
    def send(chat_id: int, text: str) -> None:
        request_json(
            "POST",
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            {"chat_id": chat_id, "text": text},
            timeout=20,
        )

    return send


class Alerter:
    def __init__(
        self,
        settings: Settings,
        send: Callable[[int, str], None] | None = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = print,
    ) -> None:
        self.chat_ids = sorted(settings.alert_chat_ids)
        self.label = settings.runtime_label
        self.cooldown = settings.alert_cooldown_minutes * 60
        self.send = send or _telegram_sender(settings)
        self.clock = clock
        self.log = log
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.chat_ids)

    def _deliver(self, text: str) -> None:
        for chat_id in self.chat_ids:
            try:
                self.send(chat_id, text)
            except Exception as exc:  # noqa: BLE001 - an alert must never break the caller
                self.log(f"[alert] could not send to {chat_id}: {exc}")

    def alert(self, key: str, summary: str, detail: str = "") -> bool:
        """Report a problem. Returns True if a message was sent (not muted by
        the cooldown)."""
        if not self.enabled:
            return False
        now = self.clock()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < self.cooldown:
                return False
            self._last_sent[key] = now
        detail = detail.strip()
        if len(detail) > MAX_DETAIL_CHARS:
            detail = detail[:MAX_DETAIL_CHARS] + "…"
        text = f"OpenClaw alert · {self.label}\n{summary}"
        if detail:
            text += f"\n\n{detail}"
        self.log(f"[alert] {key}: {summary}")
        self._deliver(text)
        return True

    def resolve(self, key: str, summary: str) -> bool:
        """The check behind ``key`` passed. Sends "recovered" only if an alert
        for it went out earlier; otherwise does nothing."""
        if not self.enabled:
            return False
        with self._lock:
            if self._last_sent.pop(key, None) is None:
                return False
        self.log(f"[alert] {key} recovered")
        self._deliver(f"OpenClaw recovered · {self.label}\n{summary}")
        return True
