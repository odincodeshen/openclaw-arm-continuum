#!/usr/bin/env python3
"""Live smoke test for the vision (VLM) endpoint.

Builds the VisionClient exactly as the Telegram gateway does, then runs one
image description against the resolved endpoint. Use it to confirm the vision
path is reachable and actually reads images *before* relying on it.

Run it where the runtime environment is set (inside the container):

    docker compose exec openclaw-telegram python scripts/vision_smoke.py
    docker compose exec openclaw-telegram python scripts/vision_smoke.py /path/to/photo.jpg

With no argument it uses a tiny bundled red/green/blue test image and checks
that the model's description actually mentions the colours -- a real capability
probe, not just a plumbing check. Pass your own photo or screenshot for a
quality check you judge yourself.

Exit codes: 0 = endpoint answered; 1 = endpoint call failed or the model did
not read the bundled test image; 2 = bad usage / missing image.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from openclaw_runtime.config import load_settings  # noqa: E402
from openclaw_runtime.model_catalog import load_model_registry  # noqa: E402
from openclaw_runtime.model_client_factory import ModelClientFactory  # noqa: E402
from openclaw_runtime.vision_client import VisionClient, VisionError  # noqa: E402

# 24x24 PNG: red / green / blue horizontal bands.
_SAMPLE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAIAAABvFaqvAAAAJ0lEQVR42mO4o6FBFcQwatCI"
    "NkhjgQ1V0KhBI9yggBNUQaMGjWiDAMEolB8Q1DwvAAAAAElFTkSuQmCC"
)

# Words a model would only use if it actually saw the red/green/blue bands.
_SAMPLE_CUES = (
    "red", "green", "blue", "band", "bands", "stripe", "stripes", "horizontal",
    "紅", "綠", "藍", "橫", "條", "帶", "三色",
)


def _sample_image() -> Path:
    path = ROOT / ".cache" / "vision_smoke_sample.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(_SAMPLE_PNG_B64))
    return path


def sample_image_understood(description: str) -> bool:
    """True if the description mentions enough of the test image to prove the
    model read the pixels (not just answered from the prompt)."""
    lowered = (description or "").lower()
    return sum(cue in lowered for cue in _SAMPLE_CUES) >= 2


def main(argv: list[str]) -> int:
    settings = load_settings()
    registry = load_model_registry(settings)
    clients = ModelClientFactory(settings, registry)

    default_spec = registry.resolve("local_default")
    vision_spec = registry.resolve("vision")
    llm = clients.get_or_default("vision")
    shared = (
        vision_spec.base_url == default_spec.base_url and vision_spec.model == default_spec.model
    )

    print(f"vision model_id : {vision_spec.model_id}")
    print(f"vision base_url : {vision_spec.base_url}")
    print(f"vision model    : {vision_spec.model}")
    print(f"shares main LLM : {shared}")
    print(f"reachable       : {llm.is_reachable()}")

    if len(argv) > 1:
        image = Path(argv[1]).expanduser()
        if not image.is_file():
            print(f"ERROR: no such image: {image}", file=sys.stderr)
            return 2
        probe = False
    else:
        image = _sample_image()
        probe = True
    print(f"image           : {image}")

    started = time.monotonic()
    try:
        description = VisionClient(llm).describe_image(image)
    except VisionError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        if shared:
            print(
                "      'vision' shares the main model endpoint and it did not return an\n"
                "      image description -- the main model may be text-only. Configure a\n"
                "      dedicated VLM (docs/VISION_SETUP.md).",
                file=sys.stderr,
            )
        return 1
    elapsed = time.monotonic() - started

    print(f"latency         : {elapsed:.1f}s\n")
    print("description:")
    print(description)
    print()

    if not probe:
        print("verdict         : endpoint answered -- review the description above yourself.")
        return 0

    if sample_image_understood(description):
        if shared:
            print(
                "verdict         : vision OK. 'vision' shares the main model endpoint and it\n"
                "                  is multimodal, so a dedicated VLM is optional\n"
                "                  (docs/VISION_SETUP.md)."
            )
        else:
            print("verdict         : vision OK -- the dedicated VLM read the test image.")
        return 0

    print(
        "WARNING         : the model answered but did not describe the red/green/blue\n"
        "                  test image. " + (
            "The main model is likely text-only; configure a\n"
            "                  dedicated VLM (docs/VISION_SETUP.md)."
            if shared
            else "Check the vision model / request format."
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
