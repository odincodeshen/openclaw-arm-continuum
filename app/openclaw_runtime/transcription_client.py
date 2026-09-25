from dataclasses import dataclass
from pathlib import Path

from openclaw_runtime.config import Settings
from openclaw_runtime.http_client import request_json
from openclaw_runtime.whisper_paths import to_whisper_path


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TimestampedTranscript:
    text: str
    duration: float
    segments: list[TranscriptSegment]


class TranscriptionClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def transcribe(self, audio_path: Path) -> str:
        response = request_json(
            "POST",
            f"{self.settings.whisper_base_url}/transcribe",
            {"path": str(to_whisper_path(audio_path, self.settings))},
            timeout=self.settings.whisper_timeout,
        )
        return str(response.get("text") or "").strip()

    def transcribe_with_segments(self, audio_path: Path) -> TimestampedTranscript:
        """Same endpoint as transcribe(), but keeps the per-segment start/end
        timestamps the plain text-only transcribe() throws away -- needed to
        pick a real window (Monday) and quote a real sub-stretch (Tuesday)
        instead of guessing at prose alone."""
        response = request_json(
            "POST",
            f"{self.settings.whisper_base_url}/transcribe",
            {"path": str(to_whisper_path(audio_path, self.settings))},
            timeout=self.settings.whisper_timeout,
        )
        segments = [
            TranscriptSegment(
                start=float(item.get("start") or 0.0),
                end=float(item.get("end") or 0.0),
                text=str(item.get("text") or "").strip(),
            )
            for item in response.get("segments") or []
        ]
        return TimestampedTranscript(
            text=str(response.get("text") or "").strip(),
            duration=float(response.get("duration") or 0.0),
            segments=segments,
        )
