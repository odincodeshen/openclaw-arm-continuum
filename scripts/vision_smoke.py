#!/usr/bin/env python3
"""Live smoke test for the vision (VLM) endpoint.

Builds the VisionClient exactly as the Telegram gateway does, then runs one
image description against the resolved endpoint. Use it to confirm a dedicated
VLM is reachable and answering *before* enabling image analysis for Telegram
users.

Run it where the runtime environment is set (inside the container):

    docker compose exec openclaw-telegram python scripts/vision_smoke.py
    docker compose exec openclaw-telegram python scripts/vision_smoke.py /path/to/photo.jpg

With no argument it uses a tiny bundled 3-colour test image, which exercises the
multimodal request/response path but is not a real quality check -- pass your
own photo or screenshot for that.

Exit codes: 0 = endpoint returned a non-empty description; 1 = endpoint call
failed; 2 = bad usage / missing image.
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

# 24x24 PNG: red / green / blue horizontal bands. Small enough to inline, real
# enough that a working VLM will mention colours or bands.
_SAMPLE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAIAAABvFaqvAAAAJ0lEQVR42mO4o6FBFcQwatCI"
    "NkhjgQ1V0KhBI9yggBNUQaMGjWiDAMEolB8Q1DwvAAAAAElFTkSuQmCC"
)


def _sample_image() -> Path:
    path = ROOT / ".cache" / "vision_smoke_sample.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(_SAMPLE_PNG_B64))
    return path


def main(argv: list[str]) -> int:
    settings = load_settings()
    registry = load_model_registry(settings)
    clients = ModelClientFactory(settings, registry)

    default_spec = registry.resolve("local_default")
    vision_spec = registry.resolve("vision")
    llm = clients.get_or_default("vision")

    print(f"vision model_id : {vision_spec.model_id}")
    print(f"vision base_url : {vision_spec.base_url}")
    print(f"vision model    : {vision_spec.model}")
    print(f"reachable       : {llm.is_reachable()}")

    shares_text_endpoint = (
        vision_spec.base_url == default_spec.base_url and vision_spec.model == default_spec.model
    )
    if shares_text_endpoint:
        print(
            "\nWARNING: 'vision' resolves to the same endpoint + model as 'local_default'.\n"
            "         No dedicated VLM is configured, so image analysis quality will be\n"
            "         poor or unusable. See docs/VISION_SETUP.md.\n"
        )

    if len(argv) > 1:
        image = Path(argv[1]).expanduser()
        if not image.is_file():
            print(f"ERROR: no such image: {image}", file=sys.stderr)
            return 2
    else:
        image = _sample_image()
    print(f"image           : {image}")

    started = time.monotonic()
    try:
        description = VisionClient(llm).describe_image(image)
    except VisionError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        return 1
    elapsed = time.monotonic() - started

    print(f"latency         : {elapsed:.1f}s\n")
    print("description:")
    print(description)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
