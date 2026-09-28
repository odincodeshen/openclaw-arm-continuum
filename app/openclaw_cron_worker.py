#!/usr/bin/env python3
import json
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from openclaw_runtime.alerts import Alerter
from openclaw_runtime.config import Settings, load_settings
from openclaw_runtime.cron_jobs import is_due, load_jobs, mark_ran, validate_time
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
# Set in main(); None (tests, or before start) means alerts are skipped.
ALERTER: Alerter | None = None
_gateway_down_since: float | None = None


def _alert(key: str, summary: str, detail: str = "") -> None:
    if ALERTER is not None:
        ALERTER.alert(key, summary, detail)


def _resolve(key: str, summary: str) -> None:
    if ALERTER is not None:
        ALERTER.resolve(key, summary)


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


def in_prepare_window(now: datetime, window: str) -> bool:
    """window is "HH:MM-HH:MM" in the cron timezone; empty or malformed = never."""
    start, _, end = (window or "").partition("-")
    try:
        start, end = validate_time(start.strip()), validate_time(end.strip())
    except Exception:
        return False
    return start <= now.strftime("%H:%M") <= end


def preparable_jobs(jobs: list[dict], now: datetime, prompts: tuple[str, ...]) -> list[dict]:
    """Enabled daily jobs due later today whose prompt starts with one of
    the prefixes -- only content that won't be stale by delivery time."""
    now_hm = now.strftime("%H:%M")
    chosen = []
    for job in jobs:
        schedule = job.get("schedule") or {}
        prompt = str(job.get("prompt", "")).strip()
        if not job.get("enabled", True) or schedule.get("type") != "daily":
            continue
        if str(schedule.get("time", "")) <= now_hm:
            continue
        if any(prompt.startswith(prefix) for prefix in prompts):
            chosen.append(job)
    return chosen


def prepare_jobs(router: SkillRouter, jobs: list[dict], now: datetime, state: dict) -> int:
    """Generate each job's result now and keep it in state for delivery at
    the job's own time. A job that fails here is simply generated live."""
    prepared = state.setdefault("prepared_jobs", {})
    count = 0
    for job in jobs:
        try:
            result = router.route(str(job.get("prompt", "")).strip())
        except Exception as exc:
            log(f"[cron] prepare failed id={job.get('id')}, will run at its time: {exc}")
            _alert(
                f"cron-prepare:{job.get('id')}",
                f'Off-peak preparation of "{job.get("name", job.get("id"))}" failed; it will run live at its time.',
                str(exc),
            )
            continue
        _resolve(f"cron-prepare:{job.get('id')}", f'Off-peak preparation of "{job.get("name", job.get("id"))}" works again.')
        prepared[str(job["id"])] = {
            "date": now.strftime("%Y-%m-%d"),
            "answer": result.answer,
            "suppress": bool(getattr(result, "suppress_if_routine", False)),
        }
        count += 1
    return count


def take_prepared(job: dict, now: datetime, state: dict) -> dict | None:
    """Today's prepared result for this job (removed from state), if any."""
    prepared = state.get("prepared_jobs") or {}
    entry = prepared.pop(str(job.get("id")), None)
    if entry and entry.get("date") == now.strftime("%Y-%m-%d"):
        return entry
    return None


def run_dynamic_job(
    settings: Settings, router: SkillRouter, now: datetime, job: dict, prepared: dict | None = None
) -> dict:
    """Run one /cron job and push its result to Telegram -- or push the
    result prepared earlier in the off-peak window, when there is one. The
    result is not written anywhere else: saving it into the inbox would have
    the memory watcher index every run into tracker memory, filling /rag
    with copies of content that already lives there."""
    started = time.time()
    prompt = str(job.get("prompt", "")).strip()
    title = str(job.get("name", job.get("id", "OpenClaw cron job"))).strip()
    if not prompt:
        raise ValueError(f"cron job {job.get('id')} has empty prompt")

    status = "ok"
    error = None
    suppressed = False
    try:
        if prepared is not None:
            answer = prepared.get("answer") or "The OpenClaw runtime returned an empty reply."
            suppressed = bool(prepared.get("suppress"))
        else:
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
    if status == "error":
        _alert(f"cron-job:{job.get('id')}", f'Cron job "{title}" failed.', error or "")
    else:
        _resolve(f"cron-job:{job.get('id')}", f'Cron job "{title}" is working again.')
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


# The job list is polled every cron_poll_seconds; only a change is logged
# (job count, or the Gateway going away / coming back). Logging every poll
# wrote ~2,900 identical lines a day per bot and hid a week-long Gateway 404.
_last_job_poll_note: str | None = None


def log_job_poll(note: str) -> None:
    global _last_job_poll_note
    if note != _last_job_poll_note:
        log(note)
        _last_job_poll_note = note


def load_dynamic_jobs(settings: Settings) -> list[dict]:
    global _gateway_down_since
    default_chat_id = recipients(settings)[0] if recipients(settings) else None
    try:
        jobs = [
            runtime_job
            for job in list_gateway_jobs(settings, include_disabled=True)
            if (runtime_job := gateway_job_to_runtime(job, default_chat_id))
        ]
        log_job_poll(f"[cron] loaded {len(jobs)} Gateway dashboard job(s)")
        _gateway_down_since = None
        _resolve("gateway-rpc", "The Gateway is reachable again; dashboard cron jobs are loading.")
        return jobs
    except Exception as exc:
        log_job_poll(f"[cron] Gateway cron RPC unavailable; falling back to legacy JSON: {exc}")
        # A restart of the Gateway takes a few seconds; only a lasting outage alerts.
        now = time.time()
        if _gateway_down_since is None:
            _gateway_down_since = now
        elif now - _gateway_down_since >= settings.alert_gateway_outage_minutes * 60:
            _alert(
                "gateway-rpc",
                f"Can't reach this bot's Gateway for {settings.alert_gateway_outage_minutes}+ minutes: "
                "cron jobs set on the dashboard aren't loading.",
                str(exc),
            )
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

    global ALERTER
    ALERTER = Alerter(settings, log=log)
    tz = ZoneInfo(settings.cron_timezone)
    state = load_json(settings.cron_state_path, {})
    llm = LlmClient(settings)
    router = SkillRouter(settings, llm)
    log(f"[cron] started timezone={settings.cron_timezone} recipients={recipients(settings)}")

    while RUNNING:
        try:
            now = datetime.now(tz)
            jobs = load_dynamic_jobs(settings)
            today = now.strftime("%Y-%m-%d")
            if in_prepare_window(now, settings.cron_prepare_window) and state.get("last_prepare_date") != today:
                # Off-peak generation, once a day (marked first, so a failure
                # isn't retried -- those jobs just run live at their time).
                state["last_prepare_date"] = today
                write_json(settings.cron_state_path, state)
                chosen = preparable_jobs(jobs, now, settings.cron_prepare_prompts)
                if chosen:
                    count = prepare_jobs(router, chosen, now, state)
                    write_json(settings.cron_state_path, state)
                    log(f"[cron] prepared {count} of {len(chosen)} job(s) for later today")
            for job in jobs:
                if is_due(job, now, state, window_minutes=settings.cron_due_window_minutes):
                    prepared = take_prepared(job, now, state)
                    result = run_dynamic_job(settings, router, now, job, prepared)
                    mark_ran(job, now, state)
                    write_gateway_runback(settings, job, now, result)
                    write_json(settings.cron_state_path, state)
                    source = "prepared" if prepared is not None else "live"
                    log(f"[cron] dynamic job id={job.get('id')} status={result['status']} ({source})")
        except Exception:
            log(traceback.format_exc())
            _alert("cron-loop", "The cron worker hit an error; scheduled jobs may not have run.", traceback.format_exc())
        time.sleep(settings.cron_poll_seconds)

    log("[cron] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
