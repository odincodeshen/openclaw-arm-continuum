import json
import re
import time
import uuid
import calendar
from datetime import datetime
from pathlib import Path


JOB_ID_PATTERN = re.compile(r"[^a-z0-9_-]+")


def load_jobs(path: Path) -> dict:
    if not path.exists():
        return {"jobs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"jobs": []}
    if not isinstance(data, dict):
        return {"jobs": []}
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        data["jobs"] = []
    return data


def save_jobs(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def make_job_id(name: str) -> str:
    base = JOB_ID_PATTERN.sub("-", name.strip().lower()).strip("-_")
    base = base[:32] or "job"
    return f"{base}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def parse_interval_seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)([mhd])", value.strip().lower())
    if not match:
        raise ValueError("Interval format must be like 30m, 2h, or 1d.")
    amount = int(match.group(1))
    unit = match.group(2)
    multiplier = {"m": 60, "h": 3600, "d": 86400}[unit]
    seconds = amount * multiplier
    if seconds < 60:
        raise ValueError("The minimum interval is 1m.")
    return seconds


def validate_time(value: str) -> str:
    if not re.fullmatch(r"\d{2}:\d{2}", value.strip()):
        raise ValueError("Time format must be HH:MM, for example 07:30.")
    hour_text, minute_text = value.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if hour > 23 or minute > 59:
        raise ValueError("Time out of range; use 00:00 through 23:59.")
    return f"{hour:02d}:{minute:02d}"


def validate_weekday(value: str) -> int:
    aliases = {
        "mon": 0,
        "monday": 0,
        "一": 0,
        "週一": 0,
        "星期一": 0,
        "tue": 1,
        "tuesday": 1,
        "二": 1,
        "週二": 1,
        "星期二": 1,
        "wed": 2,
        "wednesday": 2,
        "三": 2,
        "週三": 2,
        "星期三": 2,
        "thu": 3,
        "thursday": 3,
        "四": 3,
        "週四": 3,
        "星期四": 3,
        "fri": 4,
        "friday": 4,
        "五": 4,
        "週五": 4,
        "星期五": 4,
        "sat": 5,
        "saturday": 5,
        "六": 5,
        "週六": 5,
        "星期六": 5,
        "sun": 6,
        "sunday": 6,
        "日": 6,
        "天": 6,
        "週日": 6,
        "週天": 6,
        "星期日": 6,
        "星期天": 6,
    }
    normalized = value.strip().lower()
    if normalized in aliases:
        return aliases[normalized]
    if re.fullmatch(r"[1-7]", normalized):
        return int(normalized) - 1
    raise ValueError("Weekly schedule day must be mon/tue/.../sun, or 1-7.")


def weekday_name(index: int) -> str:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][index]


def validate_month_day(value: str) -> int | str:
    normalized = value.strip().lower()
    if normalized in {"last", "月底", "最後一天"}:
        return "last"
    if not re.fullmatch(r"\d{1,2}", normalized):
        raise ValueError("Monthly schedule day must be 1-31 or last.")
    day = int(normalized)
    if day < 1 or day > 31:
        raise ValueError("Monthly schedule day must be 1-31 or last.")
    return day


def month_has_day(now: datetime, day: int | str) -> bool:
    if day == "last":
        return True
    _, days_in_month = calendar.monthrange(now.year, now.month)
    return int(day) <= days_in_month


def split_name_prompt(raw: str) -> tuple[str, str]:
    normalized = raw.replace("：", ":")
    if "::" not in normalized:
        raise ValueError("Use `name :: task content` (or the full-width `：：`) to separate the name and task.")
    name, prompt = normalized.split("::", 1)
    name = name.strip()
    prompt = prompt.strip()
    if not name or not prompt:
        raise ValueError("Neither the name nor the task content can be blank.")
    return name, prompt


def add_daily_job(path: Path, chat_id: int, time_text: str, raw: str) -> dict:
    name, prompt = split_name_prompt(raw)
    data = load_jobs(path)
    job = {
        "id": make_job_id(name),
        "enabled": True,
        "chat_id": chat_id,
        "name": name,
        "prompt": prompt,
        "schedule": {"type": "daily", "time": validate_time(time_text)},
        "created_at": int(time.time()),
    }
    data["jobs"].append(job)
    save_jobs(path, data)
    return job


def add_weekly_job(path: Path, chat_id: int, weekday_text: str, time_text: str, raw: str) -> dict:
    name, prompt = split_name_prompt(raw)
    weekday = validate_weekday(weekday_text)
    data = load_jobs(path)
    job = {
        "id": make_job_id(name),
        "enabled": True,
        "chat_id": chat_id,
        "name": name,
        "prompt": prompt,
        "schedule": {"type": "weekly", "weekday": weekday, "day": weekday_name(weekday), "time": validate_time(time_text)},
        "created_at": int(time.time()),
    }
    data["jobs"].append(job)
    save_jobs(path, data)
    return job


def add_monthly_job(path: Path, chat_id: int, day_text: str, time_text: str, raw: str) -> dict:
    name, prompt = split_name_prompt(raw)
    day = validate_month_day(day_text)
    data = load_jobs(path)
    job = {
        "id": make_job_id(name),
        "enabled": True,
        "chat_id": chat_id,
        "name": name,
        "prompt": prompt,
        "schedule": {"type": "monthly", "day": day, "time": validate_time(time_text)},
        "created_at": int(time.time()),
    }
    data["jobs"].append(job)
    save_jobs(path, data)
    return job


def add_interval_job(path: Path, chat_id: int, interval_text: str, raw: str) -> dict:
    name, prompt = split_name_prompt(raw)
    data = load_jobs(path)
    job = {
        "id": make_job_id(name),
        "enabled": True,
        "chat_id": chat_id,
        "name": name,
        "prompt": prompt,
        "schedule": {
            "type": "interval",
            "every": interval_text.strip().lower(),
            "seconds": parse_interval_seconds(interval_text),
        },
        "created_at": int(time.time()),
    }
    data["jobs"].append(job)
    save_jobs(path, data)
    return job


def delete_job(path: Path, job_id: str) -> bool:
    data = load_jobs(path)
    before = len(data["jobs"])
    data["jobs"] = [job for job in data["jobs"] if job.get("id") != job_id]
    save_jobs(path, data)
    return len(data["jobs"]) != before


def get_job(path: Path, job_id: str) -> dict | None:
    for job in load_jobs(path).get("jobs", []):
        if job.get("id") == job_id:
            return job
    return None


def describe_job(job: dict) -> str:
    schedule = job.get("schedule", {})
    if schedule.get("type") == "daily":
        schedule_text = f"daily {schedule.get('time', '?')}"
    elif schedule.get("type") == "weekly":
        schedule_text = f"weekly {schedule.get('day', weekday_name(int(schedule.get('weekday', 0))))} {schedule.get('time', '?')}"
    elif schedule.get("type") == "monthly":
        schedule_text = f"monthly {schedule.get('day', '?')} {schedule.get('time', '?')}"
    elif schedule.get("type") == "interval":
        schedule_text = f"every {schedule.get('every', '?')}"
    else:
        schedule_text = "unknown"
    status = "on" if job.get("enabled", True) else "off"
    return f"{job.get('id')} [{status}] {schedule_text} - {job.get('name', 'unnamed')}"


DEFAULT_DUE_WINDOW_MINUTES = 15


def _minutes_of_day(value: str) -> int:
    hour_text, minute_text = value.split(":", 1)
    return int(hour_text) * 60 + int(minute_text)


def _within_due_window(now: datetime, due_time: str, window_minutes: int) -> bool:
    now_minutes = now.hour * 60 + now.minute
    return now_minutes - _minutes_of_day(due_time) <= window_minutes


def is_due(job: dict, now: datetime, state: dict, window_minutes: int = DEFAULT_DUE_WINDOW_MINUTES) -> bool:
    if not job.get("enabled", True):
        return False

    job_id = job.get("id")
    if not job_id:
        return False

    schedule = job.get("schedule", {})
    last_runs = state.setdefault("job_last_runs", {})
    last_run = last_runs.get(job_id, {})

    if schedule.get("type") == "daily":
        due_time = validate_time(str(schedule.get("time", "00:00")))
        if now.strftime("%H:%M") < due_time:
            return False
        if not _within_due_window(now, due_time, window_minutes):
            return False
        created_at = int(job.get("created_at", 0))
        if created_at and not last_run:
            created = datetime.fromtimestamp(created_at, tz=now.tzinfo)
            if created.strftime("%Y-%m-%d") == now.strftime("%Y-%m-%d") and created.strftime("%H:%M") > due_time:
                return False
        return last_run.get("date") != now.strftime("%Y-%m-%d")

    if schedule.get("type") == "weekly":
        weekday = int(schedule.get("weekday", validate_weekday(str(schedule.get("day", "mon")))))
        if now.weekday() != weekday:
            return False
        due_time = validate_time(str(schedule.get("time", "00:00")))
        if now.strftime("%H:%M") < due_time:
            return False
        if not _within_due_window(now, due_time, window_minutes):
            return False
        created_at = int(job.get("created_at", 0))
        if created_at and not last_run:
            created = datetime.fromtimestamp(created_at, tz=now.tzinfo)
            if created.strftime("%Y-%m-%d") == now.strftime("%Y-%m-%d") and created.strftime("%H:%M") > due_time:
                return False
        return last_run.get("date") != now.strftime("%Y-%m-%d")

    if schedule.get("type") == "monthly":
        day = validate_month_day(str(schedule.get("day", "1")))
        if not month_has_day(now, day):
            return False
        target_day = calendar.monthrange(now.year, now.month)[1] if day == "last" else int(day)
        if now.day != target_day:
            return False
        due_time = validate_time(str(schedule.get("time", "00:00")))
        if now.strftime("%H:%M") < due_time:
            return False
        if not _within_due_window(now, due_time, window_minutes):
            return False
        created_at = int(job.get("created_at", 0))
        if created_at and not last_run:
            created = datetime.fromtimestamp(created_at, tz=now.tzinfo)
            if created.strftime("%Y-%m-%d") == now.strftime("%Y-%m-%d") and created.strftime("%H:%M") > due_time:
                return False
        return last_run.get("date") != now.strftime("%Y-%m-%d")

    if schedule.get("type") == "interval":
        seconds = int(schedule.get("seconds") or parse_interval_seconds(str(schedule.get("every", "1h"))))
        last_ts = int(last_run.get("timestamp") or job.get("created_at") or 0)
        return int(now.timestamp()) - last_ts >= seconds

    return False


def mark_ran(job: dict, now: datetime, state: dict, path: Path) -> None:
    job_id = job["id"]
    state.setdefault("job_last_runs", {})[job_id] = {
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": int(now.timestamp()),
        "path": str(path),
    }
