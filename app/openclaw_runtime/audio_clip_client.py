from pathlib import Path

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import request_json


class AudioClipClient:
    """Calls the openclaw-whisper service's /clip endpoint. Audio decoding
    (the `av` dependency) lives in that service, not here -- the gateway and
    other stdlib-only runtime code can't import it directly."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def clip(self, input_path: Path, start_seconds: float, end_seconds: float, output_path: Path) -> float:
        response = request_json(
            "POST",
            f"{self.settings.whisper_base_url}/clip",
            {
                "path": str(input_path),
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "output_path": str(output_path),
            },
            timeout=self.settings.whisper_timeout,
        )
        return float(response.get("duration") or 0.0)
