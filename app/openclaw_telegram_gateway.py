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
from openclaw_runtime.agents.base import Task
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
from openclaw_runtime.source_ingest import save_google_doc
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

HELP_TEXT = f"""OpenClaw Arm Continuum quick reference

Common commands
/mem <content>
Save a piece of personal memory or working context.
Example: /mem OpenClaw preference: use /mem for memory writes.

/rag <question>
Query local memory and document knowledge base.
Example: /rag Which modules is OpenClaw connected to?

/search <keywords>
Browse and scrape the web with the local Playwright worker, convert to
Markdown, then hand it to the local reasoning model for a summary.
Example: /search Arm Neoverse latest news
Example: /search What tech news happened today

For plain weather questions, do not add /search -- ask in natural
language instead (see "Natural language" below). /search always forces
a general web search, bypassing the dedicated weather lookup.

Search results are saved to:
/workspace/inbox/tracker/web/*.md

/cron
Configure dynamic proactive push schedules.
Example: /cron add daily 07:30 Morning briefing :: UK weather tomorrow, and summarize today's priorities
Example: /cron add weekly mon 08:00 Weekly roundup :: /search latest Arm AI chip news
Example: /cron add monthly last 18:00 Month-end review :: Summarize this month's work
Example: /cron add every 6h Chip news :: /search latest NVIDIA Arm AI chip news
Example: /cron list
Example: /cron run <job_id>
Example: /cron delete <job_id>

/help
Show this quick reference card.

/agents
List current OpenClaw agents and their model policy.

/tasks last
View the 5 most recent task history entries and their status.

/doc url <Google Doc URL>
Import a public Google Doc, save it as Markdown, and index it into
knowledge RAG automatically.
Example: /doc url https://docs.google.com/document/d/.../edit
Example: /doc url https://docs.google.com/document/d/.../edit tracker

Natural language
You can ask general questions or about the weather directly.
Example: What's the weather like in Taiwan tomorrow?

Document RAG
Upload .pdf / .md / .txt / .log / .json / .csv / .tsv directly to
Telegram, or drop them into /workspace/inbox/knowledge. OpenClaw saves
them to the knowledge inbox and indexes them into RAG automatically.

To ask about a specific file, put the filename in /rag.
Example: /rag debugger_armv8v9.pdf What is this document about?
Example: /rag debugger_armv8v9.pdf Summarize the key points

Caption a document with /mem or /tracker to save it to dynamic
tracker memory instead.

Photos and voice
Upload a photo directly and OpenClaw will save it to the
{settings.runtime_label} inbox and hand it to the local vLLM/VLM for
analysis.

A photo caption can double as an analysis instruction.
Example: Read out the text in this image and summarize the key points
Example: Does anything look wrong in this server photo?

Uploaded voice messages are saved to the {settings.runtime_label}
inbox, transcribed locally with Whisper, then handed to OpenClaw
skills and the local reasoning model.

Voice can ask a general question or speak a command directly.
Example: Remember that OpenClaw image analysis should read the caption
Example: What's the weather like in the UK tomorrow?
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
    if lowered.startswith("/tracker") or lowered.startswith("/mem"):
        return settings.inbox_path / "tracker" / "telegram"
    return settings.inbox_path / "knowledge" / "telegram"


def handle_document_message(chat_id: int, message: dict, caption: str) -> bool:
    document = message.get("document")
    if not document:
        return False

    file_id = document.get("file_id")
    if not file_id:
        send_message(chat_id, "This document has no Telegram file_id, so OpenClaw cannot download it.")
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
            "Image document saved, handing it to the local vLLM/VLM for analysis.\n"
            f"File: {downloaded_path.name}\n"
            f"Size: {byte_count} bytes",
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
            "File saved, the memory watcher will index it automatically.\n"
            f"File: {downloaded_path.name}\n"
            f"Target collection: {collection}\n"
            "Wait a few seconds, then query it with /rag.",
        )
    else:
        send_message(
            chat_id,
            f"File saved to the {settings.runtime_label} inbox, but the watcher does not yet index this format.\n"
            f"File: {downloaded_path.name}\n"
            f"Size: {byte_count} bytes\n"
            "PDF/VLM document parsing will be added in a later stage.",
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
        send_message(chat_id, "This photo has no Telegram file_id, so OpenClaw cannot download it.")
        return True

    file_info = telegram_file_info(file_id)
    suffix = extension_from_file_path(file_info.get("file_path", ""), "image/jpeg", ".jpg")
    target_name = sanitize_filename(f"{timestamp()}-telegram-photo{suffix}", f"{timestamp()}-telegram-photo.jpg")
    target_path = unique_path(settings.inbox_path / "media" / "telegram", target_name)
    downloaded_path, byte_count = download_telegram_file(file_id, target_path)
    send_message(
        chat_id,
        f"Photo saved to the {settings.runtime_label} inbox, handing it to the local vLLM/VLM for analysis.\n"
        f"File: {downloaded_path.name}\n"
        f"Size: {byte_count} bytes",
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
        send_message(chat_id, "This voice message has no Telegram file_id, so OpenClaw cannot download it.")
        return True

    file_info = telegram_file_info(file_id)
    suffix = extension_from_file_path(file_info.get("file_path", ""), voice.get("mime_type"), ".ogg")
    target_name = sanitize_filename(f"{timestamp()}-telegram-audio{suffix}", f"{timestamp()}-telegram-audio.ogg")
    target_path = unique_path(settings.inbox_path / "audio" / "telegram", target_name)
    downloaded_path, byte_count = download_telegram_file(file_id, target_path)
    send_message(
        chat_id,
        f"Voice message saved to the {settings.runtime_label} inbox, transcribing with local Whisper.\n"
        f"File: {downloaded_path.name}\n"
        f"Size: {byte_count} bytes",
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
        send_message(chat_id, "Image saved, but OPENCLAW_VISION_ENABLED=false, so no VLM analysis was run.")
        return

    prompt = caption.strip() or "Analyze this image: describe the key points, any visible text, possible issues, and suggested next steps."
    try:
        answer = llm.chat_with_image(image_path, prompt)
    except Exception as exc:
        log(f"[vision] error chat_id={chat_id} path={image_path}: {exc}")
        send_message(
            chat_id,
            "Image saved, but vLLM failed to process the image input.\n"
            f"Reason: {exc}\n\n"
            "If the current model is text-only, switch OPENCLAW_VLLM_MODEL to a VLM such as Qwen2.5-VL or Llama Vision, then restart vLLM.",
        )
        return

    send_message(chat_id, answer or "The VLM did not return any image analysis content.")
    log(f"[vision] done chat_id={chat_id} path={image_path} answer_chars={len(answer or '')}")


def process_voice_message(chat_id: int, audio_path: Path, caption: str) -> None:
    if not settings.whisper_enabled:
        send_message(chat_id, "Voice message saved, but OPENCLAW_WHISPER_ENABLED=false, so no transcription was run.")
        return

    try:
        transcript = transcriber.transcribe(audio_path)
    except Exception as exc:
        log(f"[whisper] error chat_id={chat_id} path={audio_path}: {exc}")
        send_message(chat_id, f"Voice message saved, but Whisper transcription failed: {exc}")
        return

    if not transcript:
        send_message(chat_id, "Whisper did not produce any text from this voice message.")
        return

    send_message(chat_id, f"Whisper transcript:\n{transcript}")
    routed_text = transcript if not caption else f"{caption}\n\nVoice transcript: {transcript}"
    try:
        dispatch = task_dispatcher.dispatch(
            routed_text,
            source="telegram_voice",
            chat_id=chat_id,
            metadata={"audio_path": str(audio_path)},
        )
    except Exception as exc:
        log(f"[voice-runtime] error chat_id={chat_id}: {exc}")
        send_message(chat_id, f"Voice message transcribed, but the OpenClaw runtime failed to respond: {exc}")
        return

    send_message(chat_id, dispatch.answer or "The OpenClaw runtime returned an empty reply.")
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
    return """OpenClaw dynamic cron schedules

/cron list
List current schedules.

/cron add daily HH:MM name :: task content
Run at a fixed time every day.
Example: /cron add daily 07:30 Morning briefing :: UK weather tomorrow, and summarize today's priorities
Also accepts the full-width "：：" from Chinese input methods.
Example: /cron add daily 07:30 Morning briefing ：： UK weather tomorrow

/cron add weekly mon|tue|wed|thu|fri|sat|sun HH:MM name :: task content
Run on a fixed weekday and time every week.
Example: /cron add weekly mon 08:00 Weekly roundup :: /search latest Arm AI chip news

/cron add monthly 1-31|last HH:MM name :: task content
Run on a fixed day of the month.
Example: /cron add monthly 1 09:00 Monthly report :: Summarize this month's personal_tracker_memory highlights
Example: /cron add monthly last 18:00 Month-end review ：： Review this month's work summary

/cron add every 30m|2h|1d name :: task content
Run on a recurring interval.
Example: /cron add every 6h Chip news :: /search latest NVIDIA Arm AI chip news
The first run waits a full interval after creation; it does not fire immediately.

/cron run <job_id>
Run a schedule immediately.

/cron delete <job_id>
Delete a schedule.
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
        return "No task history yet."
    lines = [f"{len(entries)} most recent OpenClaw tasks"]
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


def doc_help_text() -> str:
    return """OpenClaw document sources

/doc url <Google Doc URL>
Import a public Google Doc, save it as Markdown, and index it into
knowledge RAG automatically.
Example: /doc url https://docs.google.com/document/d/.../edit

/doc url <Google Doc URL> tracker
Import a public Google Doc into tracker memory, suited for tracking
data, logs, or temporary context.
Example: /doc url https://docs.google.com/document/d/.../edit tracker

Note: the Google Doc must be publicly readable, or shared as "anyone
with the link can view". After importing, wait for the memory watcher
to index it, then query with /rag.
"""


def handle_doc_command(chat_id: int, text: str) -> bool:
    if not text.startswith("/doc"):
        return False

    parts = text.split(maxsplit=3)
    if len(parts) == 1 or parts[1] in {"help", "?"}:
        send_message(chat_id, doc_help_text())
        return True

    action = parts[1].lower()
    if action != "url":
        send_message(chat_id, doc_help_text())
        return True
    if len(parts) < 3:
        send_message(chat_id, "Please provide a Google Doc URL.\nExample: /doc url https://docs.google.com/document/d/.../edit")
        return True

    rest = parts[2] if len(parts) == 3 else f"{parts[2]} {parts[3]}"
    url, collection_kind = parse_doc_url_args(rest)
    send_message(chat_id, f"Got it, importing the public Google Doc into {collection_kind}.")
    worker = threading.Thread(target=process_doc_url, args=(chat_id, url, collection_kind), daemon=True)
    worker.start()
    return True


def parse_doc_url_args(rest: str) -> tuple[str, str]:
    tokens = rest.strip().split()
    if not tokens:
        raise ValueError("missing url")
    url = tokens[0]
    collection_kind = "knowledge"
    if len(tokens) > 1 and tokens[-1].lower() in {"tracker", "memory", "mem"}:
        collection_kind = "tracker"
    return url, collection_kind


def process_doc_url(chat_id: int, url: str, collection_kind: str) -> None:
    try:
        result = save_google_doc(settings, url, collection_kind)
    except Exception as exc:
        log(f"[doc-url] error chat_id={chat_id}: {exc}")
        send_message(chat_id, f"Google Doc import failed: {exc}")
        return

    preview = ""
    try:
        text = result.path.read_text(encoding="utf-8", errors="replace")
        prompt = (
            "Write a short, mobile-friendly summary of the following Google Doc content. "
            "Include: the topic, three key points, and whether it's worth a deeper /rag query. "
            "Do not output your reasoning process.\n\n"
            f"Title: {result.title}\n"
            f"Content: {text[:6000]}"
        )
        preview = llm.chat(prompt, max_tokens=260)
    except Exception as exc:
        log(f"[doc-url] preview failed chat_id={chat_id} path={result.path}: {exc}")

    collection_name = settings.tracker_collection if result.collection_kind == "tracker" else settings.knowledge_collection
    message = (
        "Google Doc imported.\n"
        f"Title: {result.title}\n"
        f"File: {result.path.name}\n"
        f"Length: ~{result.char_count} chars\n"
        f"Target collection: {collection_name}\n"
        "Once the memory watcher indexes it, you can query it with:\n"
        f"/rag {result.path.name} What is this Google Doc about?"
    )
    if preview:
        message += f"\n\nSummary:\n{preview}"
    send_message(chat_id, message)
    log(f"[doc-url] imported chat_id={chat_id} path={result.path} chars={result.char_count}")


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
        send_message(chat_id, f"Cron job not found: {job_id}")
        return
    send_message(chat_id, f"Got it, running cron job now: {job.get('name', job_id)}")
    try:
        dispatch = task_dispatcher.dispatch(
            str(job.get("prompt", "")),
            source="telegram_cron_run",
            chat_id=chat_id,
            metadata={"job_id": job_id, "job_name": job.get("name")},
        )
    except Exception as exc:
        send_message(chat_id, f"Cron job failed: {exc}")
        write_manual_runback(job, "error", str(exc), str(exc), int(started * 1000), int((time.time() - started) * 1000), False)
        return
    answer = dispatch.answer or "The OpenClaw runtime returned an empty reply."
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
            provider="local-llm",
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
            send_message(chat_id, "No dynamic cron jobs yet.")
            return True
        send_message(chat_id, "Current cron jobs (synced with the Gateway dashboard):\n" + "\n".join(describe_job(job) for job in jobs))
        return True

    if action == "delete":
        if len(parts) < 3:
            send_message(chat_id, "Please provide a job_id.\nExample: /cron delete morning-brief-1780000000")
            return True
        try:
            removed = remove_gateway_job(settings, parts[2])
        except GatewayCronError:
            removed = delete_job(settings.cron_jobs_path, parts[2])
        send_message(chat_id, "Deleted." if removed else f"Cron job not found: {parts[2]}")
        return True

    if action == "run":
        if len(parts) < 3:
            send_message(chat_id, "Please provide a job_id.\nExample: /cron run morning-brief-1780000000")
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
                send_message(chat_id, "Supported schedule types: daily, weekly, monthly, or every.")
                return True
        except Exception as exc:
            send_message(chat_id, f"Failed to add cron job: {exc}\n\n{cron_help_text()}")
            return True
        send_message(chat_id, "Cron job added (manageable from the Gateway dashboard):\n" + describe_job(job))
        return True

    send_message(chat_id, cron_help_text())
    return True


def setup_bot_commands() -> None:
    commands = [
        {"command": "help", "description": "Show the OpenClaw quick reference"},
        {"command": "mem", "description": "Write to local memory"},
        {"command": "rag", "description": "Query local memory and knowledge base"},
        {"command": "doc", "description": "Import a public Google Doc or document source"},
        {"command": "search", "description": "Search the web"},
        {"command": "cron", "description": "Configure proactive push schedules"},
        {"command": "agents", "description": "List OpenClaw agents"},
        {"command": "tasks", "description": "View recent task history"},
        {"command": "start", "description": "Show status and usage"},
    ]
    telegram("setMyCommands", {"commands": commands}, timeout=20)


ACK_MESSAGES = {
    "memory_agent": "Got it, writing to local memory.",
    "rag_agent": "Got it, querying local memory and the document knowledge base.",
    "browser_search_agent": "Got it, searching the web and summarizing with the local reasoning model.",
    "weather_agent": "Got it, checking the weather.",
}
DEFAULT_ACK_MESSAGE = "Got it, handing this to the local reasoning model."


def ack_message(text: str) -> str:
    # Ask the same agent_registry that will actually handle the message,
    # instead of a second hand-rolled keyword-priority list that can silently
    # drift out of sync with the real routing order (as it did before).
    task = Task(task_id="ack-preview", source="ack_preview", text=text)
    try:
        agent = agent_registry.find(task)
    except LookupError:
        return DEFAULT_ACK_MESSAGE
    return ACK_MESSAGES.get(agent.name, DEFAULT_ACK_MESSAGE)


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
            send_message(chat_id, f"The OpenClaw runtime could not respond right now: {exc}")
            return
        send_message(chat_id, dispatch.answer or "The OpenClaw runtime returned an empty reply.")
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
        send_message(chat_id, f"OpenClaw could not save this Telegram file: {exc}")
        return

    if not text:
        send_message(chat_id, "The OpenClaw runtime currently accepts text, documents, photos, and voice messages. Please send text or upload a file.")
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

    if handle_doc_command(chat_id, text):
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
