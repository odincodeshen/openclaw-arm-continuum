#!/usr/bin/env python3
"""Static checks run in CI.

- import-smoke the dependency-free entrypoints and every openclaw_runtime module
- parse app/skills.json
- validate every model catalog (app/models*.json) against ModelSpec/ModelRegistry

The Whisper and browser-scraper services need heavy optional deps
(faster-whisper, playwright) and are covered by their own image builds/tests, so
they are skipped here.
"""

from __future__ import annotations

import glob
import importlib
import json
import os
import pkgutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("OPENCLAW_GATEWAY_TOKEN", "ci")

failures: list[str] = []


def check(label: str, fn) -> None:
    try:
        fn()
        print(f"ok   {label}")
    except Exception as exc:  # noqa: BLE001 - collect and report all
        failures.append(f"{label}: {type(exc).__name__}: {exc}")
        print(f"FAIL {label}: {type(exc).__name__}: {exc}")


# 1. import smoke -------------------------------------------------------------
for mod in ("openclaw_telegram_gateway", "openclaw_memory_watcher", "openclaw_cron_worker"):
    check(f"import {mod}", lambda m=mod: importlib.import_module(m))

import openclaw_runtime  # noqa: E402

for info in pkgutil.walk_packages(openclaw_runtime.__path__, "openclaw_runtime."):
    check(f"import {info.name}", lambda n=info.name: importlib.import_module(n))

# 2. skills.json ------------------------------------------------------------
check("app/skills.json parses", lambda: json.loads((ROOT / "app" / "skills.json").read_text("utf-8")))

# 3. model catalogs ------------------------------------------------------------
from openclaw_runtime.model_catalog import ModelRegistry, ModelSpec  # noqa: E402


def _validate_catalog(path: Path) -> None:
    doc = json.loads(path.read_text("utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), dict):
        raise ValueError("missing top-level 'models' object")
    specs = [ModelSpec.from_dict(mid, val, default_timeout=60) for mid, val in doc["models"].items()]
    ModelRegistry(specs)  # validates duplicates + fallback references


for f in sorted(glob.glob(str(ROOT / "app" / "models*.json"))):
    check(f"catalog {Path(f).name}", lambda p=Path(f): _validate_catalog(p))

# ---------------------------------------------------------------------------
if failures:
    print(f"\n{len(failures)} check(s) failed", file=sys.stderr)
    sys.exit(1)
print("\nall static checks passed")
