import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from openclaw_runtime.config import Settings
from openclaw_runtime.cron_jobs import (
    parse_interval_seconds,
    split_name_prompt,
    validate_month_day,
    validate_time,
    validate_weekday,
    weekday_name,
)


METADATA_MARKER = "OpenClaw Telegram cron"


class GatewayCronError(RuntimeError):
    pass


def rpc(settings: Settings, method: str, params: dict | None = None, timeout: int = 20) -> dict:
    if not settings.gateway_token:
        raise GatewayCronError("OPENCLAW_GATEWAY_TOKEN is required for Gateway cron RPC")
    body = json.dumps({"method": method, "params": params or {}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        settings.gateway_rpc_url,
        data=body,
        headers={
            "Authorization": f"Bearer {settings.gateway_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except Exception as parse_exc:
            raise GatewayCronError(f"{method} failed: HTTP {exc.code}") from parse_exc
    except Exception as exc:
        raise GatewayCronError(f"{method} failed: {exc}") from exc
    if not data.get("ok"):
        error = data.get("error") or {}
        raise GatewayCronError(f"{method} failed: {error.get('message', data)}")
    return data.get("payload") or {}


def list_gateway_jobs(settings: Settings, include_disabled: bool = True) -> list[dict]:
    payload = rpc(settings, "cron.list", {"includeDisabled": include_disabled, "limit": 200})
    return list(payload.get("jobs") or [])


def remove_gateway_job(settings: Settings, job_id: str) -> bool:
    payload = rpc(settings, "cron.remove", {"jobId": job_id})
    return bool(payload.get("removed"))


def update_gateway_job_state(settings: Settings, job_id: str, state: dict) -> None:
    rpc(settings, "cron.update", {"jobId": job_id, "patch": {"state": state}})


def update_gateway_job_state_sqlite(settings: Settings, job_id: str, state: dict) -> bool:
    db_path = settings.gateway_state_db_path
    if not db_path.exists():
        return False
    store_key = _cron_store_key(db_path, job_id)
    if not store_key:
        return False
    now_ms = int(time.time() * 1000)
    with sqlite3.connect(str(db_path), timeout=10) as con:
        con.execute(
            """
            update cron_jobs set
                next_run_at_ms=?,
                running_at_ms=?,
                last_run_at_ms=?,
                last_run_status=?,
                last_error=?,
                last_duration_ms=?,
                consecutive_errors=?,
                consecutive_skipped=?,
                last_delivery_status=?,
                last_delivery_error=?,
                last_delivered=?,
                state_json=?,
                runtime_updated_at_ms=?,
                updated_at=?
            where store_key=? and job_id=?
            """,
            (
                state.get("nextRunAtMs"),
                state.get("runningAtMs"),
                state.get("lastRunAtMs"),
                state.get("lastRunStatus"),
                state.get("lastError"),
                state.get("lastDurationMs"),
                state.get("consecutiveErrors", 0),
                state.get("consecutiveSkipped", 0),
                state.get("lastDeliveryStatus"),
                state.get("lastDeliveryError"),
                1 if state.get("lastDelivered") else 0 if "lastDelivered" in state else None,
                json.dumps(state, ensure_ascii=False),
                now_ms,
                now_ms,
                store_key,
                job_id,
            ),
        )
        con.commit()
    return True


def get_gateway_job(settings: Settings, job_id: str) -> dict | None:
    try:
        return rpc(settings, "cron.get", {"jobId": job_id})
    except GatewayCronError:
        jobs = list_gateway_jobs(settings)
        return next((job for job in jobs if job.get("id") == job_id or job.get("name") == job_id), None)


def append_gateway_run_log(
    settings: Settings,
    job_id: str,
    *,
    status: str,
    summary: str,
    error: str | None,
    run_at_ms: int,
    duration_ms: int,
    next_run_at_ms: int | None,
    delivered: bool,
    model: str | None = None,
    provider: str | None = None,
) -> bool:
    db_path = settings.gateway_state_db_path
    if not db_path.exists():
        return False
    store_key = _cron_store_key(db_path, job_id)
    if not store_key:
        return False
    now_ms = int(time.time() * 1000)
    entry = {
        "ts": now_ms,
        "jobId": job_id,
        "action": "finished",
        "status": status,
        "summary": summary[:1200],
        "deliveryStatus": "delivered" if delivered else "not-delivered",
        "delivery": {
            "intended": {"channel": "telegram", "to": None, "source": "openclaw-cron-worker"},
            "fallbackUsed": False,
            "delivered": delivered,
        },
        "runAtMs": run_at_ms,
        "durationMs": duration_ms,
        **({"nextRunAtMs": next_run_at_ms} if next_run_at_ms is not None else {}),
        **({"error": error} if error else {}),
        **({"model": model} if model else {}),
        **({"provider": provider} if provider else {}),
    }
    with sqlite3.connect(str(db_path), timeout=10) as con:
        cur = con.cursor()
        seq = (
            cur.execute(
                "select coalesce(max(seq), 0) + 1 from cron_run_logs where store_key=? and job_id=?",
                (store_key, job_id),
            ).fetchone()[0]
            or 1
        )
        cur.execute(
            """
            insert into cron_run_logs (
                store_key, job_id, seq, ts, status, error, summary, diagnostics_summary,
                delivery_status, delivery_error, delivered, session_id, session_key, run_id,
                run_at_ms, duration_ms, next_run_at_ms, model, provider, total_tokens,
                entry_json, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                store_key,
                job_id,
                seq,
                now_ms,
                status,
                error,
                summary[:1200],
                None,
                "delivered" if delivered else "not-delivered",
                None if delivered else "Telegram delivery was not confirmed",
                1 if delivered else 0,
                None,
                f"agent:main:cron:{job_id}:run:openclaw-cron-worker-{now_ms}",
                f"openclaw-cron-worker-{now_ms}",
                run_at_ms,
                duration_ms,
                next_run_at_ms,
                model,
                provider,
                None,
                json.dumps(entry, ensure_ascii=False),
                now_ms,
            ),
        )
        con.commit()
    return True


def _cron_store_key(db_path: Path, job_id: str) -> str | None:
    with sqlite3.connect(str(db_path), timeout=10) as con:
        row = con.execute("select store_key from cron_jobs where job_id=? limit 1", (job_id,)).fetchone()
    return str(row[0]) if row else None


def metadata(chat_id: int, schedule: dict) -> str:
    return METADATA_MARKER + "\n" + json.dumps(
        {"openclawTelegramCron": {"chat_id": chat_id, "schedule": schedule}},
        ensure_ascii=False,
        sort_keys=True,
    )


def parse_metadata(job: dict) -> dict:
    description = str(job.get("description") or "")
    match = re.search(r"\{.*\"openclawTelegramCron\".*\}", description, flags=re.S)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    value = parsed.get("openclawTelegramCron")
    return value if isinstance(value, dict) else {}


def schedule_to_gateway(schedule: dict) -> dict:
    schedule_type = schedule.get("type")
    if schedule_type == "daily":
        hour, minute = validate_time(str(schedule.get("time"))).split(":")
        return {"kind": "cron", "expr": f"{int(minute)} {int(hour)} * * *", "tz": "Europe/London", "staggerMs": 0}
    if schedule_type == "weekly":
        hour, minute = validate_time(str(schedule.get("time"))).split(":")
        weekday = int(schedule.get("weekday", validate_weekday(str(schedule.get("day", "mon")))))
        cron_weekday = 0 if weekday == 6 else weekday + 1
        return {"kind": "cron", "expr": f"{int(minute)} {int(hour)} * * {cron_weekday}", "tz": "Europe/London", "staggerMs": 0}
    if schedule_type == "monthly":
        hour, minute = validate_time(str(schedule.get("time"))).split(":")
        day = validate_month_day(str(schedule.get("day", "1")))
        cron_day = "28-31" if day == "last" else str(day)
        return {"kind": "cron", "expr": f"{int(minute)} {int(hour)} {cron_day} * *", "tz": "Europe/London", "staggerMs": 0}
    if schedule_type == "interval":
        seconds = int(schedule.get("seconds") or parse_interval_seconds(str(schedule.get("every", "1h"))))
        return {"kind": "every", "everyMs": seconds * 1000, "anchorMs": int(datetime.now().timestamp() * 1000)}
    raise ValueError("unsupported schedule type")


def add_gateway_job(settings: Settings, chat_id: int, name: str, prompt: str, schedule: dict) -> dict:
    params = {
        "name": name,
        "description": metadata(chat_id, schedule),
        "enabled": True,
        "schedule": schedule_to_gateway(schedule),
        "payload": {"kind": "agentTurn", "message": prompt},
        "sessionTarget": "isolated",
        "wakeMode": "now",
        "delivery": {"mode": "none"},
    }
    return rpc(settings, "cron.add", params)


def build_daily(settings: Settings, chat_id: int, time_text: str, raw: str) -> dict:
    name, prompt = split_name_prompt(raw)
    return add_gateway_job(settings, chat_id, name, prompt, {"type": "daily", "time": validate_time(time_text)})


def build_weekly(settings: Settings, chat_id: int, weekday_text: str, time_text: str, raw: str) -> dict:
    name, prompt = split_name_prompt(raw)
    weekday = validate_weekday(weekday_text)
    schedule = {"type": "weekly", "weekday": weekday, "day": weekday_name(weekday), "time": validate_time(time_text)}
    return add_gateway_job(settings, chat_id, name, prompt, schedule)


def build_monthly(settings: Settings, chat_id: int, day_text: str, time_text: str, raw: str) -> dict:
    name, prompt = split_name_prompt(raw)
    return add_gateway_job(settings, chat_id, name, prompt, {"type": "monthly", "day": validate_month_day(day_text), "time": validate_time(time_text)})


def build_interval(settings: Settings, chat_id: int, interval_text: str, raw: str) -> dict:
    name, prompt = split_name_prompt(raw)
    interval = interval_text.strip().lower()
    return add_gateway_job(
        settings,
        chat_id,
        name,
        prompt,
        {"type": "interval", "every": interval, "seconds": parse_interval_seconds(interval)},
    )


def gateway_job_to_runtime(job: dict, default_chat_id: int | None = None) -> dict | None:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    prompt = str(payload.get("message") or "").strip()
    if not prompt:
        return None
    meta = parse_metadata(job)
    schedule = meta.get("schedule") if isinstance(meta.get("schedule"), dict) else gateway_schedule_to_runtime(job.get("schedule"))
    if not schedule:
        return None
    chat_id = meta.get("chat_id") or default_chat_id
    return {
        "id": job.get("id"),
        "enabled": bool(job.get("enabled", True)),
        "chat_id": chat_id,
        "name": job.get("name") or job.get("id") or "OpenClaw cron job",
        "prompt": prompt,
        "schedule": schedule,
        "gateway_state": job.get("state") if isinstance(job.get("state"), dict) else {},
        "created_at": int((job.get("createdAtMs") or 0) / 1000),
    }


def gateway_schedule_to_runtime(schedule: Any) -> dict | None:
    if not isinstance(schedule, dict):
        return None
    if schedule.get("kind") == "every":
        seconds = int(schedule.get("everyMs") or 0) // 1000
        if seconds <= 0:
            return None
        if seconds % 86400 == 0:
            every = f"{seconds // 86400}d"
        elif seconds % 3600 == 0:
            every = f"{seconds // 3600}h"
        else:
            every = f"{max(1, seconds // 60)}m"
        return {"type": "interval", "every": every, "seconds": seconds}
    if schedule.get("kind") != "cron":
        return None
    expr = str(schedule.get("expr") or "").strip()
    parts = expr.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if month != "*":
        return None
    try:
        time_text = validate_time(f"{int(hour):02d}:{int(minute):02d}")
    except Exception:
        return None
    if dom == "*" and dow == "*":
        return {"type": "daily", "time": time_text}
    if dom == "*" and re.fullmatch(r"\d", dow):
        cron_dow = int(dow)
        weekday = 6 if cron_dow == 0 else cron_dow - 1
        return {"type": "weekly", "weekday": weekday, "day": weekday_name(weekday), "time": time_text}
    if dow == "*":
        if dom == "28-31":
            day: int | str = "last"
        elif re.fullmatch(r"\d{1,2}", dom):
            day = int(dom)
        else:
            return None
        return {"type": "monthly", "day": day, "time": time_text}
    return None
