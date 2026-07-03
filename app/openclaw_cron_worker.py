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


def default_tasks() -> dict:
    return {
        "daily_briefing": {
            "enabled": True,
            "title": "OpenClaw 每日早報",
            "weather_questions": [
                "英國今天天氣如何",
                "英國明天天氣如何",
            ],
            "product_queries": [],
            "english_queries": [
                {
                    "name": "BBC Learning English",
                    "query": "BBC Learning English latest vocabulary lesson",
                }
            ],
        }
    }


def today_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def should_run(now: datetime, settings: Settings, state: dict) -> bool:
    if settings.cron_run_on_start and not state.get("startup_run_done"):
        return True

    hour_text, minute_text = settings.cron_daily_report_time.split(":", 1)
    due_hour = int(hour_text)
    due_minute = int(minute_text)
    if (now.hour, now.minute) < (due_hour, due_minute):
        return False
    return state.get("last_daily_report_date") != today_key(now)


def route_question(router: SkillRouter, text: str) -> str:
    try:
        result = router.route(text)
    except Exception as exc:
        return f"查詢失敗：{exc}"
    return result.answer or "沒有取得回覆。"


def section(title: str, body: str) -> str:
    return f"## {title}\n{body.strip()}\n"


def build_daily_report(settings: Settings, tasks: dict, router: SkillRouter, llm: LlmClient, now: datetime) -> str:
    daily = tasks.get("daily_briefing", {})
    title = daily.get("title", "OpenClaw 每日早報")
    lines = [
        f"# {title}",
        "",
        f"- 生成時間：{now.strftime('%Y-%m-%d %H:%M')} {settings.cron_timezone}",
        f"- 執行主機：GB10 OpenClaw cron worker",
        "",
    ]

    weather_questions = daily.get("weather_questions", [])
    if weather_questions:
        weather_blocks = []
        for question in weather_questions:
            weather_blocks.append(f"- {question}：{route_question(router, question)}")
        lines.append(section("天氣", "\n".join(weather_blocks)))

    product_queries = daily.get("product_queries", [])
    if product_queries:
        product_blocks = []
        for item in product_queries:
            name = item.get("name", item.get("query", "商品"))
            query = item.get("query", name)
            answer = route_question(router, "/search " + query)
            product_blocks.append(f"### {name}\n{answer}")
        lines.append(section("商品價格追蹤", "\n\n".join(product_blocks)))
    else:
        lines.append(section("商品價格追蹤", "尚未設定追蹤品項。請在 `/app/cron_tasks.json` 的 `product_queries` 加入商品名稱與搜尋語句。"))

    english_queries = daily.get("english_queries", [])
    if english_queries:
        english_blocks = []
        for item in english_queries:
            name = item.get("name", item.get("query", "英文教材"))
            query = item.get("query", name)
            search_answer = route_question(router, "/search " + query)
            lesson_prompt = (
                "請根據以下資料，產出一份 5 分鐘英文學習教材。"
                "包含：三個實用單字或片語、兩個例句、一個跟讀練習、一個中文重點提醒。"
                "使用繁體中文說明，英文例句保留英文。\n\n"
                f"主題：{name}\n資料：{search_answer}"
            )
            try:
                lesson = llm.chat(lesson_prompt, max_tokens=360)
            except Exception as exc:
                lesson = (
                    f"英文教材整理暫時失敗：{exc}\n\n"
                    "以下保留原始搜尋摘要，避免整份早報中斷：\n"
                    f"{search_answer}"
                )
            english_blocks.append(f"### {name}\n{lesson}")
        lines.append(section("英文教材", "\n\n".join(english_blocks)))
    else:
        lines.append(section("英文教材", "尚未設定英文教材來源。"))

    lines.append("## 記憶狀態\n本報告會保存到 tracker inbox，memory watcher 會自動索引到 `personal_tracker_memory`。")
    return "\n".join(lines).strip() + "\n"


def save_report(settings: Settings, now: datetime, report: str) -> Path:
    directory = settings.inbox_path / "tracker" / "cron"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now.strftime('%Y%m%d')}-daily-briefing.md"
    path.write_text(report, encoding="utf-8")
    return path


def save_job_report(settings: Settings, now: datetime, job: dict, report: str) -> Path:
    directory = settings.inbox_path / "tracker" / "cron"
    directory.mkdir(parents=True, exist_ok=True)
    safe_id = str(job.get("id", "job")).replace("/", "-")
    path = directory / f"{now.strftime('%Y%m%d-%H%M%S')}-{safe_id}.md"
    path.write_text(report, encoding="utf-8")
    return path


def run_daily_report(settings: Settings, tasks: dict, router: SkillRouter, llm: LlmClient, now: datetime) -> Path:
    report = build_daily_report(settings, tasks, router, llm, now)
    path = save_report(settings, now, report)
    for chat_id in recipients(settings):
        send_message(settings, chat_id, report)
    return path


def run_dynamic_job(settings: Settings, router: SkillRouter, now: datetime, job: dict) -> dict:
    started = time.time()
    prompt = str(job.get("prompt", "")).strip()
    title = str(job.get("name", job.get("id", "OpenClaw cron job"))).strip()
    if not prompt:
        raise ValueError(f"cron job {job.get('id')} has empty prompt")

    status = "ok"
    error = None
    try:
        result = router.route(prompt)
        answer = result.answer or "OpenClaw runtime 回傳了空回覆。"
        skill_name = result.skill_name
    except Exception as exc:
        status = "error"
        error = str(exc)
        answer = f"任務執行失敗：{exc}"
        skill_name = "error"

    report = (
        f"# {title}\n\n"
        f"- Job ID：{job.get('id')}\n"
        f"- 生成時間：{now.strftime('%Y-%m-%d %H:%M')} {settings.cron_timezone}\n"
        f"- Skill：{skill_name}\n\n"
        "## 任務\n"
        f"{prompt}\n\n"
        "## 結果\n"
        f"{answer.strip()}\n"
    )
    path = save_job_report(settings, now, job, report)
    delivered = False
    try:
        send_message(settings, int(job.get("chat_id") or recipients(settings)[0]), report)
        delivered = True
    except Exception as exc:
        status = "error"
        error = f"{error}; Telegram delivery failed: {exc}" if error else f"Telegram delivery failed: {exc}"
    return {
        "path": path,
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
    status = result.get("status") or "ok"
    run_at_ms = int(now.timestamp() * 1000)
    state = {
        **current_state,
        "lastRunAtMs": run_at_ms,
        "lastRunStatus": status,
        "lastStatus": status,
        "lastDurationMs": int(result.get("duration_ms") or 0),
        "lastDeliveryStatus": "delivered" if result.get("delivered") else "not-delivered",
        "lastDelivered": bool(result.get("delivered")),
        "consecutiveErrors": 0 if status == "ok" else previous_errors + 1,
        "consecutiveSkipped": 0,
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
            provider="gb10-vllm",
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
    tasks = load_json(settings.cron_tasks_config_path, default_tasks())
    state = load_json(settings.cron_state_path, {})
    llm = LlmClient(settings)
    router = SkillRouter(settings, llm)
    log(
        f"[cron] started timezone={settings.cron_timezone} "
        f"daily={settings.cron_daily_report_time} recipients={recipients(settings)}"
    )

    while RUNNING:
        try:
            now = datetime.now(tz)
            for job in load_dynamic_jobs(settings):
                if is_due(job, now, state):
                    result = run_dynamic_job(settings, router, now, job)
                    report_path = result["path"]
                    mark_ran(job, now, state, report_path)
                    write_gateway_runback(settings, job, now, result)
                    write_json(settings.cron_state_path, state)
                    log(f"[cron] dynamic job sent id={job.get('id')} path={report_path}")
            if should_run(now, settings, state):
                report_path = run_daily_report(settings, tasks, router, llm, now)
                state["last_daily_report_date"] = today_key(now)
                state["startup_run_done"] = True
                state["last_report_path"] = str(report_path)
                state["last_report_at"] = now.isoformat()
                write_json(settings.cron_state_path, state)
                log(f"[cron] daily report sent path={report_path}")
        except Exception:
            log(traceback.format_exc())
        time.sleep(settings.cron_poll_seconds)

    log("[cron] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
