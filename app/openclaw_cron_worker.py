#!/usr/bin/env python3
import json
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from openclaw_runtime.config import Settings, load_settings
from openclaw_runtime.cron_jobs import is_due, load_jobs, mark_ran
from openclaw_runtime.gateway_cron import (
    append_gateway_run_log,
    gateway_job_to_runtime,
    list_gateway_jobs,
    update_gateway_job_state,
    update_gateway_job_state_sqlite,
)
from openclaw_runtime.http_client import request_json
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.skill_router import SkillRouter


RUNNING = True


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    print(f"{stamp} {message}", flush=True)


def stop(_signum: int, _frame: object) -> None:
    global RUNNING
    RUNNING = False


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def telegram(settings: Settings, method: str, payload: dict | None = None, timeout: int = 60) -> dict:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    return request_json("POST", url, payload or {}, timeout=timeout)


def send_message(settings: Settings, chat_id: int, text: str) -> None:
    if len(text) <= settings.max_reply_chars:
        telegram(settings, "sendMessage", {"chat_id": chat_id, "text": text})
        return
    for start in range(0, len(text), settings.max_reply_chars):
        telegram(
            settings,
            "sendMessage",
            {"chat_id": chat_id, "text": text[start : start + settings.max_reply_chars]},
        )


def recipients(settings: Settings) -> list[int]:
    chat_ids = settings.cron_chat_ids or settings.telegram_allowed_chat_ids
    return sorted(chat_ids)


def format_job_message(title: str, answer: str) -> str:
    """Just the result, headed by the job name -- unless the result already
    opens with it (the daily reports carry their own title line)."""
    body = answer.strip()
    if body.startswith(title):
        return body
    return f"{title}\n\n{body}"


def run_dynamic_job(settings: Settings, router: SkillRouter, now: datetime, job: dict) -> dict:
    """Run one /cron job and push its result to Telegram. The result is not
    written anywhere else: saving it into the inbox would have the memory
    watcher index every run into tracker memory, filling /rag with copies of
    content that already lives there."""
    started = time.time()
    prompt = str(job.get("prompt", "")).strip()
    title = str(job.get("name", job.get("id", "OpenClaw cron job"))).strip()
    if not prompt:
        raise ValueError(f"cron job {job.get('id')} has empty prompt")

    status = "ok"
    error = None
    suppressed = False
    try:
        result = router.route(prompt)
        answer = result.answer or "The OpenClaw runtime returned an empty reply."
        suppressed = bool(getattr(result, "suppress_if_routine", False))
    except Exception as exc:
        status = "error"
        error = str(exc)
        answer = f"Task failed: {exc}"

    report = format_job_message(title, answer)
    delivered = False
    if suppressed:
        # A routine "nothing new" result (e.g. a reminder digest with
        # nothing due) -- record the run, but don't push a notification
        # nobody needs to see.
        status = "skipped"
    else:
        try:
            send_message(settings, int(job.get("chat_id") or recipients(settings)[0]), report)
            delivered = True
        except Exception as exc:
            status = "error"
            error = f"{error}; Telegram delivery failed: {exc}" if error else f"Telegram delivery failed: {exc}"
    return {
        "status": status,
        "error": error,
        "summary": answer.strip()[:1200],
        "duration_ms": int((time.time() - started) * 1000),
        "delivered": delivered,
    }


def write_gateway_runback(settings: Settings, job: dict, now: datetime, result: dict) -> None:
    job_id = str(job.get("id") or "")
    if not job_id:
        return
    current_state = dict(job.get("gateway_state") or {})
    previous_errors = int(current_state.get("consecutiveErrors") or 0)
    previous_skipped = int(current_state.get("consecutiveSkipped") or 0)
    status = result.get("status") or "ok"
    run_at_ms = int(now.timestamp() * 1000)
    if status == "skipped":
        delivery_status = "skipped"
    elif result.get("delivered"):
        delivery_status = "delivered"
    else:
        delivery_status = "not-delivered"
    state = {
        **current_state,
        "lastRunAtMs": run_at_ms,
        "lastRunStatus": status,
        "lastStatus": status,
        "lastDurationMs": int(result.get("duration_ms") or 0),
        "lastDeliveryStatus": delivery_status,
        "lastDelivered": bool(result.get("delivered")),
        # Three-way status: "skipped" (a routine no-op, e.g. an empty
        # reminder digest) is not an error and must not count toward
        # consecutiveErrors; only "error" does. Each counter resets when
        # its status doesn't apply, so a run alternating error/skipped/ok
        # never double-counts.
        "consecutiveErrors": previous_errors + 1 if status == "error" else 0,
        "consecutiveSkipped": previous_skipped + 1 if status == "skipped" else 0,
    }
    if result.get("error"):
        state["lastError"] = str(result["error"])
        state["lastDeliveryError"] = str(result["error"]) if not result.get("delivered") else None
    else:
        state["lastError"] = None
        state["lastDeliveryError"] = None
    try:
        update_gateway_job_state(settings, job_id, state)
    except Exception as exc:
        log(f"[cron] Gateway cron.update state failed id={job_id}: {exc}")
    try:
        update_gateway_job_state_sqlite(settings, job_id, state)
        append_gateway_run_log(
            settings,
            job_id,
            status=status,
            summary=str(result.get("summary") or ""),
            error=result.get("error"),
            run_at_ms=run_at_ms,
            duration_ms=int(result.get("duration_ms") or 0),
            next_run_at_ms=state.get("nextRunAtMs"),
            delivered=bool(result.get("delivered")),
            model=settings.vllm_model,
            provider="local-llm",
        )
    except Exception as exc:
        log(f"[cron] Gateway run history writeback failed id={job_id}: {exc}")


def load_dynamic_jobs(settings: Settings) -> list[dict]:
    default_chat_id = recipients(settings)[0] if recipients(settings) else None
    try:
        jobs = [
            runtime_job
            for job in list_gateway_jobs(settings, include_disabled=True)
            if (runtime_job := gateway_job_to_runtime(job, default_chat_id))
        ]
        log(f"[cron] loaded {len(jobs)} Gateway dashboard job(s)")
        return jobs
    except Exception as exc:
        log(f"[cron] Gateway cron RPC unavailable; falling back to legacy JSON: {exc}")
        return list(load_jobs(settings.cron_jobs_path).get("jobs", []))


def main() -> int:
    settings = load_settings()
    if not settings.cron_enabled:
        log("[cron] disabled")
        return 0
    if not settings.telegram_bot_token:
        log("[cron] OPENCLAW_TELEGRAM_BOT_TOKEN is required")
        return 2
    if not recipients(settings):
        log("[cron] no recipients; set OPENCLAW_CRON_CHAT_IDS or OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS")
        return 2

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    tz = ZoneInfo(settings.cron_timezone)
    state = load_json(settings.cron_state_path, {})
    llm = LlmClient(settings)
    router = SkillRouter(settings, llm)
    log(f"[cron] started timezone={settings.cron_timezone} recipients={recipients(settings)}")

    while RUNNING:
        try:
            now = datetime.now(tz)
            for job in load_dynamic_jobs(settings):
                if is_due(job, now, state, window_minutes=settings.cron_due_window_minutes):
                    result = run_dynamic_job(settings, router, now, job)
                    mark_ran(job, now, state)
                    write_gateway_runback(settings, job, now, result)
                    write_json(settings.cron_state_path, state)
                    log(f"[cron] dynamic job id={job.get('id')} status={result['status']}")
        except Exception:
            log(traceback.format_exc())
        time.sleep(settings.cron_poll_seconds)

    log("[cron] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
