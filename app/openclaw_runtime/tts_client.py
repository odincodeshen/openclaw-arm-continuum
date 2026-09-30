"""Client for the local pronunciation service (openclaw_tts_service.py)."""

import json
import urllib.request

from openclaw_runtime.config import Settings

ACCENTS = ("uk", "us")


class TtsClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.tts_base_url.rstrip("/")
        self.timeout = settings.tts_timeout

    def speak(self, text: str, accent: str, fmt: str = "ogg") -> bytes:
        """Audio of text in the given accent ("uk" or "us"): Ogg/Opus for a
        Telegram voice message, or fmt="mp3" (for Anki)."""
        request = urllib.request.Request(
            f"{self.base_url}/speak",
            data=json.dumps({"text": text, "accent": accent, "format": fmt}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()
