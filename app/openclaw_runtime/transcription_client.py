from pathlib import Path

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import request_json


class TranscriptionClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def transcribe(self, audio_path: Path) -> str:
        response = request_json(
            "POST",
            f"{self.settings.whisper_base_url}/transcribe",
            {"path": str(audio_path)},
            timeout=self.settings.whisper_timeout,
        )
        return str(response.get("text") or "").strip()
