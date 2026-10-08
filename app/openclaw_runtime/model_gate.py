"""Wait for a quiet model before off-peak preparation.

Several bots share one model, and their off-peak jobs used to start at the
same minute: the cron worker's prepared reports, the English bot's daily
task, the check-in reports. Together they slowed every request until one
timed out. That happened to the English bot on 2026-10-05, whose task was
then generated late, at push time.

Containers share no files, but they share the model server, and it can say
how busy it is:

- vLLM: ``/metrics``, requests running plus waiting;
- llama.cpp: ``/slots``, slots processing.

So before a preparation job, wait_for_quiet_model():

1. waits a short, stable per-bot offset, so bots don't all ask at the same
   second;
2. then waits until the server reports nothing running twice in a row;
3. gives up after ``max_wait`` seconds and lets the job run anyway.

It is not a lock: two jobs that see the idle model at the same moment both
start. In practice the offsets and the double check spread them out. A
server that can't say (an older or remote one) counts as quiet.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
import zlib

_RUNNING = re.compile(r"^vllm:num_requests_(running|waiting)\{[^}]*\}\s+([0-9.]+)", re.M)


def _get(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def requests_in_flight(base_url: str, timeout: float = 5) -> int | None:
    """Requests the model server is working on (running or queued), or None
    when it doesn't say. base_url is the OpenAI-style one, ending in /v1."""
    root = base_url.rstrip("/").removesuffix("/v1")
    try:
        found = _RUNNING.findall(_get(f"{root}/metrics", timeout))
        if found:
            return int(sum(float(value) for _, value in found))
    except Exception:  # noqa: BLE001 - not vLLM, or metrics off
        pass
    try:
        slots = json.loads(_get(f"{root}/slots", timeout))
        if isinstance(slots, list):
            return sum(1 for slot in slots if slot.get("is_processing"))
    except Exception:  # noqa: BLE001 - not llama.cpp, or slots off
        pass
    return None


def offset_seconds(label: str, spread: int = 120) -> int:
    """A stable 0..spread-second offset for this bot, so bots ask in turn."""
    return zlib.crc32(label.encode("utf-8")) % (spread + 1)


def wait_for_quiet_model(base_url: str, label: str, *, max_wait: int = 1800, poll: int = 15,
                         spread: int = 120, sleep=time.sleep, clock=time.monotonic, log=print) -> float:
    """Block until the model looks idle (or max_wait passes). Returns the
    seconds waited."""
    started = clock()
    sleep(offset_seconds(label, spread))
    quiet_checks = 0
    while clock() - started < max_wait:
        busy = requests_in_flight(base_url)
        if busy is None:
            break  # the server can't say; don't hold the job up
        quiet_checks = quiet_checks + 1 if busy == 0 else 0
        if quiet_checks >= 2:
            break
        sleep(poll)
    else:
        log(f"[prepare] {label}: the model was still busy after {max_wait // 60} min; going ahead")
    waited = clock() - started
    if waited >= poll:
        log(f"[prepare] {label}: waited {waited:.0f}s for a quiet model")
    return waited


def before_preparing(settings, what: str, log=print) -> None:
    """The gate for one of this bot's off-peak jobs, per its settings
    (OPENCLAW_PREPARE_WAIT_MINUTES; 0 = don't wait). The offset is keyed on
    the bot's tracker collection, which differs between bots."""
    minutes = getattr(settings, "prepare_wait_minutes", 0)
    if minutes <= 0:
        return
    wait_for_quiet_model(settings.vllm_base_url, f"{what} ({settings.tracker_collection})",
                         max_wait=minutes * 60, log=log)
