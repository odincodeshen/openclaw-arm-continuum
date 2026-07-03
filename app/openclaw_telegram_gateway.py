#!/usr/bin/env python3
import json
import mimetypes
from pathlib import Path
import re
import signal
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

from openclaw_runtime.config import load_settings
from openclaw_runtime.agents import AgentRegistry, TaskDispatcher
from openclaw_runtime.agents.skill_agents import ChatAgent, SkillAgent
from openclaw_runtime.cron_jobs import (
    add_daily_job,
    add_interval_job,
    add_monthly_job,
    add_weekly_job,
    delete_job,
    describe_job,
    get_job,
    load_jobs,
)
from openclaw_runtime.gateway_cron import (
    GatewayCronError,
    build_daily,
    build_interval,
    build_monthly,
    build_weekly,
    gateway_job_to_runtime,
    get_gateway_job,
    list_gateway_jobs,
    remove_gateway_job,
    append_gateway_run_log,
    update_gateway_job_state,
    update_gateway_job_state_sqlite,
)
from openclaw_runtime.file_ingest import SUPPORTED_SUFFIXES
from openclaw_runtime.http_client import request_json
from openclaw_runtime.llm_client import LlmClient
from openclaw_runtime.skill_router import SkillRouter
from openclaw_runtime.task_history import TaskHistory
from openclaw_runtime.transcription_client import TranscriptionClient


settings = load_settings()
llm = LlmClient(settings)
transcriber = TranscriptionClient(settings)
skill_router = SkillRouter(settings, llm)
agent_registry = AgentRegistry([SkillAgent(skill) for skill in skill_router.skills] + [ChatAgent(llm)])
task_history = TaskHistory(settings.task_history_path)
task_dispatcher = TaskDispatcher(agent_registry, task_history)

RUNNING = True
ACTIVE_LOCK = threading.Lock()
ACTIVE_REQUESTS = 0
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

HELP_TEXT = """OpenClaw GB10 使用速查

常用指令
/mem <內容>
保存一段個人記憶或工作狀態。
例：/mem OpenClaw 偏好：記憶寫入使用 /mem。

/rag <問題>
查本地記憶與文件知識庫。
例：/rag OpenClaw 接上了哪些模組？

/search <關鍵字>
用本地 Playwright browser 上網搜尋、抓頁、轉 Markdown，
再交給 GB10 vLLM 摘要。
例：/search Arm Neoverse latest news
例：/search 英國明天天氣如何

搜尋結果會落盤到：
/workspace/inbox/tracker/web/*.md

/cron
動態設定主動推播排程。
例：/cron add daily 07:30 早報 :: 英國明天天氣如何，並整理今天需要注意的事
例：/cron add weekly mon 08:00 週報 :: /search latest Arm AI chip news
例：/cron add monthly last 18:00 月底回顧 ：： 回顧本月工作摘要
例：/cron add every 6h 晶片新聞 :: /search latest NVIDIA Arm AI chip news
例：/cron list
例：/cron run <job_id>
例：/cron delete <job_id>

/help
顯示這張速查卡。

/agents
列出目前 OpenClaw agents 與模型政策。

/tasks last
查看最近 5 筆任務紀錄與執行狀態。

自然語言
可以直接問一般問題或天氣。
例：台灣明天天氣如何

文件 RAG
直接上傳 .pdf / .md / .txt / .log / .json / .csv / .tsv 到 Telegram，
或放到 /workspace/inbox/knowledge，
OpenClaw 會保存到 knowledge inbox 並自動進 RAG。

查指定文件時，把檔名放進 /rag。
例：/rag debugger_armv8v9.pdf 這份文件在說什麼
例：/rag debugger_armv8v9.pdf 摘要重點

文件 caption 寫 /tracker
可改存到動態追蹤記憶。

圖片與語音
直接上傳照片，OpenClaw 會保存到 GB10 inbox，
並交給本地 vLLM/VLM 分析。

照片 caption 可當作分析指令。
例：請讀出圖片裡的文字，並整理成重點
例：這張伺服器照片有什麼異常？

上傳語音會保存到 GB10 inbox，先用本地 Whisper 轉文字，
再交給 OpenClaw skills/GB10 vLLM 處理。

語音可以直接講一般問題，也可以講命令內容。
例：記住 OpenClaw 圖片分析要看 caption
例：英國明天天氣如何
"""


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    print(f"{stamp} {message}", flush=True)


def stop(_signum: int, _frame: object) -> None:
    global RUNNING
    RUNNING = False


def telegram(method: str, payload: dict | None = None, timeout: int = 60) -> dict:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    return request_json("POST", url, payload or {}, timeout=timeout)


def telegram_file_info(file_id: str) -> dict:
    data = telegram("getFile", {"file_id": file_id}, timeout=30)
    return data.get("result", {})


def sanitize_filename(name: str, fallback: str) -> str:
    clean = SAFE_FILENAME.sub("-", Path(name).name).strip(".-_")
    return clean or fallback


def extension_from_file_path(file_path: str, mime_type: str | None, fallback: str) -> str:
    suffix = Path(file_path).suffix.lower()
    if suffix:
        return suffix
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type)
        if guessed:
            return guessed
    return fallback


def unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 1000):
        next_candidate = directory / f"{stem}-{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
    raise RuntimeError(f"cannot allocate unique filename in {directory}")


def download_telegram_file(file_id: str, destination: Path) -> tuple[Path, int]:
    info = telegram_file_info(file_id)
    file_path = info.get("file_path")
    if not file_path:
        raise RuntimeError("Telegram did not return a file_path")

    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
    with urllib.request.urlopen(url, timeout=settings.request_timeout) as response:
        data = response.read()
    destination.write_bytes(data)
    return destination, len(data)


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def document_directory(caption: str) -> Path:
    lowered = caption.strip().lower()
    if lowered.startswith("/tracker"):
        return settings.inbox_path / "tracker" / "telegram"
    return settings.inbox_path / "knowledge" / "telegram"


def handle_document_message(chat_id: int, message: dict, caption: str) -> bool:
    document = message.get("document")
    if not document:
        return False

    file_id = document.get("file_id")
    if not file_id:
        send_message(chat_id, "這份文件沒有 Telegram file_id，OpenClaw 無法下載。")
        return True

    original_name = document.get("file_name") or f"telegram-document-{timestamp()}"
    file_info = telegram_file_info(file_id)
    mime_type = document.get("mime_type") or mimetypes.guess_type(original_name)[0] or ""
    suffix = extension_from_file_path(
        file_info.get("file_path", ""),
        mime_type,
        Path(original_name).suffix.lower() or ".bin",
    )
    base_name = sanitize_filename(Path(original_name).stem, "telegram-document")
    target_name = f"{timestamp()}-{base_name}{suffix}"
    target_directory = settings.inbox_path / "media" / "telegram" if mime_type.startswith("image/") else document_directory(caption)
    target_path = unique_path(target_directory, target_name)
    downloaded_path, byte_count = download_telegram_file(file_id, target_path)

    if mime_type.startswith("image/"):
        send_message(
            chat_id,
            "圖片文件已保存，正在交給本地 vLLM/VLM 分析。\n"
            f"檔案：{downloaded_path.name}\n"
            f"大小：{byte_count} bytes",
        )
        worker = threading.Thread(
            target=process_image_message,
            args=(chat_id, downloaded_path, caption),
            daemon=True,
        )
        worker.start()
        log(f"[telegram] saved image-document chat_id={chat_id} path={downloaded_path} bytes={byte_count}")
        return True

    if downloaded_path.suffix.lower() in SUPPORTED_SUFFIXES:
        collection = (
            settings.tracker_collection
            if target_directory.relative_to(settings.inbox_path).parts[0] == "tracker"
            else settings.knowledge_collection
        )
        send_message(
            chat_id,
            "文件已保存，memory watcher 會自動索引。\n"
            f"檔案：{downloaded_path.name}\n"
            f"目標集合：{collection}\n"
            "稍等幾秒後可用 /rag 查詢內容。",
        )
    else:
        send_message(
            chat_id,
            "文件已保存到 GB10 inbox，但目前 watcher 尚未索引這種格式。\n"
            f"檔案：{downloaded_path.name}\n"
            f"大小：{byte_count} bytes\n"
            "PDF/VLM 文件解析會在下一階段接上。",
        )
    log(f"[telegram] saved document chat_id={chat_id} path={downloaded_path} bytes={byte_count}")
    return True


def handle_photo_message(chat_id: int, message: dict) -> bool:
    photos = message.get("photo") or []
    if not photos:
        return False

    photo = photos[-1]
    file_id = photo.get("file_id")
    if not file_id:
        send_message(chat_id, "這張圖片沒有 Telegram file_id，OpenClaw 無法下載。")
        return True

    file_info = telegram_file_info(file_id)
    suffix = extension_from_file_path(file_info.get("file_path", ""), "image/jpeg", ".jpg")
    target_name = sanitize_filename(f"{timestamp()}-telegram-photo{suffix}", f"{timestamp()}-telegram-photo.jpg")
    target_path = unique_path(settings.inbox_path / "media" / "telegram", target_name)
    downloaded_path, byte_count = download_telegram_file(file_id, target_path)
    send_message(
        chat_id,
        "圖片已保存到 GB10 inbox，正在交給本地 vLLM/VLM 分析。\n"
        f"檔案：{downloaded_path.name}\n"
        f"大小：{byte_count} bytes",
    )
    caption = (message.get("caption") or "").strip()
    worker = threading.Thread(
        target=process_image_message,
        args=(chat_id, downloaded_path, caption),
        daemon=True,
    )
    worker.start()
    log(f"[telegram] saved photo chat_id={chat_id} path={downloaded_path} bytes={byte_count}")
    return True


def handle_voice_message(chat_id: int, message: dict) -> bool:
    voice = message.get("voice") or message.get("audio")
    if not voice:
        return False

    file_id = voice.get("file_id")
    if not file_id:
        send_message(chat_id, "這段語音沒有 Telegram file_id，OpenClaw 無法下載。")
        return True

    file_info = telegram_file_info(file_id)
    suffix = extension_from_file_path(file_info.get("file_path", ""), voice.get("mime_type"), ".ogg")
    target_name = sanitize_filename(f"{timestamp()}-telegram-audio{suffix}", f"{timestamp()}-telegram-audio.ogg")
    target_path = unique_path(settings.inbox_path / "audio" / "telegram", target_name)
    downloaded_path, byte_count = download_telegram_file(file_id, target_path)
    send_message(
        chat_id,
        "語音已保存到 GB10 inbox，正在用本地 Whisper 轉錄。\n"
        f"檔案：{downloaded_path.name}\n"
        f"大小：{byte_count} bytes",
    )
    caption = (message.get("caption") or "").strip()
    worker = threading.Thread(
        target=process_voice_message,
        args=(chat_id, downloaded_path, caption),
        daemon=True,
    )
    worker.start()
    log(f"[telegram] saved audio chat_id={chat_id} path={downloaded_path} bytes={byte_count}")
    return True


def process_image_message(chat_id: int, image_path: Path, caption: str) -> None:
    if not settings.vision_enabled:
        send_message(chat_id, "圖片已保存，但 OPENCLAW_VISION_ENABLED=false，目前不做 VLM 分析。")
        return

    prompt = caption.strip() or "請分析這張圖片，描述重點、可見文字、可能的問題，以及下一步建議。"
    try:
        answer = llm.chat_with_image(image_path, prompt)
    except Exception as exc:
        log(f"[vision] error chat_id={chat_id} path={image_path}: {exc}")
        send_message(
            chat_id,
            "圖片已保存，但目前 vLLM 沒有成功處理 image input。\n"
            f"原因：{exc}\n\n"
            "若目前模型是文字模型，請把 OPENCLAW_VLLM_MODEL 換成 VLM，例如 Qwen2.5-VL 或 Llama Vision，再重啟 vLLM。",
        )
        return

    send_message(chat_id, answer or "VLM 沒有回傳圖片分析內容。")
    log(f"[vision] done chat_id={chat_id} path={image_path} answer_chars={len(answer or '')}")


def process_voice_message(chat_id: int, audio_path: Path, caption: str) -> None:
    if not settings.whisper_enabled:
        send_message(chat_id, "語音已保存，但 OPENCLAW_WHISPER_ENABLED=false，目前不做轉錄。")
        return

    try:
        transcript = transcriber.transcribe(audio_path)
    except Exception as exc:
        log(f"[whisper] error chat_id={chat_id} path={audio_path}: {exc}")
        send_message(chat_id, f"語音已保存，但 Whisper 轉錄失敗：{exc}")
        return

    if not transcript:
        send_message(chat_id, "Whisper 沒有從這段語音轉出文字。")
        return

    send_message(chat_id, f"Whisper 轉錄：\n{transcript}")
    routed_text = transcript if not caption else f"{caption}\n\n語音轉錄：{transcript}"
    try:
        dispatch = task_dispatcher.dispatch(
            routed_text,
            source="telegram_voice",
            chat_id=chat_id,
            metadata={"audio_path": str(audio_path)},
        )
    except Exception as exc:
        log(f"[voice-runtime] error chat_id={chat_id}: {exc}")
        send_message(chat_id, f"語音已轉錄，但 OpenClaw runtime 無法回應：{exc}")
        return

    send_message(chat_id, dispatch.answer or "OpenClaw runtime 回傳了空回覆。")
    log(
        f"[voice-runtime] done chat_id={chat_id} task_id={dispatch.task_id} agent={dispatch.agent_name} "
        f"transcript_chars={len(transcript)} answer_chars={len(dispatch.answer or '')}"
    )


def send_message(chat_id: int, text: str) -> None:
    if len(text) <= settings.max_reply_chars:
        telegram("sendMessage", {"chat_id": chat_id, "text": text})
        return
    for start in range(0, len(text), settings.max_reply_chars):
        telegram(
            "sendMessage",
            {"chat_id": chat_id, "text": text[start : start + settings.max_reply_chars]},
        )


def cron_help_text() -> str:
    return """OpenClaw Cron 動態排程

/cron list
列出目前排程。

/cron add daily HH:MM 名稱 :: 任務內容
每天指定時間執行。
例：/cron add daily 07:30 早報 :: 英國明天天氣如何，並整理今天需要注意的事
也接受中文輸入法的 ：：
例：/cron add daily 07:30 早報 ：： 英國明天天氣如何

/cron add weekly mon|tue|wed|thu|fri|sat|sun HH:MM 名稱 :: 任務內容
每週指定星期與時間執行。
例：/cron add weekly mon 08:00 週報 :: /search latest Arm AI chip news

/cron add monthly 1-31|last HH:MM 名稱 :: 任務內容
每月指定日期與時間執行。
例：/cron add monthly 1 09:00 月報 :: 整理本月 personal_tracker_memory 重點
例：/cron add monthly last 18:00 月底回顧 ：： 回顧本月工作摘要

/cron add every 30m|2h|1d 名稱 :: 任務內容
每隔一段時間執行。
例：/cron add every 6h 晶片新聞 :: /search latest NVIDIA Arm AI chip news
新增後會先等待完整間隔，不會立刻執行。

/cron run <job_id>
立即執行一次。

/cron delete <job_id>
刪除排程。
"""


def agents_text() -> str:
    lines = ["OpenClaw Agents"]
    for status in agent_registry.statuses():
        lines.append(f"- {status.name}: {status.status} ({status.model_policy})")
        lines.append(f"  {status.description}")
    return "\n".join(lines)


def tasks_text(limit: int = 5) -> str:
    entries = task_history.recent(limit)
    if not entries:
        return "目前沒有 task history。"
    lines = [f"最近 {len(entries)} 筆 OpenClaw tasks"]
    for entry in entries:
        task_id = str(entry.get("task_id", ""))[-18:] or "(no-id)"
        agent = entry.get("agent", "(unknown-agent)")
        status = entry.get("status", "(unknown)")
        duration = entry.get("duration_ms")
        duration_text = f" {duration}ms" if duration is not None else ""
        summary = entry.get("input_summary") or entry.get("error") or ""
        lines.append(f"- {task_id} {agent} {status}{duration_text}")
        if summary:
            lines.append(f"  {summary[:180]}")
    return "\n".join(lines)


def run_cron_job_once(chat_id: int, job_id: str) -> None:
    started = time.time()
    job = None
    try:
        gateway_job = get_gateway_job(settings, job_id)
        if gateway_job:
            job = gateway_job_to_runtime(gateway_job, chat_id)
    except Exception:
        job = None
    if not job:
        job = get_job(settings.cron_jobs_path, job_id)
    if not job:
        send_message(chat_id, f"找不到 cron job：{job_id}")
        return
    send_message(chat_id, f"收到，正在立即執行 cron job：{job.get('name', job_id)}")
    try:
        dispatch = task_dispatcher.dispatch(
            str(job.get("prompt", "")),
            source="telegram_cron_run",
            chat_id=chat_id,
            metadata={"job_id": job_id, "job_name": job.get("name")},
        )
    except Exception as exc:
        send_message(chat_id, f"cron job 執行失敗：{exc}")
        write_manual_runback(job, "error", str(exc), str(exc), int(started * 1000), int((time.time() - started) * 1000), False)
        return
    answer = dispatch.answer or "OpenClaw runtime 回傳了空回覆。"
    delivered = False
    try:
        send_message(chat_id, answer)
        delivered = True
    finally:
        write_manual_runback(job, "ok", answer, None, int(started * 1000), int((time.time() - started) * 1000), delivered)


def write_manual_runback(job: dict, status: str, summary: str, error: str | None, run_at_ms: int, duration_ms: int, delivered: bool) -> None:
    job_id = str(job.get("id") or "")
    if not job_id:
        return
    current_state = dict(job.get("gateway_state") or {})
    state = {
        **current_state,
        "lastRunAtMs": run_at_ms,
        "lastRunStatus": status,
        "lastStatus": status,
        "lastDurationMs": duration_ms,
        "lastDeliveryStatus": "delivered" if delivered else "not-delivered",
        "lastDelivered": delivered,
        "lastError": error,
        "consecutiveErrors": 0 if status == "ok" else int(current_state.get("consecutiveErrors") or 0) + 1,
        "consecutiveSkipped": 0,
    }
    try:
        update_gateway_job_state(settings, job_id, state)
    except Exception as exc:
        log(f"[cron-run] Gateway cron.update failed id={job_id}: {exc}")
    try:
        update_gateway_job_state_sqlite(settings, job_id, state)
        append_gateway_run_log(
            settings,
            job_id,
            status=status,
            summary=summary,
            error=error,
            run_at_ms=run_at_ms,
            duration_ms=duration_ms,
            next_run_at_ms=state.get("nextRunAtMs"),
            delivered=delivered,
            model=settings.vllm_model,
            provider="gb10-vllm",
        )
    except Exception as exc:
        log(f"[cron-run] Gateway run history writeback failed id={job_id}: {exc}")


def handle_cron_command(chat_id: int, text: str) -> bool:
    if not text.startswith("/cron"):
        return False

    parts = text.split(maxsplit=5)
    if len(parts) == 1 or parts[1] in {"help", "?"}:
        send_message(chat_id, cron_help_text())
        return True

    action = parts[1].lower()
    if action == "list":
        try:
            jobs = [
                runtime_job
                for job in list_gateway_jobs(settings, include_disabled=True)
                if (runtime_job := gateway_job_to_runtime(job, chat_id))
            ]
        except Exception:
            jobs = load_jobs(settings.cron_jobs_path).get("jobs", [])
        if not jobs:
            send_message(chat_id, "目前沒有動態 cron job。")
            return True
        send_message(chat_id, "目前 cron job（Gateway dashboard 同步）：\n" + "\n".join(describe_job(job) for job in jobs))
        return True

    if action == "delete":
        if len(parts) < 3:
            send_message(chat_id, "請提供 job_id。\n例：/cron delete morning-brief-1780000000")
            return True
        try:
            removed = remove_gateway_job(settings, parts[2])
        except GatewayCronError:
            removed = delete_job(settings.cron_jobs_path, parts[2])
        send_message(chat_id, "已刪除。" if removed else f"找不到 cron job：{parts[2]}")
        return True

    if action == "run":
        if len(parts) < 3:
            send_message(chat_id, "請提供 job_id。\n例：/cron run morning-brief-1780000000")
            return True
        worker = threading.Thread(target=run_cron_job_once, args=(chat_id, parts[2]), daemon=True)
        worker.start()
        return True

    if action == "add":
        if len(parts) < 5:
            send_message(chat_id, cron_help_text())
            return True
        schedule_type = parts[2].lower()
        try:
            if schedule_type == "daily":
                raw = parts[4] if len(parts) == 5 else parts[4] + " " + parts[5]
                try:
                    job = gateway_job_to_runtime(build_daily(settings, chat_id, parts[3], raw), chat_id)
                except GatewayCronError:
                    job = add_daily_job(settings.cron_jobs_path, chat_id, parts[3], raw)
            elif schedule_type == "every":
                raw = parts[4] if len(parts) == 5 else parts[4] + " " + parts[5]
                try:
                    job = gateway_job_to_runtime(build_interval(settings, chat_id, parts[3], raw), chat_id)
                except GatewayCronError:
                    job = add_interval_job(settings.cron_jobs_path, chat_id, parts[3], raw)
            elif schedule_type == "weekly":
                if len(parts) < 6:
                    send_message(chat_id, cron_help_text())
                    return True
                try:
                    job = gateway_job_to_runtime(build_weekly(settings, chat_id, parts[3], parts[4], parts[5]), chat_id)
                except GatewayCronError:
                    job = add_weekly_job(settings.cron_jobs_path, chat_id, parts[3], parts[4], parts[5])
            elif schedule_type == "monthly":
                if len(parts) < 6:
                    send_message(chat_id, cron_help_text())
                    return True
                try:
                    job = gateway_job_to_runtime(build_monthly(settings, chat_id, parts[3], parts[4], parts[5]), chat_id)
                except GatewayCronError:
                    job = add_monthly_job(settings.cron_jobs_path, chat_id, parts[3], parts[4], parts[5])
            else:
                send_message(chat_id, "排程類型支援 daily、weekly、monthly 或 every。")
                return True
        except Exception as exc:
            send_message(chat_id, f"新增 cron job 失敗：{exc}\n\n{cron_help_text()}")
            return True
        send_message(chat_id, "已新增 cron job（可在 Gateway dashboard 管理）：\n" + describe_job(job))
        return True

    send_message(chat_id, cron_help_text())
    return True


def setup_bot_commands() -> None:
    commands = [
        {"command": "help", "description": "顯示 OpenClaw 使用速查"},
        {"command": "mem", "description": "寫入本地記憶"},
        {"command": "rag", "description": "查本地記憶與知識庫"},
        {"command": "search", "description": "搜尋網路資料"},
        {"command": "cron", "description": "設定主動推播排程"},
        {"command": "agents", "description": "列出 OpenClaw agents"},
        {"command": "tasks", "description": "查看最近任務紀錄"},
        {"command": "start", "description": "顯示狀態與用法"},
    ]
    telegram("setMyCommands", {"commands": commands}, timeout=20)


def ack_message(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith(("/mem ", "/remember ", "mem:", "remember:")):
        return "收到，正在寫入本地記憶。"
    if lowered.startswith(("/rag ", "rag:")):
        return "收到，正在查詢本地記憶與文件知識庫。"
    if lowered.startswith(("/search ", "web:")) or any(
        keyword in text for keyword in ("搜尋", "查詢", "查一下", "最新", "新聞")
    ):
        return "收到，正在搜尋網路資料並交給 GB10 摘要。"
    if any(keyword in text for keyword in ("天氣", "氣溫", "下雨", "降雨", "weather")):
        return "收到，正在查詢天氣資料。"
    return "收到，正在交給 GB10 本地 vLLM 回答。"


def handle_text_message(chat_id: int, text: str) -> None:
    global ACTIVE_REQUESTS
    with ACTIVE_LOCK:
        ACTIVE_REQUESTS += 1
        active = ACTIVE_REQUESTS
    try:
        log(f"[runtime] start chat_id={chat_id} active={active}")
        send_message(chat_id, ack_message(text))
        try:
            dispatch = task_dispatcher.dispatch(text, source="telegram_text", chat_id=chat_id)
        except Exception as exc:
            log(f"[runtime] error chat_id={chat_id}: {exc}")
            send_message(chat_id, f"OpenClaw runtime 目前無法回應：{exc}")
            return
        send_message(chat_id, dispatch.answer or "OpenClaw runtime 回傳了空回覆。")
        log(
            f"[runtime] done chat_id={chat_id} task_id={dispatch.task_id} agent={dispatch.agent_name} "
            f"duration_ms={dispatch.duration_ms} answer_chars={len(dispatch.answer or '')}"
        )
    finally:
        with ACTIVE_LOCK:
            ACTIVE_REQUESTS -= 1


def handle_message(message: dict) -> None:
    chat = message.get("chat", {})
    chat_id = int(chat.get("id"))
    if settings.telegram_allowed_chat_ids and chat_id not in settings.telegram_allowed_chat_ids:
        log(f"[telegram] rejected chat_id={chat_id}")
        return

    text = (message.get("text") or message.get("caption") or "").strip()

    try:
        if handle_document_message(chat_id, message, text):
            return
        if handle_photo_message(chat_id, message):
            return
        if handle_voice_message(chat_id, message):
            return
    except Exception as exc:
        log(f"[telegram] file handling error chat_id={chat_id}: {exc}")
        send_message(chat_id, f"OpenClaw 無法保存這個 Telegram 檔案：{exc}")
        return

    if not text:
        send_message(chat_id, "目前 OpenClaw clean runtime 支援文字、文件、圖片與語音落盤。請傳文字或上傳檔案。")
        return

    if text in {"/start", "/help"}:
        send_message(chat_id, HELP_TEXT)
        return

    if text == "/agents":
        send_message(chat_id, agents_text())
        return

    if text == "/tasks" or text.startswith("/tasks "):
        parts = text.split()
        limit = 5
        if len(parts) >= 3 and parts[1] == "last":
            try:
                limit = max(1, min(int(parts[2]), 20))
            except ValueError:
                limit = 5
        send_message(chat_id, tasks_text(limit))
        return

    if handle_cron_command(chat_id, text):
        return

    log(f"[telegram] chat_id={chat_id} text_chars={len(text)}")
    worker = threading.Thread(target=handle_text_message, args=(chat_id, text), daemon=True)
    worker.start()


def main() -> int:
    if not settings.telegram_bot_token:
        log("OPENCLAW_TELEGRAM_BOT_TOKEN is required")
        return 2

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    offset = 0
    me = telegram("getMe", timeout=20)
    setup_bot_commands()
    log(f"[telegram] connected as @{me.get('result', {}).get('username', 'unknown')}")
    log(f"[vllm] endpoint={settings.vllm_base_url} model={settings.vllm_model}")
    log(f"[skills] loaded={[skill.name for skill in skill_router.skills]}")
    log(f"[agents] loaded={[agent.name for agent in agent_registry.agents]}")

    while RUNNING:
        try:
            updates = telegram(
                "getUpdates",
                {"offset": offset, "timeout": settings.telegram_poll_timeout, "allowed_updates": ["message"]},
                timeout=settings.telegram_poll_timeout + 10,
            )
            for update in updates.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                message = update.get("message")
                if message:
                    handle_message(message)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            log(f"[telegram] http error {exc.code}: {body}")
            time.sleep(5)
        except Exception:
            log(traceback.format_exc())
            time.sleep(5)
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
