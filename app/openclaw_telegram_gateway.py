#!/usr/bin/env python3
import dataclasses
from collections import OrderedDict
import hashlib
import json
import mimetypes
import shutil
from pathlib import Path
import re
import signal
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from openclaw_runtime.audio_clip_client import AudioClipClient
from openclaw_runtime.categories import (
    parse_category_caption,
    parse_two_category_names,
    registry_entries,
    remove_registry_entry,
    rename_registry_entry,
    resolve_category,
    upsert_registry_entry,
    validate_category_name,
)
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
    validate_time,
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
from openclaw_runtime.conversation_memory import ConversationMemory
from openclaw_runtime.embedding_client import EmbeddingClient
from openclaw_runtime.english_bot_scheduler import (
    clear_prepared,
    day_code_for,
    deliver_outbox,
    mark_prepare_attempted,
    prepare_todays_push,
    prepared_for,
    should_prepare_today,
    store_prepared,
    dispatch_pending_reply,
    load_json as load_english_bot_state,
    mark_pushed_today,
    mark_swept_today,
    run_todays_push,
    run_todays_sweep,
    should_push_today,
    should_sweep_today,
    write_json as write_english_bot_state,
)
from openclaw_runtime.file_ingest import META_SIDECAR_SUFFIX, SUPPORTED_SUFFIXES
from openclaw_runtime.engineering_review import EngineeringReviewAgent
from openclaw_runtime.http_client import post_multipart_file, request_json
from openclaw_runtime.llm_client import VLLM_NOT_READY_MESSAGE
from openclaw_runtime.alerts import Alerter
from openclaw_runtime.dictionary import LocalDictionary
from openclaw_runtime.message_cards import RULE, bold, esc, html_to_plain, split_html_message
from openclaw_runtime.skills.memory import MemoryWriteSkill, sources_for_answer
from openclaw_runtime.tts_client import TtsClient
from openclaw_runtime.anki_package import AnkiNote, write_apkg
from openclaw_runtime.upload_hints import find_duplicate, preview_text, suggest_category
from openclaw_runtime.housekeeping import run_cleanup, write_status
from openclaw_runtime.skills.english_bot import (
    read_this_week_chunks,
    read_this_week_payload,
    record_gist_choice,
    render_gist_card,
)
from openclaw_runtime.night_ritual import (
    NightStore,
    build_report,
    close_status,
    closing_line,
    condense_voice_answer,
    new_entry,
    next_step,
    parse_ritual_days,
    render_closing,
    render_first_check,
    render_first_check_answered,
    render_history,
    render_morning,
    render_question,
    render_reminder,
    reports_due,
)
from openclaw_runtime.model_catalog import load_model_registry
from openclaw_runtime.model_client_factory import ModelClientFactory
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.skill_router import SkillRouter
from openclaw_runtime.source_ingest import save_google_doc
from openclaw_runtime.task_history import TaskHistory
from openclaw_runtime.transcription_client import TranscriptionClient
from openclaw_runtime.vision_client import DEFAULT_DESCRIBE_INSTRUCTION, VisionClient, VisionError
from openclaw_runtime.vocabulary import (
    LIST_LIMIT,
    LOOKUP_USAGE,
    MAX_LOOKUP_WORDS,
    TOO_LONG_MESSAGE,
    count_due_words,
    grade_review,
    list_word_list,
    lookup_word,
    parse_bare_lookup,
    parse_lookup_command,
    record_review,
    remove_from_word_list,
    render_anki_tsv,
    render_lookup_card,
    build_quiz,
    pronunciation_matches,
    render_quiz_question,
    render_quiz_summary,
    render_review_quiz,
    render_say_prompt,
    render_say_result,
    render_review_reminder,
    render_self_check_question,
    render_self_check_summary,
    render_word_list,
    save_to_word_list,
    start_review,
)


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_YOUTUBE_ONLY_RE = re.compile(
    r"^https?://(?:www\.)?(?:youtube\.com/watch\?v=[\w-]+|youtu\.be/[\w-]+)\S*$",
    re.IGNORECASE,
)

settings = load_settings()
model_registry = load_model_registry(settings)
model_clients = ModelClientFactory(settings, model_registry)
llm = model_clients.get("local_default")
vision = VisionClient(model_clients.get_or_default("vision"))
qdrant = QdrantClient(settings)
transcriber = TranscriptionClient(settings)
tts = TtsClient(settings)
embeddings = EmbeddingClient(settings)
english_bot_clip_client = AudioClipClient(settings)
dictionary = LocalDictionary(settings.dictionary_path)
alerter = Alerter(settings, log=lambda message: log(message))
conversation_memory = ConversationMemory(settings, llm)
skill_router = SkillRouter(settings, llm, model_clients)
task_history = TaskHistory(settings.task_history_path)
runtime_agents = [SkillAgent(skill) for skill in skill_router.skills]
try:
    for required_policy in ("local_router", "local_coder", "local_reasoner"):
        model_registry.resolve(required_policy)
except LookupError:
    pass
else:
    runtime_agents.append(EngineeringReviewAgent(model_clients, task_history))
runtime_agents.append(ChatAgent(llm, conversation_memory))
agent_registry = AgentRegistry(runtime_agents)
task_dispatcher = TaskDispatcher(agent_registry, task_history)

RUNNING = True
ACTIVE_LOCK = threading.Lock()
ACTIVE_REQUESTS = 0
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

# chat_id -> {"items": [{"path": str, "kind": "document"|"image", "note": str}], "updated_at": int}
# A file uploaded without a recognised category caption parks here until the
# user's next plain-text message names the category (the two-step flow).
PENDING_CATEGORY_LOCK = threading.Lock()
PENDING_CATEGORY: dict[int, dict] = {}

# chat_id -> arbitrary caller-defined dict describing what's being waited on.
# Generic version of the PENDING_CATEGORY pattern above, for any "this chat
# is waiting to answer something, the next message (text or voice) is the
# answer" flow -- e.g. the English-learning bot's Saturday cloze quiz. Kept
# separate from PENDING_CATEGORY so wiring a new answer-flow can never
# regress the existing category two-step upload behaviour.
PENDING_ANSWER_LOCK = threading.Lock()
PENDING_ANSWER: dict[int, dict] = {}

# Below this, a voice reply to a pending English-bot task is treated as a
# non-attempt (accidental tap, misfire) rather than a real practice attempt
# -- see handle_english_bot_pending_reply.
MIN_ENGLISH_BOT_VOICE_REPLY_SECONDS = 10.0

# Ends a day's practice session (see handle_english_bot_pending_reply):
# every qualifying voice/text reply after a day's task is pushed is
# evaluated AND persisted (evaluate_<day>_reply already overwrites the same
# tracked record on each call, so the last attempt before /Done is what
# ends up recorded -- no separate "final answer" bookkeeping needed). Must
# match the whole message, not a substring, so ordinary practice text
# mentioning "done" never accidentally closes the session.
ENGLISH_BOT_DONE_COMMAND = "/done"


def set_pending_answer(chat_id: int, item: dict) -> None:
    with PENDING_ANSWER_LOCK:
        PENDING_ANSWER[chat_id] = item
    save_pending_state()


def pop_pending_answer(chat_id: int) -> dict | None:
    with PENDING_ANSWER_LOCK:
        item = PENDING_ANSWER.pop(chat_id, None)
    if item is not None:
        save_pending_state()
    return item


def peek_pending_answer(chat_id: int) -> dict | None:
    with PENDING_ANSWER_LOCK:
        return PENDING_ANSWER.get(chat_id)


def has_pending_answer(chat_id: int) -> bool:
    with PENDING_ANSWER_LOCK:
        return chat_id in PENDING_ANSWER


# Open English-bot tasks and /vocab reviews live in memory; these two keep a
# copy on disk (settings.pending_state_path) so restarting the container --
# e.g. `openclawctl restart` to load new code -- doesn't drop today's task.
PENDING_SAVE_LOCK = threading.Lock()


def save_pending_state() -> None:
    path = settings.pending_state_path
    if path is None:
        return
    with PENDING_ANSWER_LOCK:
        english = {str(chat_id): item for chat_id, item in PENDING_ANSWER.items()}
    with VOCAB_REVIEW_LOCK:
        reviews = {str(chat_id): item for chat_id, item in VOCAB_REVIEW_PENDING.items()}
    with NIGHT_LOCK:
        night = {str(chat_id): item for chat_id, item in NIGHT_PENDING.items()}
    with PENDING_CATEGORY_LOCK:
        uploads = {str(chat_id): item for chat_id, item in PENDING_CATEGORY.items()}
    data = {"english_task": english, "vocab_review": reviews, "night": night, "category": uploads}
    try:
        with PENDING_SAVE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 - saving is best-effort, never block a reply
        log(f"[pending] could not save open tasks to {path}: {exc}")


def restore_pending_state() -> None:
    """Load what save_pending_state wrote, dropping /vocab reviews that
    expired while the gateway was down."""
    path = settings.pending_state_path
    if path is None or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"[pending] could not read {path}: {exc}")
        return
    now = time.time()
    with PENDING_ANSWER_LOCK:
        PENDING_ANSWER.update({int(k): v for k, v in (data.get("english_task") or {}).items()})
    with VOCAB_REVIEW_LOCK:
        VOCAB_REVIEW_PENDING.update(
            {
                int(k): v
                for k, v in (data.get("vocab_review") or {}).items()
                if float(v.get("expires_at") or 0) > now
            }
        )
    with NIGHT_LOCK:
        NIGHT_PENDING.update({int(k): v for k, v in (data.get("night") or {}).items()})
    # an upload waiting for its category: keep only files still in staging
    with PENDING_CATEGORY_LOCK:
        for key, pending in (data.get("category") or {}).items():
            items = [item for item in pending.get("items", []) if Path(item.get("path", "")).exists()]
            if items:
                PENDING_CATEGORY[int(key)] = {**pending, "items": items}
    log(
        f"[pending] restored english_task={len(PENDING_ANSWER)} vocab_review={len(VOCAB_REVIEW_PENDING)} "
        f"night={len(NIGHT_PENDING)} category={len(PENDING_CATEGORY)}"
    )

# (chat_id, media_group_id) -> {"paths": [Path], "caption": str, "timer": Timer}
# Telegram delivers a multi-photo album as separate messages that share one
# media_group_id, but attaches the caption to only ONE of them. Without this
# buffer, every photo would be routed by its OWN (mostly empty) caption --
# fragmenting one #trip album across a real "trip" category and a garbage
# category made from whatever text arrived next. Each arrival (re)starts a
# short debounce timer; once no sibling shows up for
# media_group_flush_seconds, the whole group is routed together using
# whichever caption the group actually carried.
MEDIA_GROUP_LOCK = threading.Lock()
MEDIA_GROUP_BUFFER: dict[tuple[int, str], dict] = {}

HELP_TEXT = f"""OpenClaw Arm Continuum quick reference

Common commands
/menu
Buttons for the common commands.

/mem <content>
Save a piece of personal memory or working context. Add due:YYYY-MM-DD
and/or tag:<word> anywhere in the text to attach them.
Example: /mem OpenClaw preference: use /mem for memory writes.
Example: /mem renew passport due:2026-12-01 tag:admin

/mem list [done|archived] [tag:<word>]
List active memory items (or completed/archived ones), soonest due date
first. Add tag:<word> to scope the list to that exact tag.

/mem done <id>
Mark a memory item done. /mem rm <id> deletes it. Both take the short
ID shown by /mem list or after a /mem save.

/mem snooze <id> <3d|1w|YYYY-MM-DD>
Push an item's due date out (relative or absolute). Also works on items
with no due date yet, and reactivates a done item.

/mem edit <id> <new text>
Replace an item's text. due:/tag: in the new text override the stored
ones; omitting them keeps what was already there.

/mem digest [tag:<word>]
Reminder summary: overdue items, items due soon, and stale undated
items. Add tag:<word> to scope it to one topic. Add a daily cron job
to get this pushed automatically:
Example: /cron add daily 08:00 Memory digest :: /mem digest
A day with nothing due or stale stays silent -- it is not pushed.

/mem upcoming [days]
Daily schedule report: items due today through the next 3 days (or
[days]), grouped by day. Always reports, even when nothing is scheduled.
Example: /cron add daily 07:00 Schedule :: /mem upcoming

/mem archive-stale
Fully-automatic sweep: archives active, undated items that have gone
stale (the same test /mem digest uses), out of the default list. No
unarchive -- see /mem list archived. Meant for a weekly cron job:
Example: /cron add weekly mon 03:00 Archive stale memory :: /mem archive-stale

/rag <question>
Query local memory and the document knowledge base.
Example: /rag Which modules is OpenClaw connected to?
Add #<category> to search one category, or #all for every category.
Example: /rag #work-notes What are the open action items?
Add source:<text> to use only material whose source (file name, title
or link, as shown on the Sources: line) contains that text; works with
#<category> and #all too.
Example: /rag source:q1-plan #work-notes What is due this month?
/rag digest summarizes, in one sentence each, what was added to the
knowledge base and categories yesterday (not /mem notes).
Example: /cron add daily 07:05 New knowledge :: /rag digest
Add tag:<word> to scope to /mem items with that tag (tracker only).
Example: /rag tag:work what did I save about the deadline?
Add since:YYYY-MM-DD and/or before:YYYY-MM-DD to scope by save date
(tracker + knowledge; since is inclusive, before is exclusive).
Example: /rag since:2026-09-01 what have I saved this month?

/search <keywords>
Browse and scrape the web with the local Playwright worker, convert to
Markdown, then hand it to the local reasoning model for a summary.
Example: /search Arm Neoverse latest news
Example: /search What tech news happened today
Paste a link (anywhere in the message, with or without a question) to
open that page directly instead of searching for it.
Example: /search https://example.com/article what does this say?
Note: this reads a page's visible text, not a video's spoken content --
a video link yields its title/description, not a transcript.

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

/new  (or /reset)
Start a new chat conversation. Plain chat remembers the last few
turns per chat, plus a rolling summary of anything older than that so
context is compressed, not lost. /new clears all of it. Commands like
/rag and /search are always independent.

/keep <fact>
Pin a fact so it always stays in context for this conversation, even
after it would otherwise roll into the summary or drop off. Cleared by
/new. For anything that should survive /new, use /mem instead.
Example: /keep My flight home is on the 23rd, not the 21st.

/history
Preview what this chat currently remembers: pinned facts, the rolling
summary, and the recent raw turns. Read-only -- does not change anything.

/review <sanitized engineering request>
Run the bounded code and architecture review workflow. This command is
available when the multi-model catalog is configured.

/doc url <Google Doc URL>
Import a public Google Doc, save it as Markdown, and index it into
knowledge RAG automatically.
Example: /doc url https://docs.google.com/document/d/.../edit
Example: /doc url https://docs.google.com/document/d/.../edit tracker

Natural language
You can ask general questions or about the weather directly.
Example: What's the weather like in Taiwan tomorrow?
Plain chat keeps the last few turns as context, so you can follow up
without repeating yourself. Send /new to start over.

Document RAG
Upload .pdf / .md / .txt / .log / .json / .csv / .tsv directly to
Telegram, or drop them into /workspace/inbox/knowledge. OpenClaw saves
them to the knowledge inbox and indexes them into RAG automatically.

To ask about a specific file, put the filename in /rag.
Example: /rag debugger_armv8v9.pdf What is this document about?
Example: /rag debugger_armv8v9.pdf Summarize the key points

Caption a document with /mem or /tracker to save it to dynamic
tracker memory instead.

Category RAG
Keep different kinds of material in separate, non-overlapping
knowledge bases (one Qdrant collection per category). Upload a photo
or document, then name a category one of two ways:
- caption the upload #<name> (e.g. #work-notes or #[Work Notes]), or
- send the file first, then tap a category (or type a new name).
Query one category:  /rag #<name> <question>
Query every category: /rag #all <question>
/cat list shows your categories and their sizes.
/cat rename <old> <new> and /cat merge <source> <target> fix a
mistyped or duplicated category without losing any indexed content.
/cancel drops a file that is waiting for a category name.

Every /rag answer ends with a "Sources:" line naming the documents
it used.

Photos and voice
Upload a photo directly and OpenClaw will save it to the
{settings.runtime_label} inbox and hand it to the vision model for
analysis (configure a dedicated VLM via models.json or
OPENCLAW_VLM_*; see docs/VISION_SETUP.md).

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


def send_audio_file(chat_id: int, audio_path: Path, caption: str = "") -> None:
    """The reverse of download_telegram_file -- OpenClaw's first outbound
    file transfer. Uses Telegram's sendAudio, which needs a real multipart
    upload (request_json only speaks JSON bodies)."""
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendAudio"
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
    post_multipart_file(url, fields, "audio", audio_path, timeout=settings.request_timeout)


def send_voice_file(chat_id: int, voice_path: Path, caption: str = "") -> None:
    """sendVoice: an OGG/Opus file shown as a voice message (waveform, speed
    control) rather than a music-player attachment."""
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendVoice"
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
    post_multipart_file(url, fields, "voice", voice_path, timeout=settings.request_timeout)


def send_document_file(chat_id: int, document_path: Path, caption: str = "") -> None:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendDocument"
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
    post_multipart_file(url, fields, "document", document_path, timeout=settings.request_timeout)


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def document_directory(caption: str) -> Path:
    lowered = caption.strip().lower()
    if lowered.startswith("/tracker") or lowered.startswith("/mem"):
        return settings.inbox_path / "tracker" / "telegram"
    return settings.inbox_path / "knowledge" / "telegram"


# --- category RAG -------------------------------------------------------------


def category_staging_dir() -> Path:
    return settings.inbox_path / ".staging" / "telegram"


def category_dir(slug: str) -> Path:
    return settings.inbox_path / settings.category_inbox_dirname / slug


def is_tracker_caption(caption: str) -> bool:
    lowered = caption.strip().lower()
    return lowered.startswith("/tracker") or lowered.startswith("/mem")


def _write_meta_sidecar(doc_path: Path, data: dict) -> None:
    sidecar = doc_path.with_name(doc_path.name + ".meta.json")
    clean = {key: value for key, value in data.items() if value}
    sidecar.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def write_upload_meta(stored_path: Path, original_name: str, **extra: str) -> None:
    """Record the uploader-supplied filename next to a saved upload so RAG can cite it."""
    _write_meta_sidecar(stored_path, {"original_file_name": original_name, "origin": "telegram", **extra})


def ingest_document_into_category(
    source_path: Path, entry: dict, note: str = "", original_name: str = ""
) -> Path:
    """Place an already-downloaded document into its category inbox folder."""
    directory = category_dir(entry["slug"])
    target = unique_path(directory, source_path.name)
    shutil.move(str(source_path), str(target))
    _write_meta_sidecar(
        target,
        {
            "category": entry["display"],
            "category_slug": entry["slug"],
            "origin": "telegram",
            "caption_note": note,
            "original_file_name": original_name or source_path.name,
        },
    )
    return target


def ingest_text_into_category(
    text: str, entry: dict, note: str = "", source_label: str = "telegram reply", url: str = ""
) -> Path:
    """File a plain-text item with no source file -- e.g. a replied-to
    Telegram message -- directly into its category inbox folder as
    markdown, same shape as an image's description document.

    ``url`` (e.g. a video link) gets its own line in the document AND
    becomes the stored ``original_file_name`` when present, so /rag's
    "Sources:" citation shows the actual link instead of the generic
    "telegram reply" label -- otherwise there is no way to trace an
    answer back to the video it came from.
    """
    directory = category_dir(entry["slug"])
    target_name = sanitize_filename(f"{timestamp()}-{source_label}.md", f"{timestamp()}-note.md")
    target = unique_path(directory, target_name)
    target.write_text(
        "\n".join(
            [
                f"# {source_label}",
                "",
                f"Category: {entry['display']}",
                f"Source: {source_label}",
                f"URL: {url}" if url else "",
                f"Indexed: {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}",
                f"Note: {note}" if note else "",
                "",
                "## Content",
                "",
                text,
                "",
            ]
        ),
        encoding="utf-8",
    )
    _write_meta_sidecar(
        target,
        {
            "category": entry["display"],
            "category_slug": entry["slug"],
            "origin": "telegram_reply",
            "caption_note": note,
            "source_url": url,
            "original_file_name": url or source_label,
        },
    )
    return target


def ingest_image_into_category(
    chat_id: int, image_path: Path, entry: dict, note: str = "", original_name: str = ""
) -> Path:
    """Describe an image with the vision model and index that text under a category."""
    directory = category_dir(entry["slug"])
    stored_image = unique_path(directory / "media", image_path.name)
    shutil.copy2(str(image_path), str(stored_image))

    instruction = DEFAULT_DESCRIBE_INSTRUCTION
    if note:
        instruction = f"{instruction}\n\nThe uploader added this note, use it as context: {note}"
    description = vision.describe_image(
        stored_image, instruction, max_tokens=settings.category_image_max_tokens
    )

    doc = unique_path(directory, f"{stored_image.stem}.md")
    doc.write_text(
        "\n".join(
            [
                f"# {image_path.name}",
                "",
                f"Category: {entry['display']}",
                "Source: telegram image",
                f"Image: {stored_image}",
                f"Indexed: {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}",
                f"Note: {note}" if note else "",
                "",
                "## Description",
                "",
                description,
                "",
            ]
        ),
        encoding="utf-8",
    )
    _write_meta_sidecar(
        doc,
        {
            "category": entry["display"],
            "category_slug": entry["slug"],
            "origin": "telegram",
            "image_path": str(stored_image),
            "caption_note": note,
            "original_file_name": original_name or image_path.name,
        },
    )
    return doc


def set_pending_category(chat_id: int, item: dict) -> int:
    with PENDING_CATEGORY_LOCK:
        pending = PENDING_CATEGORY.setdefault(chat_id, {"items": [], "updated_at": 0})
        pending["items"].append(item)
        pending["updated_at"] = int(time.time())
        count = len(pending["items"])
    save_pending_state()
    return count


def pop_pending_category(chat_id: int) -> dict | None:
    with PENDING_CATEGORY_LOCK:
        pending = PENDING_CATEGORY.pop(chat_id, None)
    if pending is not None:
        save_pending_state()
    return pending


def has_pending_category(chat_id: int) -> bool:
    with PENDING_CATEGORY_LOCK:
        return chat_id in PENDING_CATEGORY


CATEGORY_BUTTON_PREFIX = "cat:"
NEW_CATEGORY_TITLE = "檔案｜新分類"


def category_picker_markup(suggested: str | None = None, duplicate: bool = False) -> dict:
    """Inline keyboard for a file waiting for its category: one button per
    existing category (two per row), then New category and Cancel. A slug
    too long for Telegram's 64-byte callback_data is left out -- typing its
    name still works. A suggested category (display name) comes first with
    a star; a duplicate upload gets a Skip button."""
    entries = [
        entry for entry in registry_entries(settings)
        if len((CATEGORY_BUTTON_PREFIX + entry["slug"]).encode("utf-8")) <= 64
    ]
    rows = []
    if suggested:
        match = next((e for e in entries if e["display"] == suggested), None)
        if match:
            entries.remove(match)
            rows.append([{"text": f"⭐ #{match['display']} (suggested)",
                          "callback_data": CATEGORY_BUTTON_PREFIX + match["slug"]}])
    buttons = [{"text": f"#{entry['display']}", "callback_data": CATEGORY_BUTTON_PREFIX + entry["slug"]}
               for entry in entries]
    rows += [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if duplicate:
        rows.append([{"text": "Skip — keep the saved copy", "callback_data": "cat_dupskip"}])
    rows.append(
        [{"text": "+ New category", "callback_data": "cat_new"}, {"text": "Cancel", "callback_data": "cat_cancel"}]
    )
    return {"inline_keyboard": rows}


def duplicate_places() -> dict[str, Path]:
    places = {f"#{entry['display']}": category_dir(entry["slug"]) for entry in registry_entries(settings)}
    places["the knowledge base"] = settings.inbox_path / "knowledge"
    return places


def _suggest_for_picker(chat_id: int, message_id: int, staged: Path, name: str, duplicate: bool) -> None:
    """Background: ask the model which category fits, then move it to the
    top of the picker. Skipped when the picker has already been answered."""
    categories = {
        entry["display"]: [p.name for p in _category_documents(category_dir(entry["slug"]))][:5]
        for entry in registry_entries(settings)
    }
    choice = suggest_category(llm, name, preview_text(staged), categories)
    if not choice:
        return
    with PENDING_CATEGORY_LOCK:
        pending = PENDING_CATEGORY.get(chat_id)
        still_open = pending is not None and message_id in pending.get("prompt_ids", [])
    if not still_open:
        return
    try:
        telegram(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id,
             "reply_markup": category_picker_markup(suggested=choice, duplicate=duplicate)},
            timeout=20,
        )
        log(f"[category] suggested chat_id={chat_id} category={choice}")
    except Exception as exc:  # noqa: BLE001
        log(f"[category] could not show suggestion: {exc}")


def send_category_picker(chat_id: int, html: str, *, staged: Path | None = None, name: str = "",
                         duplicate: bool = False) -> None:
    """Ask for a category with buttons. The message id is kept on the
    pending batch so its buttons can be taken away once the batch is
    filed, cancelled or expired. For a document (staged), a suggested
    category is added a moment later."""
    try:
        result = telegram(
            "sendMessage",
            {"chat_id": chat_id, "text": html, "parse_mode": "HTML",
             "reply_markup": category_picker_markup(duplicate=duplicate)},
        )
    except Exception as exc:  # noqa: BLE001 - the typed reply still works without buttons
        log(f"[category] picker send failed chat_id={chat_id}: {exc}")
        send_message(chat_id, html_to_plain(html))
        return
    message_id = (result.get("result") or {}).get("message_id")
    if message_id:
        with PENDING_CATEGORY_LOCK:
            pending = PENDING_CATEGORY.get(chat_id)
            if pending is not None:
                pending.setdefault("prompt_ids", []).append(int(message_id))
        save_pending_state()
        if staged is not None:
            threading.Thread(
                target=_suggest_for_picker, args=(chat_id, int(message_id), staged, name, duplicate), daemon=True
            ).start()


def close_category_pickers(chat_id: int, pending: dict) -> None:
    """Remove the buttons from a batch's category prompts."""
    for message_id in pending.get("prompt_ids", []):
        try:
            telegram(
                "editMessageReplyMarkup",
                {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001 - e.g. already edited, or deleted by the user
            log(f"[category] could not close picker {message_id}: {exc}")


def pending_item_names(pending: dict) -> list[str]:
    return [item.get("original_name") or Path(item["path"]).name for item in pending.get("items", [])]


def _edit_card(chat_id: int, message_id: int, html: str) -> None:
    try:
        telegram(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": html, "parse_mode": "HTML"},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 - the button's toast already told the user
        log(f"[category] could not edit picker {message_id}: {exc}")


def handle_callback_query(query: dict) -> None:
    """A tap on an inline button. Today only the category picker uses them."""
    message = query.get("message") or {}
    chat_id = int((message.get("chat") or {}).get("id") or 0)
    message_id = message.get("message_id")
    data = str(query.get("data") or "")

    def answer(text: str = "") -> None:
        try:
            telegram("answerCallbackQuery", {"callback_query_id": query.get("id"), **({"text": text} if text else {})})
        except Exception as exc:  # noqa: BLE001
            log(f"[telegram] answerCallbackQuery failed: {exc}")

    if not chat_id or (settings.telegram_allowed_chat_ids and chat_id not in settings.telegram_allowed_chat_ids):
        log(f"[telegram] rejected callback chat_id={chat_id}")
        answer()
        return
    if data.startswith(NIGHT_CALLBACK_PREFIX):
        handle_night_callback(chat_id, message_id, data, answer)
        return
    if data.startswith(NIGHT_SCHEDULE_PREFIX):
        handle_night_schedule_callback(chat_id, message_id, data, answer)
        return
    if data.startswith("vq:") and settings.dictionary_enabled:
        handle_quiz_callback(chat_id, message_id, data, answer)
        return
    if data.startswith("say:") and settings.dictionary_enabled:
        handle_say_callback(chat_id, data, answer)
        return
    if data.startswith("rag:"):
        handle_rag_callback(chat_id, message_id, data, answer)
        return
    if data.startswith(TTS_PREFIX):
        handle_tts_callback(chat_id, data, answer)
        return
    if data.startswith(MENU_PREFIX):
        handle_menu_callback(chat_id, data, answer)
        return
    if data.startswith("gist:") and settings.english_bot_enabled:
        handle_gist_callback(chat_id, message_id, data, answer)
        return
    if data.startswith("vw:") and settings.dictionary_enabled:
        handle_word_list_callback(chat_id, message_id, data, answer)
        return
    if data.startswith("vr:") and settings.dictionary_enabled:
        handle_vocab_review_callback(chat_id, message_id, data, answer)
        return
    if not settings.category_rag_enabled or not data.startswith("cat"):
        answer()
        return
    if data.startswith("catdel"):
        handle_category_delete_callback(chat_id, message_id, data, answer)
        return
    with PENDING_CATEGORY_LOCK:
        waiting = dict(PENDING_CATEGORY.get(chat_id) or {})
    if not waiting.get("items"):
        answer("Nothing is waiting for a category.")
        if message_id:
            close_category_pickers(chat_id, {"prompt_ids": [message_id]})
        return
    names = ", ".join(esc(name) for name in pending_item_names(waiting))

    if data == "cat_new":
        # a toast alone is easy to miss; ask in the chat and open the reply box
        answer()
        try:
            telegram(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": f"<b>{NEW_CATEGORY_TITLE}</b>\n{RULE}\n{names}\n\nType the new category name.",
                    "parse_mode": "HTML",
                    "reply_markup": {"force_reply": True, "input_field_placeholder": "Category name"},
                },
            )
        except Exception as exc:  # noqa: BLE001
            log(f"[category] new-category prompt failed chat_id={chat_id}: {exc}")
        return

    if data == "cat_dupskip":
        dropped = pop_pending_category(chat_id)
        if dropped:
            close_category_pickers(chat_id, dropped)
            for item in dropped.get("items", []):
                Path(item["path"]).unlink(missing_ok=True)
        answer("Skipped")
        if message_id:
            _edit_card(chat_id, message_id, f"<b>檔案｜已略過</b>\n{RULE}\n{names}\n\n"
                       "Already saved -- this copy wasn't stored again.")
        return

    if data == "cat_cancel":
        dropped = pop_pending_category(chat_id)
        if dropped:
            close_category_pickers(chat_id, dropped)
            sweep_pending_items_to_default(dropped)
        answer("Cancelled")
        if message_id:
            _edit_card(chat_id, message_id, f"<b>檔案｜已取消</b>\n{RULE}\n{names}\n\n"
                       "Any waiting document goes to the general knowledge base.")
        return

    slug = data[len(CATEGORY_BUTTON_PREFIX):]
    entry = next((e for e in registry_entries(settings) if e["slug"] == slug), None)
    if entry is None:
        answer("That category no longer exists -- type a name instead.")
        return
    resolve_pending_with_category(chat_id, entry["display"])
    answer(f"Filing into #{entry['display']}")
    if message_id:
        _edit_card(chat_id, message_id, f"<b>檔案｜已分類</b>\n{RULE}\n{names} → #{esc(entry['display'])}")


def sweep_pending_items_to_default(pending: dict) -> None:
    """Send a dropped/expired batch's staged documents to the general knowledge inbox."""
    for item in pending.get("items", []):
        if item.get("kind") != "document":
            continue
        staged = Path(item["path"])
        if not staged.exists():
            continue
        fallback = unique_path(settings.inbox_path / "knowledge" / "telegram", staged.name)
        try:
            shutil.move(str(staged), str(fallback))
            write_upload_meta(fallback, item.get("original_name") or staged.name)
            log(f"[category] staged doc -> knowledge {fallback.name}")
        except OSError as exc:
            log(f"[category] fallback move failed {staged}: {exc}")


def sweep_expired_pending() -> None:
    """Move staged docs from timed-out two-step uploads into the default knowledge inbox."""
    cutoff = int(time.time()) - settings.category_pending_ttl_seconds
    expired: list[tuple[int, dict]] = []
    with PENDING_CATEGORY_LOCK:
        for chat_id, pending in list(PENDING_CATEGORY.items()):
            if pending.get("updated_at", 0) < cutoff:
                expired.append((chat_id, PENDING_CATEGORY.pop(chat_id)))
    if expired:
        save_pending_state()
    for chat_id, pending in expired:
        close_category_pickers(chat_id, pending)
        had_doc = any(item.get("kind") == "document" for item in pending.get("items", []))
        sweep_pending_items_to_default(pending)
        if had_doc:
            send_message(
                chat_id,
                "No category was given in time, so the waiting document was filed "
                "into the general knowledge base.",
            )
        log(f"[category] pending expired chat_id={chat_id}")


def resolve_pending_with_category(chat_id: int, category_text: str) -> bool:
    """Consume a chat's pending uploads into the category named by category_text."""
    pending = pop_pending_category(chat_id)
    if not pending or not pending.get("items"):
        return False

    # A user replying to "reply with a category name" very naturally reuses
    # the #category caption syntax out of habit. Without this, the literal
    # "#trip some note" text becomes the category name itself -- silently
    # creating a second, unrelated category instead of filing into the one
    # the caption path would have used. Be lenient: if the reply starts with
    # # / ＃, parse it the same way a caption is parsed and use just the
    # category part.
    stripped = category_text.strip()
    if stripped[:1] in ("#", "＃"):
        parsed_name, _note = parse_category_caption(stripped)
        if parsed_name:
            category_text = parsed_name

    try:
        display = validate_category_name(settings, category_text)
    except ValueError as exc:
        # keep the pending items so the user can retry with a valid name
        with PENDING_CATEGORY_LOCK:
            PENDING_CATEGORY[chat_id] = pending
        save_pending_state()
        send_message(chat_id, f"That category name will not work: {exc}. Send another name, or /cancel.")
        return True

    entry = upsert_registry_entry(settings, display)
    close_category_pickers(chat_id, pending)
    worker = threading.Thread(
        target=_run_category_ingest,
        args=(chat_id, pending["items"], entry),
        daemon=True,
    )
    worker.start()
    return True


def handle_reply_category_message(chat_id: int, message: dict) -> bool:
    """Reply to any text message with #<category> to file that message's
    text into a category -- e.g. an external script that posts a video
    summary by calling sendMessage with the bot's own token. Telegram never
    delivers a bot's own outgoing messages back to it as an update, so
    there is no way to treat that push itself as input; replying is the
    lightweight way to pull a specific message in on demand instead."""
    if not settings.category_rag_enabled:
        return False
    reply_to = message.get("reply_to_message") or {}
    replied_text = (reply_to.get("text") or reply_to.get("caption") or "").strip()
    if not replied_text:
        return False
    if replied_text.startswith(NEW_CATEGORY_TITLE) and has_pending_category(chat_id):
        return False  # the answer to "New category": it names the waiting file's category
    own_text = (message.get("text") or "").strip()
    category_name, note = parse_category_caption(own_text)
    if not category_name:
        return False
    try:
        display = validate_category_name(settings, category_name)
    except ValueError as exc:
        send_message(chat_id, f"That category name will not work: {exc}")
        return True
    # A URL in the reply's own note (e.g. "#video-notes https://...") takes
    # priority and is stripped out of the note to avoid duplicating it;
    # otherwise fall back to one embedded in the replied-to text itself
    # (e.g. a video summary that lists its own source link), left in place.
    note_url_match = _URL_RE.search(note)
    if note_url_match:
        url = note_url_match.group(0)
        note = (note[: note_url_match.start()] + note[note_url_match.end() :]).strip()
    else:
        replied_url_match = _URL_RE.search(replied_text)
        url = replied_url_match.group(0) if replied_url_match else ""
    entry = upsert_registry_entry(settings, display)
    doc = ingest_text_into_category(replied_text, entry, note=note, url=url)
    send_message(
        chat_id,
        f'Filed the replied-to message into category "{entry["display"]}": {doc.name}\n'
        f'Give the indexer ~10 seconds, then query with /rag #{entry["display"]}.',
    )
    log(f"[category] reply ingest chat_id={chat_id} slug={entry['slug']}")
    return True


def _relay_video_summary(chat_id: int, video_url: str) -> None:
    """Background-thread call to the configured Apps Script relay: ask it to
    run Gemini's video summary and push the result back into this same chat
    via its own sendMessage. Runs off the polling loop since Gemini's video
    analysis can take a while -- failures are reported back explicitly so the
    user is never left waiting silently."""
    payload = {"video_url": video_url}
    if settings.video_summary_relay_secret:
        payload["secret"] = settings.video_summary_relay_secret
    started = time.time()
    try:
        result = request_json(
            "POST",
            settings.video_summary_relay_url,
            payload,
            timeout=settings.video_summary_relay_timeout_seconds,
        )
    except Exception as exc:
        log(f"[video] relay unreachable chat_id={chat_id} after={time.time() - started:.0f}s: {exc}")
        send_message(chat_id, f"Could not reach the video summary relay: {exc}")
        return
    if not result.get("ok"):
        error = result.get("error", "unknown error")
        log(f"[video] relay failed chat_id={chat_id} after={time.time() - started:.0f}s: {error}")
        send_message(chat_id, f"Video summary relay failed: {error}")
        return
    log(f"[video] relay ok chat_id={chat_id} after={time.time() - started:.0f}s")


def handle_video_link_message(chat_id: int, text: str) -> bool:
    """A message that is nothing but a YouTube link gets relayed to the
    configured Apps Script endpoint (see telegram-video-relay-spec.md), which
    runs Gemini's video summary and pushes the result back into this chat.
    Reply to that pushed summary with #<category> to file it via the
    existing handle_reply_category_message flow -- this function only
    triggers the summary, it does not itself touch RAG."""
    if not settings.video_summary_relay_url:
        return False
    if not _YOUTUBE_ONLY_RE.match(text):
        return False
    send_message(
        chat_id,
        "Got it -- sending this to Gemini for a summary. I'll push the result back here once it's "
        "ready; reply to that message with #<category> to file it into RAG.",
    )
    worker = threading.Thread(target=_relay_video_summary, args=(chat_id, text), daemon=True)
    worker.start()
    log(f"[video] relay dispatched chat_id={chat_id}")
    return True


def _process_english_bot_reply(chat_id: int, pending: dict, transcribed_reply: str, reply_duration_seconds: float) -> None:
    try:
        feedback = dispatch_pending_reply(
            pending=pending,
            transcribed_reply=transcribed_reply,
            reply_duration_seconds=reply_duration_seconds,
            llm=llm,
            qdrant=qdrant,
            embeddings=embeddings,
            collection=settings.tracker_collection,
            owner=str(chat_id),
        )
    except Exception as exc:
        log(f"[english_bot] reply dispatch error chat_id={chat_id} kind={pending.get('kind')}: {exc}")
        send_message(chat_id, f"OpenClaw could not evaluate that reply: {exc}")
        return
    send_html(chat_id, feedback)
    log(f"[english_bot] reply evaluated chat_id={chat_id} kind={pending.get('kind')}")


def handle_english_bot_pending_reply(chat_id: int, message: dict) -> bool:
    """If this chat has a pending English-bot task awaiting an answer
    (PENDING_ANSWER, set by the daily scheduler loop -- see
    _english_bot_scheduler_loop near main()), treat this message -- voice
    or text, transcribed if voice -- as a practice attempt at it. Runs
    before the generic document/photo/voice handling in handle_message() so
    a voice reply doesn't get swallowed into the normal voice-chat flow.

    A day's task is repeatable, not single-shot: every qualifying attempt
    (voice >= MIN_ENGLISH_BOT_VOICE_REPLY_SECONDS, or any text that isn't
    /Done) is evaluated AND persisted via the same evaluate_<day>_reply
    used before -- it already overwrites the same tracked record on each
    call, so the LAST attempt naturally ends up as what's recorded, with no
    separate bookkeeping needed. The task stays pending (peeked, not
    popped) until the user sends /Done, which closes it out without being
    evaluated itself."""
    if not settings.english_bot_enabled:
        return False
    if not has_pending_answer(chat_id):
        return False

    voice = message.get("voice") or message.get("audio")
    text = (message.get("text") or message.get("caption") or "").strip()
    if not voice and not text:
        return False  # e.g. a stray photo/document -- leave the pending answer intact

    if text.lower() == ENGLISH_BOT_DONE_COMMAND:
        pending = pop_pending_answer(chat_id)
        if pending is None:
            return False
        send_message(chat_id, "Got it -- today's task is closed out. See you next time!")
        log(f"[english_bot] practice session closed chat_id={chat_id} kind={pending.get('kind')}")
        return True

    if text.startswith("/"):
        # A command (e.g. /w to look up a word mid-task) is never a practice
        # attempt -- let the normal command handling take it, and leave the
        # task open.
        return False

    pending = peek_pending_answer(chat_id)
    if pending is None:
        return False

    if voice:
        file_id = voice.get("file_id")
        if not file_id:
            send_message(chat_id, "This voice reply has no Telegram file_id, so OpenClaw cannot download it.")
            return True
        duration = float(voice.get("duration") or 0.0)
        if duration < MIN_ENGLISH_BOT_VOICE_REPLY_SECONDS:
            # Too short to be a real practice attempt (accidental tap,
            # misfire, etc.) -- nothing was popped, so the task is already
            # still open; just tell the user.
            send_message(
                chat_id,
                f"That voice reply was only {duration:.0f}s -- needs to be at least "
                f"{MIN_ENGLISH_BOT_VOICE_REPLY_SECONDS:.0f}s to count as a practice attempt. "
                "Today's task is still open, try again (or send /Done to finish).",
            )
            return True
        file_info = telegram_file_info(file_id)
        suffix = extension_from_file_path(file_info.get("file_path", ""), voice.get("mime_type"), ".ogg")
        target_name = sanitize_filename(
            f"{timestamp()}-english-bot-reply{suffix}", f"{timestamp()}-english-bot-reply.ogg"
        )
        target_path = unique_path(settings.inbox_path / "audio" / "telegram", target_name)
        downloaded_path, _ = download_telegram_file(file_id, target_path)

        def _transcribe_and_evaluate() -> None:
            try:
                transcript = transcriber.transcribe(downloaded_path)
            except Exception as exc:
                log(f"[english_bot] transcription error chat_id={chat_id}: {exc}")
                send_message(
                    chat_id,
                    f"Voice reply saved, but Whisper transcription failed: {exc} -- "
                    "today's task is still open, try again.",
                )
                return
            if not transcript:
                send_message(
                    chat_id,
                    "Whisper did not produce any text from this voice reply -- "
                    "today's task is still open, try again.",
                )
                return
            _process_english_bot_reply(chat_id, pending, transcript, duration)

        worker = threading.Thread(target=_transcribe_and_evaluate, daemon=True)
        worker.start()
        log(f"[english_bot] voice practice attempt queued chat_id={chat_id} kind={pending.get('kind')}")
        return True

    worker = threading.Thread(
        target=_process_english_bot_reply, args=(chat_id, pending, text, 0.0), daemon=True
    )
    worker.start()
    log(f"[english_bot] text practice attempt queued chat_id={chat_id} kind={pending.get('kind')}")
    return True


def _ingest_caption_category(
    chat_id: int, source_path: Path, kind: str, category_name: str, note: str, original_name: str = ""
) -> None:
    try:
        display = validate_category_name(settings, category_name)
    except ValueError as exc:
        send_message(chat_id, f"That category name will not work: {exc}")
        return
    entry = upsert_registry_entry(settings, display)
    if kind == "document":
        duplicate = find_duplicate(source_path, {f"#{entry['display']}": category_dir(entry["slug"])})
        if duplicate:
            source_path.unlink(missing_ok=True)
            send_message(chat_id, f"This file is already in \"{entry['display']}\" ({duplicate[1]}) -- not saved again.")
            return
    send_message(chat_id, f"Filing this into category \"{entry['display']}\".")
    worker = threading.Thread(
        target=_run_category_ingest,
        args=(chat_id, [{"path": str(source_path), "kind": kind, "note": note, "original_name": original_name}], entry),
        daemon=True,
    )
    worker.start()


def _route_image_to_category(
    chat_id: int, image_path: Path, category_name: str | None, note: str, original_name: str = ""
) -> None:
    if not settings.category_rag_enabled:
        return
    if category_name:
        _ingest_caption_category(chat_id, image_path, "image", category_name, note, original_name)
        return
    set_pending_category(
        chat_id,
        {"path": str(image_path), "kind": "image", "note": note, "original_name": original_name},
    )
    send_category_picker(
        chat_id,
        f"<b>圖片｜選擇分類</b>\n{RULE}\nTo also index this image for retrieval, tap a category "
        "or type a new name (/cancel skips it). The analysis above is sent regardless.",
    )


def _ingest_caption_category_batch(
    chat_id: int, paths: list[Path], kind: str, category_name: str, note: str
) -> None:
    """Like _ingest_caption_category, but for every item in one media group
    at once -- one validation, one registry upsert, one confirmation
    message, one ingest worker, instead of one of each per photo."""
    try:
        display = validate_category_name(settings, category_name)
    except ValueError as exc:
        send_message(chat_id, f"That category name will not work: {exc}")
        return
    entry = upsert_registry_entry(settings, display)
    plural = "s" if len(paths) != 1 else ""
    send_message(chat_id, f'Filing {len(paths)} item{plural} into category "{entry["display"]}".')
    items = [{"path": str(path), "kind": kind, "note": note, "original_name": ""} for path in paths]
    worker = threading.Thread(target=_run_category_ingest, args=(chat_id, items, entry), daemon=True)
    worker.start()


def _buffer_media_group_photo(chat_id: int, media_group_id: str, image_path: Path, caption: str) -> None:
    """Park one photo from a Telegram album until no sibling has arrived for
    media_group_flush_seconds, then route the whole group together (see the
    MEDIA_GROUP_BUFFER comment for why: only one message in the album
    carries the caption)."""
    key = (chat_id, media_group_id)
    with MEDIA_GROUP_LOCK:
        group = MEDIA_GROUP_BUFFER.setdefault(key, {"paths": [], "caption": ""})
        group["paths"].append(image_path)
        if caption:
            group["caption"] = caption
        existing_timer = group.get("timer")
        if existing_timer is not None:
            existing_timer.cancel()
        timer = threading.Timer(settings.media_group_flush_seconds, _flush_media_group, args=(chat_id, media_group_id))
        timer.daemon = True
        group["timer"] = timer
        timer.start()


def _flush_media_group(chat_id: int, media_group_id: str) -> None:
    key = (chat_id, media_group_id)
    with MEDIA_GROUP_LOCK:
        group = MEDIA_GROUP_BUFFER.pop(key, None)
    if not group or not group["paths"]:
        return
    paths: list[Path] = group["paths"]
    caption = group.get("caption", "")
    category_name, category_note = parse_category_caption(caption) if settings.category_rag_enabled else (None, "")
    if not settings.category_rag_enabled:
        return
    if category_name:
        _ingest_caption_category_batch(chat_id, paths, "image", category_name, category_note)
        return
    for path in paths:
        set_pending_category(chat_id, {"path": str(path), "kind": "image", "note": "", "original_name": ""})
    send_category_picker(
        chat_id,
        f"<b>圖片｜選擇分類</b>\n{RULE}\nTo also index "
        f"{'this image' if len(paths) == 1 else f'these {len(paths)} images'} for retrieval, tap a "
        "category or type a new name (/cancel skips it). The analysis above is sent regardless.",
    )
    log(f"[category] media group pending chat_id={chat_id} group={media_group_id} count={len(paths)}")


def _run_category_ingest(chat_id: int, items: list[dict], entry: dict) -> None:
    done: list[str] = []
    failed: list[str] = []
    for item in items:
        path = Path(item["path"])
        note = item.get("note", "")
        original_name = item.get("original_name", "")
        try:
            if item.get("kind") == "image":
                doc = ingest_image_into_category(chat_id, path, entry, note, original_name)
            else:
                doc = ingest_document_into_category(path, entry, note, original_name)
            done.append(doc.name)
        except Exception as exc:  # noqa: BLE001 - report back to the user
            log(f"[category] ingest failed chat_id={chat_id} path={path}: {exc}")
            failed.append(f"{path.name} ({exc})")

    lines = [f"Category \"{entry['display']}\" (collection: {entry['collection']})"]
    if done:
        lines.append("Queued for indexing: " + ", ".join(done))
        lines.append(f"In a few seconds: /rag #{entry['display']} <your question>")
    if failed:
        lines.append("Failed: " + "; ".join(failed))
    send_message(chat_id, "\n".join(lines))
    log(f"[category] ingest chat_id={chat_id} slug={entry['slug']} ok={len(done)} fail={len(failed)}")


def format_history_preview(snapshot: dict) -> str:
    """Render a ConversationMemory.preview() snapshot for /history: pinned
    facts, the rolling summary, then the raw recent-turn window."""
    sections: list[str] = []
    pinned = snapshot.get("pinned") or []
    if pinned:
        sections.append("Pinned facts:\n" + "\n".join(f"- {fact}" for fact in pinned))
    summary = snapshot.get("summary") or ""
    if summary:
        sections.append(f"Summary of earlier conversation:\n{summary}")
    turns = snapshot.get("turns") or []
    if turns:
        lines = []
        for turn in turns:
            role = "You" if turn.get("role") == "user" else "Bot"
            lines.append(f"{role}: {turn.get('content', '')}")
        sections.append("Recent turns:\n" + "\n".join(lines))
    return "\n\n".join(sections)


def category_command_text() -> str:
    return (
        "OpenClaw category RAG\n\n"
        "Upload a photo or document, then either:\n"
        "- put the category in the caption as #<name> (e.g. #工作筆記), or\n"
        "- send the file first, then tap a category (or type a new name).\n\n"
        "Or reply to ANY text message with #<name> to file that message's "
        "text into a category -- no file upload needed.\n\n"
        "/cat list   Show categories and their document counts\n"
        "/cat rename <old> <new>   Rename a category (its files/index are untouched)\n"
        "/cat merge <source> <target>   Move everything from source into target, "
        "then delete source\n"
        "/cat delete <name>   Delete a category, its files and its index (asks first)\n"
        "/cancel     Drop a file that is waiting for a category\n\n"
        "Query one category:  /rag #<name> <question>\n"
        "Query every category: /rag #all <question>"
    )


def handle_category_command(chat_id: int, text: str) -> bool:
    if text != "/cat" and not text.startswith("/cat "):
        return False
    remainder = text[len("/cat") :].strip()
    action, _, rest = remainder.partition(" ")
    action = action.lower() or "help"
    rest = rest.strip()

    if action in {"help", "?"}:
        send_message(chat_id, category_command_text())
        return True

    if action == "list":
        entries = registry_entries(settings)
        if not entries:
            send_message(chat_id, "No categories yet. Upload a file with a #<name> caption to create one.")
            return True
        lines = ["Categories:"]
        for item in entries:
            count = qdrant.points_count(item["collection"])
            suffix = f" - {count} chunks" if count is not None else ""
            lines.append(f"- {item['display']}{suffix}  (collection: {item['collection']})")
        send_message(chat_id, "\n".join(lines))
        return True

    if action == "rename":
        names = parse_two_category_names(rest)
        if not names:
            send_message(
                chat_id,
                "Usage: /cat rename <old name> <new name> -- the new name is one word, no spaces "
                "(an old multi-word name goes in [brackets], e.g. /cat rename [Bus trip] bustrip)",
            )
            return True
        old_token, new_name = names
        entry = resolve_category(settings, old_token)
        if not entry or not entry.get("known"):
            send_message(chat_id, f'No category named "{old_token}".')
            return True
        try:
            new_display = validate_category_name(settings, new_name)
        except ValueError as exc:
            send_message(chat_id, f"That category name will not work: {exc}")
            return True
        collision = resolve_category(settings, new_display)
        if collision and collision.get("known") and collision["slug"] != entry["slug"]:
            send_message(
                chat_id,
                f'"{new_display}" is already a different category (collection: {collision["collection"]}). '
                f'Use /cat merge {old_token} {new_name} if you want to combine them instead.',
            )
            return True
        updated = rename_registry_entry(settings, entry["slug"], new_display)
        send_message(chat_id, f'Renamed "{entry["display"]}" to "{updated["display"]}".')
        return True

    if action == "merge":
        names = parse_two_category_names(rest)
        if not names:
            send_message(
                chat_id,
                "Usage: /cat merge <source> <target> -- everything in <source> "
                "moves into <target>, then <source> is deleted.",
            )
            return True
        from_token, into_token = names
        from_entry = resolve_category(settings, from_token)
        if not from_entry or not from_entry.get("known"):
            send_message(chat_id, f'No category named "{from_token}".')
            return True
        into_entry = resolve_category(settings, into_token)
        if not into_entry or not into_entry.get("known"):
            send_message(chat_id, f'No category named "{into_token}".')
            return True
        if from_entry["slug"] == into_entry["slug"]:
            send_message(chat_id, "Source and target are the same category.")
            return True
        send_message(chat_id, f'Merging "{from_entry["display"]}" into "{into_entry["display"]}"...')
        worker = threading.Thread(
            target=_run_category_merge, args=(chat_id, from_entry, into_entry), daemon=True
        )
        worker.start()
        return True

    if action in {"delete", "rm"}:
        token = rest.strip()
        if token[:1] in ("[", "［", "{", "｛") and token[-1:] in ("]", "］", "}", "｝"):
            token = token[1:-1].strip()
        if token[:1] in ("#", "＃"):
            token = token[1:].strip()
        if not token:
            send_message(chat_id, "Usage: /cat delete <name> (see /cat list)")
            return True
        entry = resolve_category(settings, token)
        if not entry or not entry.get("known"):
            send_message(chat_id, f'No category named "{token}".')
            return True
        send_category_delete_confirm(chat_id, entry)
        return True

    send_message(chat_id, category_command_text())
    return True


CATEGORY_DELETE_PREFIX = "catdel:"


def _category_file_count(slug: str) -> int:
    return len(_category_documents(category_dir(slug)))


def send_category_delete_confirm(chat_id: int, entry: dict) -> None:
    files = _category_file_count(entry["slug"])
    chunks = qdrant.points_count(entry["collection"])
    size = f"{files} file{'s' if files != 1 else ''}" + (f", {chunks} chunks" if chunks is not None else "")
    html = (
        f"<b>分類｜刪除確認</b>\n{RULE}\n#{esc(entry['display'])} · {size}\n\n"
        "Deletes its files and its search index. This can't be undone."
    )
    markup = {
        "inline_keyboard": [
            [
                {"text": f"Delete #{entry['display']}", "callback_data": CATEGORY_DELETE_PREFIX + entry["slug"]},
                {"text": "Keep it", "callback_data": "catdel_no"},
            ]
        ]
    }
    if len((CATEGORY_DELETE_PREFIX + entry["slug"]).encode("utf-8")) > 64:
        send_message(chat_id, f'"{entry["display"]}" can\'t be deleted from Telegram (its id is too long).')
        return
    telegram("sendMessage", {"chat_id": chat_id, "text": html, "parse_mode": "HTML", "reply_markup": markup})


def delete_category(entry: dict) -> int:
    """Remove a category completely: its inbox folder (documents, sidecars,
    media), its Qdrant collection and its registry entry. Returns the number
    of documents removed."""
    files = _category_file_count(entry["slug"])
    shutil.rmtree(category_dir(entry["slug"]), ignore_errors=True)
    qdrant.delete_collection(entry["collection"])
    remove_registry_entry(settings, entry["slug"])
    log(f"[category] deleted slug={entry['slug']} files={files}")
    return files


def handle_category_delete_callback(chat_id: int, message_id: int | None, data: str, answer) -> None:
    if data == "catdel_no":
        answer("Kept")
        if message_id:
            close_category_pickers(chat_id, {"prompt_ids": [message_id]})
        return
    slug = data[len(CATEGORY_DELETE_PREFIX):]
    entry = next((e for e in registry_entries(settings) if e["slug"] == slug), None)
    if entry is None:
        answer("That category is already gone.")
        if message_id:
            close_category_pickers(chat_id, {"prompt_ids": [message_id]})
        return
    try:
        files = delete_category(entry)
    except Exception as exc:  # noqa: BLE001
        log(f"[category] delete failed slug={slug}: {exc}")
        answer("Delete failed -- see the log.")
        return
    answer(f"Deleted #{entry['display']}")
    if message_id:
        _edit_card(
            chat_id,
            message_id,
            f"<b>分類｜已刪除</b>\n{RULE}\n#{esc(entry['display'])} · {files} file{'s' if files != 1 else ''} removed",
        )


def _category_documents(folder: Path) -> list[Path]:
    """A category folder's documents: every top-level file (PDF, Markdown,
    text...) except the .meta.json sidecars. Photos live in media/ and are
    reached through their Markdown description."""
    if not folder.is_dir():
        return []
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and not path.name.endswith(META_SIDECAR_SUFFIX) and not path.name.endswith(".tmp")
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _merge_category_files(from_slug: str, into_entry: dict) -> tuple[int, int]:
    """Move every document from one category's inbox directory into
    another's, updating each .meta.json sidecar to point at the new
    category. A document whose bytes are already in the target is dropped
    instead of copied, so a merge never leaves the same file indexed twice.
    The memory watcher re-indexes moved files at its next poll -- merge does
    not touch Qdrant data directly, so there is only ever one ingest path to
    reason about. Returns (moved, skipped duplicates)."""
    from_dir = category_dir(from_slug)
    if not from_dir.is_dir():
        return 0, 0
    into_dir = category_dir(into_entry["slug"])
    into_media = into_dir / "media"
    existing = {_file_sha256(path) for path in _category_documents(into_dir)}
    moved = skipped = 0
    for doc_path in _category_documents(from_dir):
        meta_path = doc_path.with_name(doc_path.name + META_SIDECAR_SUFFIX)
        digest = _file_sha256(doc_path)
        if digest in existing:
            skipped += 1
            continue  # removed with from_dir below
        existing.add(digest)
        into_dir.mkdir(parents=True, exist_ok=True)
        target_doc = unique_path(into_dir, doc_path.name)
        shutil.move(str(doc_path), str(target_doc))

        data: dict = {}
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            meta_path.unlink(missing_ok=True)
        image_path = data.get("image_path")
        if image_path and Path(image_path).exists():
            target_image = unique_path(into_media, Path(image_path).name)
            shutil.move(image_path, str(target_image))
            data["image_path"] = str(target_image)
        data["category"] = into_entry["display"]
        data["category_slug"] = into_entry["slug"]
        _write_meta_sidecar(target_doc, data)
        moved += 1
    shutil.rmtree(from_dir, ignore_errors=True)
    return moved, skipped


def _run_category_merge(chat_id: int, from_entry: dict, into_entry: dict) -> None:
    try:
        moved_files, duplicates = _merge_category_files(from_entry["slug"], into_entry)
        qdrant.delete_collection(from_entry["collection"])
        remove_registry_entry(settings, from_entry["slug"])
    except Exception as exc:
        log(
            f"[category] merge failed chat_id={chat_id} from={from_entry['slug']} "
            f"into={into_entry['slug']}: {exc}"
        )
        send_message(chat_id, f"Merge failed partway through: {exc}")
        return
    log(
        f"[category] merge chat_id={chat_id} from={from_entry['slug']} "
        f"into={into_entry['slug']} files={moved_files} duplicates={duplicates}"
    )
    skipped_note = (
        f" {duplicates} duplicate file(s) already in \"{into_entry['display']}\" were dropped." if duplicates else ""
    )
    if moved_files:
        send_message(
            chat_id,
            f'Merged "{from_entry["display"]}" into "{into_entry["display"]}": {moved_files} file(s).'
            f"{skipped_note} Give the indexer ~10 seconds to catch up, then query with "
            f'/rag #{into_entry["display"]}.',
        )
    else:
        send_message(
            chat_id,
            f'"{from_entry["display"]}" had no new files to move.{skipped_note} '
            f'"{into_entry["display"]}" is unchanged; "{from_entry["display"]}" no longer exists.',
        )


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

    category_name, category_note = parse_category_caption(caption) if settings.category_rag_enabled else (None, "")

    if mime_type.startswith("image/"):
        target_directory = settings.inbox_path / "media" / "telegram"
    elif category_name:
        # Land a category-captioned document in staging so the memory watcher
        # never briefly indexes it into the default knowledge collection.
        target_directory = category_staging_dir()
    else:
        target_directory = document_directory(caption)
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
            args=(chat_id, downloaded_path, category_note if category_name else caption),
            daemon=True,
        )
        worker.start()
        log(f"[telegram] saved image-document chat_id={chat_id} path={downloaded_path} bytes={byte_count}")
        _route_image_to_category(chat_id, downloaded_path, category_name, category_note, original_name)
        return True

    if category_name:
        _ingest_caption_category(chat_id, downloaded_path, "document", category_name, category_note, original_name)
        return True

    if downloaded_path.suffix.lower() in SUPPORTED_SUFFIXES:
        if settings.category_rag_enabled and not is_tracker_caption(caption):
            staged = unique_path(category_staging_dir(), downloaded_path.name)
            shutil.move(str(downloaded_path), str(staged))
            set_pending_category(
                chat_id, {"path": str(staged), "kind": "document", "note": "", "original_name": original_name}
            )
            duplicate = find_duplicate(staged, duplicate_places())
            dup_note = (
                f"\n<i>Already saved in {esc(duplicate[0])} ({esc(duplicate[1])}).</i>" if duplicate else ""
            )
            send_category_picker(
                chat_id,
                f"<b>檔案｜選擇分類</b>\n{RULE}\n{esc(original_name or staged.name)}{dup_note}\n\n"
                "Tap a category, or type a new name.\n"
                "/cancel files it into the general knowledge base.",
                staged=staged,
                name=original_name or staged.name,
                duplicate=duplicate is not None,
            )
            log(f"[category] pending document chat_id={chat_id} staged={staged.name}")
            return True
        collection = (
            settings.tracker_collection
            if target_directory.relative_to(settings.inbox_path).parts[0] == "tracker"
            else settings.knowledge_collection
        )
        write_upload_meta(downloaded_path, original_name)
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
    category_name, category_note = parse_category_caption(caption) if settings.category_rag_enabled else (None, "")
    analysis_prompt = category_note if category_name else caption
    worker = threading.Thread(
        target=process_image_message,
        args=(chat_id, downloaded_path, analysis_prompt),
        daemon=True,
    )
    worker.start()
    log(f"[telegram] saved photo chat_id={chat_id} path={downloaded_path} bytes={byte_count}")
    media_group_id = message.get("media_group_id")
    if media_group_id and settings.category_rag_enabled:
        # Telegram attaches a multi-photo album's caption to only one
        # message in the group -- buffer and route the whole group
        # together instead of using this message's own (likely empty)
        # caption. See the MEDIA_GROUP_BUFFER comment.
        _buffer_media_group_photo(chat_id, media_group_id, downloaded_path, caption)
    else:
        _route_image_to_category(chat_id, downloaded_path, category_name, category_note)
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
        answer = vision.describe_image(image_path, prompt, max_tokens=settings.vision_max_tokens)
    except VisionError as exc:
        log(f"[vision] error chat_id={chat_id} path={image_path} endpoint={vision.endpoint_id}: {exc}")
        send_message(
            chat_id,
            "Image saved, but the vision model failed to process the image input.\n"
            f"Reason: {exc}\n\n"
            "Configure a dedicated VLM (models.json role \"vision\" or OPENCLAW_VLM_*) and "
            "verify it with scripts/vision_smoke.py. See docs/VISION_SETUP.md.",
        )
        return

    send_message(chat_id, answer or "The vision model did not return any image analysis content.")
    log(f"[vision] done chat_id={chat_id} path={image_path} endpoint={vision.endpoint_id} answer_chars={len(answer or '')}")


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
        if str(exc) == VLLM_NOT_READY_MESSAGE or not llm.is_reachable():
            send_message(chat_id, "Voice message transcribed and saved.\n\n" + MODEL_PAUSED_MESSAGE)
        else:
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


def send_html(chat_id: int, html: str) -> None:
    """Send a message built with openclaw_runtime.message_cards (Telegram
    HTML parse mode). Long messages are split at blank lines outside any
    tag; a part Telegram still rejects (bad markup, or a single block over
    the limit) is resent as plain text, so a formatting problem never loses
    the content itself."""
    for part in split_html_message(html, settings.max_reply_chars):
        if len(part) <= settings.max_reply_chars:
            try:
                telegram("sendMessage", {"chat_id": chat_id, "text": part, "parse_mode": "HTML"})
                continue
            except urllib.error.HTTPError as exc:
                log(f"[telegram] HTML message rejected ({exc.code}), resending as plain text chat_id={chat_id}")
        send_message(chat_id, html_to_plain(part))


DICTIONARY_HELP_TEXT = """
Word lookup
/w <word>
Look a word up in the offline dictionary; it's added to your word list.
You can also just send the word (1-4 English words) without /w.
Add the sentence you saw it in after a | for the meaning in context.
Example: /w resilient | She's remarkably resilient.

/vocab
Show your word list, newest first. /vocab rm <word> removes a word.

/vocab review
Review the saved words that are due today (spaced repetition): up to 5
questions, answered in one typed message.

/vocab quiz
A quick button quiz on the words you looked up this week (also offered on
Sundays).

/say <word>
Pronunciation practice: record yourself saying it; speech recognition
checks what it heard (no word = one from your list).

/vocab export
Get your word list as an Anki deck (.apkg) with UK / US audio when
pronunciation is on; /vocab export tsv for a plain Anki import file."""


def help_text() -> str:
    return (
        HELP_TEXT
        + (DICTIONARY_HELP_TEXT if settings.dictionary_enabled else "")
        + (NIGHT_HELP_TEXT if settings.night_ritual_enabled else "")
    )


# /vocab review sessions waiting for a typed answer, kept apart from
# PENDING_ANSWER so a review never replaces an open daily English task.
# chat_id -> {"questions": [...], "expires_at": epoch seconds}
VOCAB_REVIEW_PENDING: dict[int, dict] = {}
VOCAB_REVIEW_LOCK = threading.Lock()
# An unanswered review stops capturing typed replies after this long, so it
# can't swallow tomorrow's answer to a daily task.
VOCAB_REVIEW_TTL_SECONDS = 60 * 60


def _vocab_today():
    return datetime.now(ZoneInfo(settings.english_bot_timezone)).date()


def _start_vocab_review(chat_id: int) -> None:
    questions = start_review(qdrant, settings.tracker_collection, str(chat_id), _vocab_today())
    if not questions:
        send_message(chat_id, "No saved words are due for review today. Look words up with /w to add more.")
        return
    with VOCAB_REVIEW_LOCK:
        VOCAB_REVIEW_PENDING[chat_id] = {
            "questions": questions,
            # wall-clock, not monotonic, so it still means something after a restart
            "expires_at": time.time() + VOCAB_REVIEW_TTL_SECONDS,
        }
    save_pending_state()
    send_card_with_buttons(
        chat_id, render_review_quiz(questions), [[{"text": "Self-check with buttons", "callback_data": "vr:self"}]]
    )


def _grade_vocab_review(chat_id: int, questions: list[dict], answer: str) -> None:
    try:
        feedback = grade_review(llm, qdrant, settings.tracker_collection, questions, answer, _vocab_today())
    except Exception as exc:
        log(f"[vocab] review grading error chat_id={chat_id}: {exc}")
        send_message(chat_id, f"OpenClaw could not grade that review: {exc} -- send /vocab review to try again.")
        return
    send_html(chat_id, feedback)
    log(f"[vocab] review graded chat_id={chat_id} words={len(questions)}")


def handle_vocab_review_answer(chat_id: int, message: dict) -> bool:
    """A typed, non-command reply answers an open /vocab review. Voice
    replies and commands are left alone (voice still answers the daily
    task), and an expired review is dropped instead of answered."""
    if not settings.dictionary_enabled:
        return False
    text = (message.get("text") or "").strip()
    if not text or text.startswith("/") or message.get("voice") or message.get("audio"):
        return False
    with VOCAB_REVIEW_LOCK:
        pending = VOCAB_REVIEW_PENDING.get(chat_id)
        if pending is not None and pending.get("mode") == "self":
            return False  # a button self-check is under way; typing doesn't answer it
        pending = VOCAB_REVIEW_PENDING.pop(chat_id, None)
    if pending is None:
        return False
    save_pending_state()
    if time.time() > pending["expires_at"]:
        return False
    threading.Thread(target=_grade_vocab_review, args=(chat_id, pending["questions"], text), daemon=True).start()
    return True


# 🔊 buttons carry a short key; the text to speak stays here (Telegram's
# callback_data is only 64 bytes), mirrored to a small file so buttons on
# older cards still work after a restart.
TTS_TEXTS: "OrderedDict[str, str]" = OrderedDict()
TTS_TEXTS_LOCK = threading.Lock()
TTS_TEXTS_MAX = 2000
TTS_PREFIX = "tts:"


def _tts_texts_path() -> Path:
    return settings.inbox_path.parent / ".openclaw" / "tts_texts.json"


def _load_tts_texts() -> None:
    """Fill TTS_TEXTS from disk (once, when a key isn't in memory)."""
    try:
        saved = json.loads(_tts_texts_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    with TTS_TEXTS_LOCK:
        for key, text in saved.items():
            TTS_TEXTS.setdefault(key, text)


def _tts_key(text: str) -> str:
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    with TTS_TEXTS_LOCK:
        known = key in TTS_TEXTS
        TTS_TEXTS[key] = text
        TTS_TEXTS.move_to_end(key)
        while len(TTS_TEXTS) > TTS_TEXTS_MAX:
            TTS_TEXTS.popitem(last=False)
        snapshot = dict(TTS_TEXTS) if not known else None
    if snapshot is not None:
        try:
            path = _tts_texts_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log(f"[tts] could not save button texts: {exc}")
    return key


def pronunciation_rows(word: str, sentence: str = "") -> list[list[dict]]:
    """🔊 UK / 🔊 US for the word, and for the sentence when there is one."""
    if not settings.tts_enabled or not word.strip():
        return []
    rows = []
    for label, text in (("", word.strip()), (" sentence", (sentence or "").strip())):
        if not text:
            continue
        key = _tts_key(text)
        rows.append([
            {"text": f"🔊 UK{label}", "callback_data": f"{TTS_PREFIX}{key}:uk"},
            {"text": f"🔊 US{label}", "callback_data": f"{TTS_PREFIX}{key}:us"},
        ])
    return rows


def _speak_and_send(chat_id: int, text: str, accent: str) -> None:
    try:
        audio = tts.speak(text, accent)
    except Exception as exc:  # noqa: BLE001
        log(f"[tts] speak failed accent={accent}: {exc}")
        send_message(chat_id, "Couldn't make the pronunciation audio right now -- try again in a moment.")
        return
    folder = settings.inbox_path.parent / ".openclaw" / "tts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{hashlib.sha1((accent + text).encode('utf-8')).hexdigest()[:16]}.ogg"
    try:
        path.write_bytes(audio)
        caption = f"{accent.upper()} · {text if len(text) <= 200 else text[:200] + '…'}"
        send_voice_file(chat_id, path, caption)
    finally:
        path.unlink(missing_ok=True)


def handle_tts_callback(chat_id: int, data: str, answer) -> None:
    key, _, accent = data[len(TTS_PREFIX):].partition(":")
    with TTS_TEXTS_LOCK:
        text = TTS_TEXTS.get(key)
    if text is None:
        _load_tts_texts()
        with TTS_TEXTS_LOCK:
            text = TTS_TEXTS.get(key)
    if not text or accent not in ("uk", "us"):
        answer("This button has expired -- look the word up again.")
        return
    answer()
    threading.Thread(target=_speak_and_send, args=(chat_id, text, accent), daemon=True).start()


def send_card_with_buttons(chat_id: int, html: str, rows: list[list[dict]]) -> int | None:
    """An HTML card with inline buttons; returns its message id. Falls back
    to the plain card (no buttons) if Telegram rejects it."""
    try:
        result = telegram(
            "sendMessage",
            {"chat_id": chat_id, "text": html, "parse_mode": "HTML", "reply_markup": {"inline_keyboard": rows}},
        )
        return (result.get("result") or {}).get("message_id")
    except Exception as exc:  # noqa: BLE001
        log(f"[telegram] card with buttons failed chat_id={chat_id}: {exc}")
        send_html(chat_id, html)
        return None


def _edit_card_buttons(chat_id: int, message_id: int, html: str, rows: list[list[dict]]) -> None:
    try:
        telegram(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": html, "parse_mode": "HTML",
             "reply_markup": {"inline_keyboard": rows}},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"[telegram] could not edit card {message_id}: {exc}")


def _send_self_check_question(chat_id: int, session: dict) -> None:
    index = session["index"]
    questions = session["questions"]
    send_card_with_buttons(
        chat_id,
        render_self_check_question(index + 1, len(questions), questions[index]),
        [[{"text": "Show answer", "callback_data": f"vr:show:{index}"}]],
    )


def handle_vocab_review_callback(chat_id: int, message_id: int | None, data: str, answer) -> None:
    """Button self-check for /vocab review: Self-check -> per word Show
    answer -> Remembered / Forgot, which moves the word to its next box."""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
    with VOCAB_REVIEW_LOCK:
        session = VOCAB_REVIEW_PENDING.get(chat_id)
        if session is None or time.time() > session.get("expires_at", 0):
            session = None
        elif action == "self" and session.get("mode") != "self":
            session.update({"mode": "self", "index": 0, "outcomes": []})
        elif action != "self" and (session.get("mode") != "self" or index != session.get("index")):
            session = None
        session = dict(session) if session else None
    if session is None:
        answer("This review has ended -- send /vocab review for a new one.")
        if message_id:
            close_category_pickers(chat_id, {"prompt_ids": [message_id]})
        return
    questions = session["questions"]
    if action == "self":
        save_pending_state()
        answer()
        if message_id:
            close_category_pickers(chat_id, {"prompt_ids": [message_id]})
        _send_self_check_question(chat_id, session)
        return
    question = questions[index]
    if action == "show":
        answer()
        if message_id:
            _edit_card_buttons(
                chat_id,
                message_id,
                render_self_check_question(index + 1, len(questions), question, reveal=True),
                [[{"text": "✅ Remembered", "callback_data": f"vr:ok:{index}"},
                  {"text": "❌ Forgot", "callback_data": f"vr:no:{index}"}]]
                + pronunciation_rows(question["phrase"]),
            )
        return
    if action not in ("ok", "no"):
        answer()
        return
    correct = action == "ok"
    today = _vocab_today()
    next_date = record_review(qdrant, settings.tracker_collection, question, correct, today)
    answer("Remembered" if correct else "It'll come back sooner")
    if message_id:
        _edit_card(
            chat_id,
            message_id,
            render_self_check_question(index + 1, len(questions), question, result=(correct, next_date, today)),
        )
    outcomes = session.get("outcomes", []) + [(question["phrase"], correct)]
    with VOCAB_REVIEW_LOCK:
        live = VOCAB_REVIEW_PENDING.get(chat_id)
        finished = index + 1 >= len(questions)
        if live is not None:
            if finished:
                VOCAB_REVIEW_PENDING.pop(chat_id, None)
            else:
                live.update({"index": index + 1, "outcomes": outcomes})
                session = dict(live)
    save_pending_state()
    if finished:
        send_html(chat_id, render_self_check_summary(outcomes))
        log(f"[vocab] self-check done chat_id={chat_id} words={len(outcomes)}")
    else:
        _send_self_check_question(chat_id, session)


def handle_bare_word_lookup(chat_id: int, message: dict) -> bool:
    """Look a word up when it's sent on its own, without /w -- see
    vocabulary.parse_bare_lookup for what counts. Skipped while a file is
    waiting for its category name, since that reply is plain text too."""
    if not settings.dictionary_enabled:
        return False
    text = (message.get("text") or "").strip()
    if not text or text.startswith("/"):
        return False
    parsed = parse_bare_lookup(text)
    if parsed is None:
        return False
    if settings.category_rag_enabled and has_pending_category(chat_id):
        return False
    word, sentence = parsed
    threading.Thread(target=_lookup_and_reply, args=(chat_id, word, sentence), daemon=True).start()
    log(f"[vocab] bare-word lookup chat_id={chat_id}")
    return True


# /say sessions: chat_id -> {"word": str, "expires_at": float}. The next voice
# message is the attempt (short-lived, so it never swallows a daily-task reply
# much later).
SAY_PENDING: dict[int, dict] = {}
SAY_LOCK = threading.Lock()
SAY_TTL_SECONDS = 10 * 60


def start_say(chat_id: int, word: str) -> None:
    word = " ".join(word.split())
    if not word:
        entries = list_word_list(qdrant, settings.tracker_collection, str(chat_id))[:20]
        if not entries:
            send_message(chat_id, "Usage: /say <word> -- or look words up first and /say picks one of them.")
            return
        import random

        word = (random.choice(entries).get("display_word") or "").strip()
    with SAY_LOCK:
        SAY_PENDING[chat_id] = {"word": word, "expires_at": time.time() + SAY_TTL_SECONDS}
    rows = pronunciation_rows(word)
    if rows:
        send_card_with_buttons(chat_id, render_say_prompt(word), rows)
    else:
        send_html(chat_id, render_say_prompt(word))


def _check_say(chat_id: int, word: str, audio_path: Path) -> None:
    try:
        heard = transcriber.transcribe(audio_path)
    except Exception as exc:  # noqa: BLE001
        log(f"[vocab] /say transcription failed: {exc}")
        send_message(chat_id, "Couldn't check that recording -- try /say again.")
        return
    finally:
        audio_path.unlink(missing_ok=True)
    ok = pronunciation_matches(word, heard)
    key = _tts_key(word)
    rows = [[{"text": "Try again", "callback_data": f"say:{key}"}]] + pronunciation_rows(word)
    send_card_with_buttons(chat_id, render_say_result(word, heard, ok), rows)
    log(f"[vocab] /say chat_id={chat_id} ok={ok}")


def handle_say_reply(chat_id: int, message: dict) -> bool:
    """While a /say is open, the next voice message is the attempt."""
    voice = message.get("voice") or message.get("audio")
    if not voice:
        return False
    with SAY_LOCK:
        pending = SAY_PENDING.get(chat_id)
        if pending is None:
            return False
        SAY_PENDING.pop(chat_id, None)
    if time.time() > pending["expires_at"]:
        return False
    file_id = voice.get("file_id")
    if not file_id:
        return False
    file_info = telegram_file_info(file_id)
    suffix = extension_from_file_path(file_info.get("file_path", ""), voice.get("mime_type"), ".ogg")
    target = unique_path(settings.inbox_path / "audio" / "telegram", f"{timestamp()}-say{suffix}")
    path, _ = download_telegram_file(file_id, target)
    threading.Thread(target=_check_say, args=(chat_id, pending["word"], path), daemon=True).start()
    return True


def handle_say_callback(chat_id: int, data: str, answer) -> None:
    key = data[len("say:"):]
    with TTS_TEXTS_LOCK:
        word = TTS_TEXTS.get(key)
    if word is None:
        _load_tts_texts()
        with TTS_TEXTS_LOCK:
            word = TTS_TEXTS.get(key)
    if not word:
        answer("This button has expired -- send /say <word>.")
        return
    answer()
    start_say(chat_id, word)


# Button quizzes on recent words: chat_id -> {"questions", "index", "results"}.
QUIZ_PENDING: dict[int, dict] = {}
QUIZ_LOCK = threading.Lock()


def start_quiz(chat_id: int) -> bool:
    entries = list_word_list(qdrant, settings.tracker_collection, str(chat_id))
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    questions = build_quiz(entries, since_iso=since, seed=int(time.time()))
    if not questions:
        send_message(chat_id, "A quiz needs at least two saved words -- look some up with /w first.")
        return False
    with QUIZ_LOCK:
        QUIZ_PENDING[chat_id] = {"questions": questions, "index": 0, "results": []}
    _send_quiz_question(chat_id)
    return True


def _send_quiz_question(chat_id: int) -> None:
    with QUIZ_LOCK:
        session = QUIZ_PENDING.get(chat_id)
        if not session:
            return
        index, questions = session["index"], session["questions"]
    question = questions[index]
    buttons = [{"text": option, "callback_data": f"vq:{index}:{n}"} for n, option in enumerate(question["options"])]
    send_card_with_buttons(chat_id, render_quiz_question(index + 1, len(questions), question),
                           [buttons[i : i + 2] for i in range(0, len(buttons), 2)])


def handle_quiz_callback(chat_id: int, message_id: int | None, data: str, answer) -> None:
    if data == "vq:start":
        answer()
        if message_id:
            close_category_pickers(chat_id, {"prompt_ids": [message_id]})
        start_quiz(chat_id)
        return
    try:
        _, index_text, choice_text = data.split(":")
        index, choice = int(index_text), int(choice_text)
    except ValueError:
        answer()
        return
    with QUIZ_LOCK:
        session = QUIZ_PENDING.get(chat_id)
        if not session or session["index"] != index:
            session = None
        else:
            question = session["questions"][index]
            right = choice == question["answer"]
            session["results"].append((question["word"], right))
            session["index"] += 1
            finished = session["index"] >= len(session["questions"])
            results = list(session["results"])
            if finished:
                QUIZ_PENDING.pop(chat_id, None)
    if session is None:
        answer("This quiz has ended -- send /vocab quiz for a new one.")
        return
    answer("Right!" if right else f"It's {question['word']}")
    if message_id:
        _edit_card(chat_id, message_id, render_quiz_question(index + 1, len(session["questions"]), question, choice))
    if finished:
        send_html(chat_id, render_quiz_summary(results))
    else:
        _send_quiz_question(chat_id)


def offer_weekly_quiz(owners: list[str]) -> None:
    """Sunday: invite each learner to a quiz on the words they looked up."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    for owner in owners:
        try:
            entries = list_word_list(qdrant, settings.tracker_collection, owner)
        except Exception as exc:  # noqa: BLE001
            log(f"[vocab] quiz offer skipped owner={owner}: {exc}")
            continue
        recent = [e for e in entries if e.get("source") != "weekly_chunk" and (e.get("added_at") or "") >= since]
        if len(recent) < 2:
            continue
        send_card_with_buttons(
            int(owner),
            f"<b>【本週小測驗】</b>\n{RULE}\nYou looked up {len(recent)} words this week -- "
            "a quick quiz on them? Tap the answers, no typing.",
            [[{"text": "Start quiz", "callback_data": "vq:start"}]],
        )


ANKI_MAX_WORDS = 500


def build_anki_notes(entries: list[dict], speak=None) -> list[AnkiNote]:
    """Word-list entries as Anki notes; speak(text, accent) -> MP3 bytes adds
    UK / US audio (a word whose audio fails is kept without it)."""
    notes = []
    for entry in entries[:ANKI_MAX_WORDS]:
        word = entry.get("display_word") or entry.get("word") or ""
        if not word:
            continue
        audio = {}
        for accent in ("uk", "us"):
            if speak is None:
                continue
            try:
                audio[accent] = speak(word, accent)
            except Exception as exc:  # noqa: BLE001
                log(f"[vocab] anki audio failed word={word!r} accent={accent}: {exc}")
        notes.append(AnkiNote(
            word=word,
            phonetic=entry.get("phonetic") or "",
            meaning=esc(entry.get("meaning") or "").replace("\n", "<br>"),
            sentence=esc(entry.get("context_sentence") or ""),
            audio_uk=audio.get("uk"),
            audio_us=audio.get("us"),
            tags=["openclaw_chunk" if entry.get("source") == "weekly_chunk" else "openclaw_lookup"],
        ))
    return notes


def _export_anki_package(chat_id: int, entries: list[dict]) -> None:
    send_message(chat_id, f"Making your Anki deck with UK / US audio for {min(len(entries), ANKI_MAX_WORDS)} "
                          "words -- new words take about a second each.")
    try:
        notes = build_anki_notes(entries, speak=lambda text, accent: tts.speak(text, accent, "mp3"))
        directory = settings.inbox_path.parent / ".openclaw" / "exports"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"openclaw-words-{chat_id}-{_vocab_today().isoformat()}.apkg"
        count = write_apkg(path, "OpenClaw words", notes)
        send_document_file(
            chat_id,
            path,
            f"{count} words with UK / US audio. Open this file with Anki (or File > Import) -- it goes into "
            "the \"OpenClaw words\" deck; importing a newer one updates the same cards.",
        )
        log(f"[vocab] exported apkg chat_id={chat_id} words={count}")
    except Exception as exc:  # noqa: BLE001
        log(f"[vocab] apkg export failed chat_id={chat_id}: {exc}")
        send_message(chat_id, f"Couldn't build the Anki deck: {exc} -- /vocab export tsv still works.")


def _export_word_list(chat_id: int, fmt: str = "") -> None:
    entries = list_word_list(qdrant, settings.tracker_collection, str(chat_id))
    if not entries:
        send_message(chat_id, "Your word list is empty -- nothing to export yet.")
        return
    if fmt != "tsv" and settings.tts_enabled:
        threading.Thread(target=_export_anki_package, args=(chat_id, entries), daemon=True).start()
        return
    directory = settings.inbox_path.parent / ".openclaw" / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"openclaw-words-{chat_id}-{_vocab_today().isoformat()}.txt"
    path.write_text(render_anki_tsv(entries), encoding="utf-8")
    send_document_file(
        chat_id,
        path,
        f"{len(entries)} words for Anki: File > Import this file, pick a Basic note type "
        "and a deck -- fields and tags are set up already.",
    )
    log(f"[vocab] exported chat_id={chat_id} words={len(entries)}")


def _lookup_and_reply(chat_id: int, word: str, sentence: str) -> None:
    try:
        result = lookup_word(dictionary, llm, word, sentence)
        if result is None:
            send_message(chat_id, f'Couldn\'t find "{word}" -- check the spelling and try again.')
            return
        count = save_to_word_list(
            qdrant, embeddings, settings.tracker_collection, str(chat_id), result, sentence
        )
        html = render_lookup_card(result, sentence, count)
        rows = pronunciation_rows(result.word, sentence or result.example)
        if rows:
            send_card_with_buttons(chat_id, html, rows)
        else:
            send_html(chat_id, html)
    except Exception as exc:
        log(f"[vocab] lookup error chat_id={chat_id}: {exc}")
        send_message(chat_id, f"OpenClaw could not look that word up: {exc}")


def handle_vocabulary_command(chat_id: int, text: str) -> bool:
    """/w <word> [| sentence] and /vocab [rm <word>]. Only active when the
    dictionary is enabled (the English-learning bot)."""
    if not settings.dictionary_enabled:
        return False
    lowered = text.lower()
    if lowered == "/say" or lowered.startswith("/say "):
        start_say(chat_id, text[4:].strip())
        return True
    if lowered == "/w" or lowered.startswith("/w "):
        word, sentence = parse_lookup_command(text)
        if not word:
            send_message(chat_id, LOOKUP_USAGE)
            return True
        if len(word.split()) > MAX_LOOKUP_WORDS:
            send_message(chat_id, TOO_LONG_MESSAGE)
            return True
        if not dictionary.available():
            log(f"[vocab] dictionary file missing: {settings.dictionary_path}")
        threading.Thread(target=_lookup_and_reply, args=(chat_id, word, sentence), daemon=True).start()
        return True
    if lowered == "/vocab" or lowered.startswith("/vocab "):
        parts = text.split(maxsplit=2)
        owner = str(chat_id)
        if len(parts) >= 2 and parts[1].lower() == "review":
            _start_vocab_review(chat_id)
            return True
        if len(parts) >= 2 and parts[1].lower() == "quiz":
            start_quiz(chat_id)
            return True
        if len(parts) >= 2 and parts[1].lower() == "export":
            _export_word_list(chat_id, parts[2].strip().lower() if len(parts) == 3 else "")
            return True
        if len(parts) >= 2 and parts[1].lower() == "rm":
            word = parts[2] if len(parts) == 3 else ""
            if not word:
                send_message(chat_id, "Usage: /vocab rm <word>")
            elif remove_from_word_list(qdrant, settings.tracker_collection, owner, word):
                send_message(chat_id, f'Removed "{word}" from your word list.')
            else:
                send_message(chat_id, f'"{word}" isn\'t in your word list.')
            return True
        show_word_list(chat_id)
        return True
    return False


MENU_PREFIX = "menu:"


def menu_items() -> list[tuple[str, str]]:
    """(button label, command it runs) for this bot's /menu, by what's
    enabled here. Every entry is an existing command, so a tap behaves
    exactly like typing it."""
    items: list[tuple[str, str]] = []
    if settings.night_ritual_enabled:
        items += [("Start tonight", "/night start"), ("Night history", "/night")]
    if settings.dictionary_enabled:
        items += [("Word list", "/vocab"), ("Word review", "/vocab review"), ("Word quiz", "/vocab quiz"),
                  ("Say a word", "/say"),
                  ("Anki export", "/vocab export")]
    items += [("Memory", "/mem list"), ("Upcoming", "/mem upcoming")]
    if settings.category_rag_enabled:
        items.append(("Categories", "/cat list"))
    items += [("Schedules", "/cron list"), ("Help", "/help")]
    return items


def send_menu(chat_id: int) -> None:
    buttons = [{"text": label, "callback_data": MENU_PREFIX + command} for label, command in menu_items()]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    send_card_with_buttons(chat_id, f"<b>【選單】</b>· Menu\n{RULE}\nTap what you'd like to do.", rows)


def handle_menu_callback(chat_id: int, data: str, answer) -> None:
    command = data[len(MENU_PREFIX):]
    if command not in {cmd for _, cmd in menu_items()}:
        answer()
        return
    answer(command)
    handle_message({"chat": {"id": chat_id}, "text": command})


WORD_BUTTON_PREFIX = "vw:rm:"


def _word_list_view(chat_id: int, editing: bool | str) -> tuple[str, list[list[dict]]]:
    """editing: False (the list), True (✕ buttons) or "say" (🔊 buttons)."""
    owner = str(chat_id)
    entries = list_word_list(qdrant, settings.tracker_collection, owner)
    due = count_due_words(qdrant, settings.tracker_collection, owner, _vocab_today()) if entries else 0
    html = render_word_list(entries, due)
    if not entries:
        return html, []
    if not editing:
        first = [{"text": "Remove words…", "callback_data": "vw:edit"}]
        if settings.tts_enabled:
            first.append({"text": "🔊 Pronounce…", "callback_data": "vw:say"})
        return html, [first]
    if editing == "say":
        buttons = [
            {"text": f"🔊 {word}", "callback_data": f"{TTS_PREFIX}{_tts_key(word)}:uk"}
            for entry in entries[:LIST_LIMIT]
            if (word := entry.get("display_word") or entry.get("word", ""))
        ]
        rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
        rows.append([{"text": "Done", "callback_data": "vw:done"}])
        return html, rows
    buttons = []
    for entry in entries[:LIST_LIMIT]:
        word = entry.get("display_word") or entry.get("word", "")
        data = WORD_BUTTON_PREFIX + (entry.get("word") or word)
        if word and len(data.encode("utf-8")) <= 64:
            buttons.append({"text": f"✕ {word}", "callback_data": data})
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([{"text": "Done", "callback_data": "vw:done"}])
    return html, rows


def show_word_list(chat_id: int) -> None:
    html, rows = _word_list_view(chat_id, editing=False)
    if rows:
        send_card_with_buttons(chat_id, html, rows)
    else:
        send_html(chat_id, html)


def handle_word_list_callback(chat_id: int, message_id: int | None, data: str, answer) -> None:
    """/vocab card buttons: Remove words… shows one ✕ button per word;
    tapping one removes it and redraws the list; Done goes back."""
    if data.startswith(WORD_BUTTON_PREFIX):
        word = data[len(WORD_BUTTON_PREFIX):]
        removed = remove_from_word_list(qdrant, settings.tracker_collection, str(chat_id), word)
        answer(f"Removed {word}" if removed else "Already gone")
        editing = True
    else:
        answer()
        editing = {"vw:edit": True, "vw:say": "say"}.get(data, False)
    if not message_id:
        return
    html, rows = _word_list_view(chat_id, editing=editing)
    if rows:
        _edit_card_buttons(chat_id, message_id, html, rows)
    else:
        _edit_card(chat_id, message_id, html)


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

Proactive reminders
Example: /cron add daily 08:00 Memory digest :: /mem digest
Pushes overdue / due-soon / stale items from /mem. A day with nothing to
report stays silent -- see /mem digest and docs/TRACKER_MEMORY.md.
"""


def agents_text() -> str:
    lines = ["OpenClaw Agents"]
    for status in agent_registry.statuses():
        endpoint = f" -> {status.endpoint_id}" if status.endpoint_id else ""
        lines.append(f"- {status.name}: {status.status} ({status.model_policy}{endpoint})")
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
        {"command": "mem", "description": "Save, list, or complete memory items (see /mem list)"},
        {"command": "rag", "description": "Query local memory and knowledge base"},
        {"command": "doc", "description": "Import a public Google Doc or document source"},
        {"command": "cat", "description": "List category knowledge bases (see /cat help)"},
        {"command": "search", "description": "Search the web"},
        {"command": "cron", "description": "Configure proactive push schedules"},
        {"command": "new", "description": "Start a new chat conversation (clear context)"},
        {"command": "keep", "description": "Pin a fact so chat always remembers it"},
        {"command": "history", "description": "Preview what the chat remembers about this conversation"},
        {"command": "agents", "description": "List OpenClaw agents"},
        {"command": "tasks", "description": "View recent task history"},
        {"command": "review", "description": "Run a bounded private engineering review"},
        {"command": "start", "description": "Show status and usage"},
    ]
    if settings.dictionary_enabled:
        commands[1:1] = [
            {"command": "w", "description": "Look up a word (added to your word list)"},
            {"command": "vocab", "description": "Show your word list"},
            {"command": "say", "description": "Pronunciation practice: say a word"},
        ]
    commands.insert(0, {"command": "menu", "description": "Buttons for the common commands"})
    if settings.night_ritual_enabled:
        commands.insert(1, {"command": "night", "description": "Night ritual: start, history, reports"})
    telegram("setMyCommands", {"commands": commands}, timeout=20)


ACK_MESSAGES = {
    "memory_agent": "Got it, writing to local memory.",
    "rag_agent": "Got it, querying local memory and the document knowledge base.",
    "browser_search_agent": "Got it, searching the web and summarizing with the local reasoning model.",
    "weather_agent": "Got it, checking the weather.",
    "engineering_review_agent": "Got it, running the bounded local engineering review.",
}
DEFAULT_ACK_MESSAGE = "Got it, handing this to the local reasoning model."

MODEL_PAUSED_MESSAGE = (
    "The local model engine is not responding. It may be paused to free the GPU "
    "(OPENCLAW_BOOT_MODE=core) or still loading a model.\n\n"
    "Still works without it: /mem writes, /cat, /doc imports, /cron, /tasks, "
    "/agents, /help.\n"
    "On the host, start it with:  bin/openclawctl start model"
)


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


# /rag answers the buttons refer to: key -> {"question", "answer", "chat_id"}.
RAG_ANSWERS: "OrderedDict[str, dict]" = OrderedDict()
RAG_ANSWERS_MAX = 200
# chat_id -> {"question", "answer", "expires_at"} while a follow-up is awaited.
RAG_FOLLOWUP: dict[int, dict] = {}
RAG_LOCK = threading.Lock()
RAG_FOLLOWUP_TTL = 10 * 60
_RAG_SCOPE = re.compile(r"^((?:(?:source|tag|since|before):\S+\s+|[#＃](?:\[[^\]]+\]|\S+)\s+)*)")


def rag_answer_rows(chat_id: int, question: str, answer: str) -> list[list[dict]]:
    """Buttons under a /rag answer: its passages, a follow-up, and (with
    categories on) saving the answer as a document."""
    if not question.startswith(("/rag ", "rag:")) or question.strip().lower().startswith("/rag digest"):
        return []
    key = hashlib.sha1(f"{chat_id}:{question}:{answer}".encode("utf-8")).hexdigest()[:12]
    with RAG_LOCK:
        RAG_ANSWERS[key] = {"question": question, "answer": answer}
        while len(RAG_ANSWERS) > RAG_ANSWERS_MAX:
            RAG_ANSWERS.popitem(last=False)
    row = []
    if sources_for_answer(answer):
        row.append({"text": "📄 Show sources", "callback_data": f"rag:src:{key}"})
    row.append({"text": "↪ Follow-up", "callback_data": f"rag:ask:{key}"})
    if settings.category_rag_enabled:
        row.append({"text": "💾 Save answer", "callback_data": f"rag:save:{key}"})
    return [row]


def send_plain_with_buttons(chat_id: int, text: str, rows: list[list[dict]]) -> None:
    try:
        telegram("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": {"inline_keyboard": rows}})
    except Exception as exc:  # noqa: BLE001
        log(f"[telegram] answer with buttons failed: {exc}")
        send_message(chat_id, text)


def follow_up_query(question: str, text: str) -> str:
    """Keep the first question's scope (#category, source:, tag:...) and name
    it, so retrieval and the model both see what the follow-up refers to."""
    body = question.split(" ", 1)[1] if " " in question else ""
    scope = _RAG_SCOPE.match(body).group(1) if body else ""
    previous = body[len(scope):].strip()
    return f"/rag {scope}{text.strip()} (follow-up to: {previous})"


def handle_rag_callback(chat_id: int, message_id: int | None, data: str, answer) -> None:
    _, action, key = (data.split(":", 2) + ["", ""])[:3]
    with RAG_LOCK:
        record = RAG_ANSWERS.get(key)
    if record is None:
        answer("This answer has expired -- ask again with /rag.")
        return
    if action == "src":
        answer()
        passages = sources_for_answer(record["answer"])
        if not passages:
            send_message(chat_id, "The passages for this answer are no longer kept -- ask again with /rag.")
            return
        blocks = [f"{bold(p['source'])}\n{esc(p['text'][:400] + ('…' if len(p['text']) > 400 else ''))}" for p in passages[:5]]
        send_html(chat_id, f"<b>【來源】</b>· Sources\n{RULE}\n" + "\n\n".join(blocks))
        return
    if action == "ask":
        answer()
        with RAG_LOCK:
            RAG_FOLLOWUP[chat_id] = {**record, "expires_at": time.time() + RAG_FOLLOWUP_TTL}
        telegram("sendMessage", {
            "chat_id": chat_id,
            "text": "Type your follow-up question.",
            "reply_markup": {"force_reply": True, "input_field_placeholder": "Follow-up question"},
        })
        return
    if action == "save" and settings.category_rag_enabled:
        answer()
        question_text = record["question"].split(" ", 1)[1] if " " in record["question"] else record["question"]
        staged = unique_path(category_staging_dir(), f"{timestamp()}-rag-answer.md")
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(f"# {question_text}\n\n{record['answer']}\n", encoding="utf-8")
        set_pending_category(chat_id, {"path": str(staged), "kind": "document", "note": "",
                                       "original_name": f"rag answer: {question_text[:60]}.md"})
        send_category_picker(chat_id, f"<b>檔案｜選擇分類</b>\n{RULE}\nSave this answer into which category?\n\n"
                                      "Tap a category, or type a new name.")
        return
    answer()


def handle_rag_followup(chat_id: int, message: dict) -> bool:
    text = (message.get("text") or "").strip()
    if not text or text.startswith("/"):
        return False
    with RAG_LOCK:
        pending = RAG_FOLLOWUP.pop(chat_id, None)
    if not pending or time.time() > pending["expires_at"]:
        return False
    threading.Thread(
        target=handle_text_message, args=(chat_id, follow_up_query(pending["question"], text)), daemon=True
    ).start()
    return True


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
            if str(exc) == VLLM_NOT_READY_MESSAGE or not llm.is_reachable():
                send_message(chat_id, MODEL_PAUSED_MESSAGE)
            else:
                send_message(chat_id, f"The OpenClaw runtime could not respond right now: {exc}")
            return
        answer = dispatch.answer or "The OpenClaw runtime returned an empty reply."
        rows = rag_answer_rows(chat_id, text, answer) if dispatch.agent_name == "rag_agent" else []
        if rows and len(answer) <= settings.max_reply_chars:
            send_plain_with_buttons(chat_id, answer, rows)
        else:
            send_message(chat_id, answer)
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

    if settings.category_rag_enabled:
        try:
            sweep_expired_pending()
        except Exception as exc:  # noqa: BLE001 - never let housekeeping drop a message
            log(f"[category] sweep error: {exc}")

    if text.lower() in ("/menu", "/start menu"):
        send_menu(chat_id)
        return

    try:
        if night_command(chat_id, text):
            return
        if handle_night_reply(chat_id, message):
            return
        if handle_rag_followup(chat_id, message):
            return
        if settings.dictionary_enabled and handle_say_reply(chat_id, message):
            return
        if handle_vocab_review_answer(chat_id, message):
            return
        if handle_bare_word_lookup(chat_id, message):
            return
        if handle_english_bot_pending_reply(chat_id, message):
            return
    except Exception as exc:
        log(f"[english_bot] pending-reply routing error chat_id={chat_id}: {exc}")
        send_message(chat_id, f"OpenClaw could not process that reply: {exc}")
        return

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

    try:
        if handle_reply_category_message(chat_id, message):
            return
    except Exception as exc:
        log(f"[category] reply ingest error chat_id={chat_id}: {exc}")
        send_message(chat_id, f"OpenClaw could not file that reply into a category: {exc}")
        return

    try:
        if handle_video_link_message(chat_id, text):
            return
    except Exception as exc:
        log(f"[video] relay dispatch error chat_id={chat_id}: {exc}")
        send_message(chat_id, f"OpenClaw could not start the video summary relay: {exc}")
        return

    if not text:
        send_message(chat_id, "The OpenClaw runtime currently accepts text, documents, photos, and voice messages. Please send text or upload a file.")
        return

    if text in {"/start", "/help"}:
        send_message(chat_id, help_text())
        return

    if handle_vocabulary_command(chat_id, text):
        return

    if text.lower() in {"/new", "/reset"}:
        if not conversation_memory.enabled:
            send_message(chat_id, "Conversation memory is disabled, so every chat message is already independent.")
        elif conversation_memory.clear(chat_id):
            send_message(chat_id, "Started a new conversation. Earlier chat turns will not be used as context.")
        else:
            send_message(chat_id, "No active conversation to clear -- the next message starts fresh.")
        return

    if text.lower() == "/keep" or text.lower().startswith("/keep "):
        fact = text[len("/keep") :].strip()
        if not conversation_memory.enabled:
            send_message(chat_id, "Conversation memory is disabled, so there's nothing to pin.")
        elif not fact:
            send_message(chat_id, "Usage: /keep <fact to remember for this conversation>")
        elif conversation_memory.pin(chat_id, fact):
            send_message(chat_id, f"Pinned: {fact}")
        else:
            send_message(chat_id, "Could not pin that.")
        return

    if text.lower() == "/history":
        if not conversation_memory.enabled:
            send_message(chat_id, "Conversation memory is disabled, so there's no history to show.")
        else:
            snapshot = conversation_memory.preview(chat_id)
            if not snapshot:
                send_message(chat_id, "No conversation history yet for this chat.")
            else:
                send_message(chat_id, format_history_preview(snapshot))
        return

    if settings.category_rag_enabled:
        # a bare "cancel" while a file waits is a cancel, not a category named "cancel"
        if text.lower() in {"/cancel", "/skip"} or (
            text.lower() in {"cancel", "skip"} and has_pending_category(chat_id)
        ):
            dropped = pop_pending_category(chat_id)
            if dropped:
                close_category_pickers(chat_id, dropped)
                sweep_pending_items_to_default(dropped)
                send_message(chat_id, "Okay, cancelled. Any waiting document goes to the general knowledge base.")
            else:
                send_message(chat_id, "Nothing was waiting for a category.")
            return
        if handle_category_command(chat_id, text):
            return
        if not text.startswith("/") and has_pending_category(chat_id):
            if resolve_pending_with_category(chat_id, text):
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


# --- Night ritual (bot2) -----------------------------------------------------
# One owner, one open night at a time: chat_id -> {"date": "YYYY-MM-DD",
# "step": "check" | "wins" | "better" | "adjust" | "first"}. Saved with the
# other open tasks so a restart mid-ritual picks up at the same question.
NIGHT_PENDING: dict[int, dict] = {}
NIGHT_LOCK = threading.Lock()
# serializes answers so two quick messages can't both claim the same step
NIGHT_ANSWER_LOCK = threading.Lock()
NIGHT_CALLBACK_PREFIX = "night_first:"
NIGHT_SCHEDULE_PREFIX = "night_sched:"
NIGHT_HELP_TEXT = """
Night ritual (22:30, Sun-Fri):
/night            Recent nights and the latest entry
/night start      Start (or pick up) tonight's four questions now
/night 7d         The last 7 nights in full
/night 2026-09-28 One night
/night week       Last week's report · /night month  last month's · /night year
/night move 2026-09-29 2026-09-28   Re-date a night (e.g. one you wrote about yesterday)
"""


def night_store() -> NightStore:
    return NightStore(settings.night_ritual_dir)


def _night_tz() -> ZoneInfo:
    return ZoneInfo(settings.night_ritual_timezone)


def _night_now() -> datetime:
    return datetime.now(_night_tz())


def _night_ritual_days() -> set[int]:
    return parse_ritual_days(settings.night_ritual_days)


def is_night_owner(chat_id: int) -> bool:
    return settings.night_ritual_enabled and settings.night_ritual_owner == chat_id


def _set_night_pending(chat_id: int, item: dict | None) -> None:
    with NIGHT_LOCK:
        if item is None:
            NIGHT_PENDING.pop(chat_id, None)
        else:
            NIGHT_PENDING[chat_id] = item
    save_pending_state()


def _peek_night_pending(chat_id: int) -> dict | None:
    with NIGHT_LOCK:
        item = NIGHT_PENDING.get(chat_id)
        return dict(item) if item else None


def _send_night_question(chat_id: int, step: str) -> None:
    send_html(chat_id, render_question(step))


def _send_first_check(chat_id: int, previous: dict) -> None:
    data = NIGHT_CALLBACK_PREFIX + "{}:" + previous["date"]
    markup = {
        "inline_keyboard": [
            [{"text": "✅ Done", "callback_data": data.format("y")}, {"text": "❌ Not yet", "callback_data": data.format("n")}]
        ]
    }
    telegram(
        "sendMessage",
        {"chat_id": chat_id, "text": render_first_check(previous), "parse_mode": "HTML", "reply_markup": markup},
    )


def start_night(chat_id: int, *, manual: bool) -> bool:
    """Open tonight's ritual (or pick it up where it stopped). Returns False
    when tonight is already done."""
    store = night_store()
    owner = str(chat_id)
    today = _night_now().date()
    entry = store.load(owner, today)
    if entry and entry.get("status") == "done":
        if manual:
            send_message(chat_id, "Tonight's wind-down is already done. /night shows it.")
        return False
    if entry is None:
        entry = new_entry(today, _night_now().isoformat())
    entry["status"] = "open"
    store.save(owner, entry)
    previous = None if entry.get("checked_previous") else store.previous_to_check(owner, today)
    if previous is not None:
        _set_night_pending(chat_id, {"date": today.isoformat(), "step": "check"})
        _send_first_check(chat_id, previous)
    else:
        step = next_step(entry) or "first"
        _set_night_pending(chat_id, {"date": today.isoformat(), "step": step})
        _send_night_question(chat_id, step)
    log(f"[night] started date={today} manual={manual}")
    return True


def handle_night_callback(chat_id: int, message_id: int | None, data: str, answer) -> None:
    if not is_night_owner(chat_id):
        answer()
        return
    try:
        _, flag, day_text = data.split(":", 2)
        previous_day = date.fromisoformat(day_text)
    except ValueError:
        answer()
        return
    store = night_store()
    owner = str(chat_id)
    previous = store.load(owner, previous_day)
    if previous is None:
        answer()
        return
    done = flag == "y"
    previous["first_done"] = done
    store.save(owner, previous)
    answer("Noted")
    if message_id:
        _edit_card(chat_id, message_id, render_first_check_answered(previous, done))
    pending = _peek_night_pending(chat_id)
    if not pending or pending.get("step") != "check":
        return
    tonight = store.load(owner, date.fromisoformat(pending["date"])) or new_entry(
        date.fromisoformat(pending["date"]), _night_now().isoformat()
    )
    tonight["checked_previous"] = True
    store.save(owner, tonight)
    step = next_step(tonight) or "first"
    _set_night_pending(chat_id, {"date": pending["date"], "step": step})
    _send_night_question(chat_id, step)


def _night_answer(chat_id: int, text: str, spoken: str = "") -> None:
    with NIGHT_ANSWER_LOCK:
        pending = _peek_night_pending(chat_id)
        if not pending or pending.get("step") in (None, "check"):
            return
        store = night_store()
        owner = str(chat_id)
        day = date.fromisoformat(pending["date"])
        entry = store.load(owner, day) or new_entry(day, _night_now().isoformat())
        entry.setdefault("answers", {})[pending["step"]] = text.strip()
        if spoken:
            entry.setdefault("spoken", {})[pending["step"]] = spoken.strip()
        following = next_step(entry)
        if following is not None:
            store.save(owner, entry)
            _set_night_pending(chat_id, {"date": pending["date"], "step": following})
            _send_night_question(chat_id, following)
            return
        entry["status"] = "done"
        entry["completed_at"] = _night_now().isoformat()
        store.save(owner, entry)
        _set_night_pending(chat_id, None)
    closing = closing_line(llm, entry)
    entry["closing"] = closing
    store.save(owner, entry)
    send_card_with_buttons(
        chat_id,
        render_closing(entry, closing),
        [[{"text": "Add to tomorrow's schedule", "callback_data": NIGHT_SCHEDULE_PREFIX + entry["date"]}]],
    )
    log(f"[night] done date={entry['date']}")


def add_first_thing_to_schedule(entry: dict) -> None:
    """Save the night's first thing as a /mem item due the next day, in the
    schedule collection (another bot's tracker memory, so it shows in that
    bot's morning schedule report) or this bot's own."""
    first = (entry.get("answers") or {}).get("first", "").strip()
    due = date.fromisoformat(entry["date"]) + timedelta(days=1)
    target = settings.night_ritual_schedule_collection or settings.tracker_collection
    skill = MemoryWriteSkill(dataclasses.replace(settings, tracker_collection=target), {}, embeddings, qdrant)
    skill.run(f"/mem {first} due:{due.isoformat()} tag:night")


def handle_night_schedule_callback(chat_id: int, message_id: int | None, data: str, answer) -> None:
    if not is_night_owner(chat_id):
        answer()
        return
    try:
        day = date.fromisoformat(data[len(NIGHT_SCHEDULE_PREFIX):])
    except ValueError:
        answer()
        return
    store = night_store()
    entry = store.load(str(chat_id), day)
    if not entry or not (entry.get("answers") or {}).get("first"):
        answer()
        return
    if entry.get("scheduled"):
        answer("Already on tomorrow's schedule")
    else:
        try:
            add_first_thing_to_schedule(entry)
        except Exception as exc:  # noqa: BLE001
            log(f"[night] schedule add failed: {exc}")
            answer("Couldn't add it -- try again later.")
            return
        entry["scheduled"] = True
        store.save(str(chat_id), entry)
        answer("Added to tomorrow's schedule")
        log(f"[night] first thing scheduled date={day}")
    if message_id:
        _edit_card(
            chat_id,
            message_id,
            render_closing(entry, entry.get("closing") or "") + "\n\n<i>Added to tomorrow's schedule.</i>",
        )


def handle_night_reply(chat_id: int, message: dict) -> bool:
    """Text or voice while tonight's ritual is open is the answer to the
    current question. Commands pass through untouched."""
    if not is_night_owner(chat_id):
        return False
    pending = _peek_night_pending(chat_id)
    if not pending:
        return False
    voice = message.get("voice") or message.get("audio")
    text = (message.get("text") or "").strip()
    if not voice and not text:
        return False
    if text.startswith("/"):
        return False
    if pending.get("step") == "check":
        send_message(chat_id, "Tap ✅ or ❌ on the card above first.")
        return True
    if not voice:
        threading.Thread(target=_night_answer, args=(chat_id, text), daemon=True).start()
        return True
    file_id = voice.get("file_id")
    if not file_id:
        return False
    file_info = telegram_file_info(file_id)
    suffix = extension_from_file_path(file_info.get("file_path", ""), voice.get("mime_type"), ".ogg")
    target = unique_path(settings.inbox_path / "audio" / "telegram", f"{timestamp()}-night{suffix}")
    downloaded_path, _ = download_telegram_file(file_id, target)

    def transcribe_and_answer() -> None:
        try:
            transcript = transcriber.transcribe(downloaded_path)
        except Exception as exc:  # noqa: BLE001
            log(f"[night] transcription error: {exc}")
            send_message(chat_id, "Couldn't transcribe that voice message -- try again, or type it.")
            return
        finally:
            downloaded_path.unlink(missing_ok=True)  # the diary keeps the text, not the recording
        if not transcript.strip():
            send_message(chat_id, "Didn't catch any words in that one -- try again, or type it.")
            return
        step = (_peek_night_pending(chat_id) or {}).get("step", "")
        condensed = condense_voice_answer(llm, step, transcript) if step in ("wins", "better", "adjust", "first") \
            else transcript
        _night_answer(chat_id, condensed, spoken=transcript if condensed != transcript.strip() else "")

    threading.Thread(target=transcribe_and_answer, daemon=True).start()
    return True


def close_stale_night(now: datetime) -> None:
    """At midnight an unfinished night is saved as it is: partial if some
    questions were answered, missed if none."""
    owner_id = settings.night_ritual_owner
    pending = _peek_night_pending(owner_id) if owner_id else None
    if not pending or date.fromisoformat(pending["date"]) >= now.date():
        return
    store = night_store()
    day = date.fromisoformat(pending["date"])
    entry = store.load(str(owner_id), day) or new_entry(day, now.isoformat())
    entry["status"] = close_status(entry)
    store.save(str(owner_id), entry)
    _set_night_pending(owner_id, None)
    log(f"[night] closed date={day} status={entry['status']}")


def night_command(chat_id: int, text: str) -> bool:
    if text != "/night" and not text.startswith("/night "):
        return False
    if not is_night_owner(chat_id):
        send_message(chat_id, "The night ritual isn't set up for this chat.")
        return True
    arg = text[len("/night"):].strip().lower()
    store = night_store()
    owner = str(chat_id)
    today = _night_now().date()
    ritual_days = _night_ritual_days()
    if arg == "start":
        start_night(chat_id, manual=True)
        return True
    if arg.startswith("move"):
        parts = arg.split()
        try:
            source, target = date.fromisoformat(parts[1]), date.fromisoformat(parts[2])
        except (IndexError, ValueError):
            send_message(chat_id, "Use /night move <from> <to>, e.g. /night move 2026-09-29 2026-09-28.")
            return True
        pending = _peek_night_pending(chat_id)
        if pending and pending.get("date") == source.isoformat():
            send_message(chat_id, "That night is still open -- finish it (or wait for midnight) before moving it.")
            return True
        try:
            store.move(owner, source, target)
        except ValueError as exc:
            send_message(chat_id, f"Couldn't move it: {exc}.")
            return True
        send_message(chat_id, f"Moved the night of {source.isoformat()} to {target.isoformat()}.")
        log(f"[night] moved {source} -> {target}")
        return True
    if arg in ("week", "month", "year"):
        send_message(chat_id, f"Putting together the {arg} report…")

        def report() -> None:
            html, _ = build_report(llm, store, owner, arg, today, ritual_days)
            send_html(chat_id, html)

        threading.Thread(target=report, daemon=True).start()
        return True
    days_back = 7
    detail_days: list[date]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
        try:
            detail_days = [date.fromisoformat(arg)]
        except ValueError:
            send_message(chat_id, "Use /night YYYY-MM-DD, e.g. /night 2026-09-28.")
            return True
    elif re.fullmatch(r"\d{1,2}d", arg):
        days_back = max(1, min(31, int(arg[:-1])))
        detail_days = [today - timedelta(days=n) for n in range(days_back)]
    elif arg:
        send_message(chat_id, NIGHT_HELP_TEXT.strip())
        return True
    else:
        detail_days = []
    days = [today - timedelta(days=n) for n in range(days_back - 1, -1, -1)]
    entries = {d: e for d in days + detail_days if (e := store.load(owner, d))}
    if detail_days:
        detail = [entries[d] for d in detail_days if d in entries]
    else:
        latest = [entries[d] for d in sorted(entries, reverse=True) if any((entries[d].get("answers") or {}).values())]
        detail = latest[:1]
    send_html(chat_id, render_history(entries, days, ritual_days, today, detail=detail))
    return True


def _hhmm(text: str):
    hour, minute = validate_time(text).split(":")
    return dt_time(int(hour), int(minute))


def _night_state_path() -> Path:
    return settings.night_ritual_dir / "state.json"


def _load_night_state() -> dict:
    try:
        return json.loads(_night_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_night_state(state: dict) -> None:
    path = _night_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def night_tick(now: datetime, state: dict) -> None:
    """One pass of the night-ritual schedule. Each step fires once a day
    (tracked in state) and only inside its window, so a restart never
    replays an old reminder or a stale morning card."""
    owner_id = settings.night_ritual_owner
    owner = str(owner_id)
    today = now.date()
    iso = today.isoformat()
    clock = now.time()
    ritual_days = _night_ritual_days()
    store = night_store()

    close_stale_night(now)

    reminders = [_hhmm(t) for t in settings.night_ritual_reminder_times.split(",") if t.strip()]
    last_start = reminders[-1] if reminders else dt_time(23, 59)
    start = _hhmm(settings.night_ritual_start_time)
    if today.weekday() in ritual_days and start <= clock < last_start and state.get("started") != iso:
        state["started"] = iso
        pending = _peek_night_pending(owner_id)
        if not (pending and pending.get("date") == iso):
            start_night(owner_id, manual=False)

    for index, at in enumerate(reminders):
        key = f"reminder{index}"
        end = reminders[index + 1] if index + 1 < len(reminders) else dt_time(23, 59, 59)
        if at <= clock <= end and state.get(key) != iso:
            state[key] = iso
            pending = _peek_night_pending(owner_id)
            if pending and pending.get("date") == iso:
                send_html(owner_id, render_reminder(pending.get("step"), last=index == len(reminders) - 1))

    morning = _hhmm(settings.night_ritual_morning_time)
    if morning <= clock < dt_time(12, 0) and state.get("morning") != iso:
        state["morning"] = iso
        yesterday = store.load(owner, today - timedelta(days=1))
        first = ((yesterday or {}).get("answers") or {}).get("first")
        if first:
            send_html(owner_id, render_morning(first))

    kinds = reports_due(today)
    prepare_text = settings.night_ritual_prepare_time.strip()
    if kinds and prepare_text and _hhmm(prepare_text) <= clock < _hhmm(settings.night_ritual_report_time) \
            and state.get("prepared") != iso:
        state["prepared"] = iso
        state["reports"] = {kind: build_report(llm, store, owner, kind, today, ritual_days)[0] for kind in kinds}
        state["reports_date"] = iso
        log(f"[night] prepared reports={kinds}")

    report_at = _hhmm(settings.night_ritual_report_time)
    if kinds and report_at <= clock < dt_time(12, 0) and state.get("reported") != iso:
        state["reported"] = iso
        prepared = state.get("reports") if state.get("reports_date") == iso else {}
        for kind in kinds:
            html = (prepared or {}).get(kind) or build_report(llm, store, owner, kind, today, ritual_days)[0]
            send_html(owner_id, html)
        log(f"[night] sent reports={kinds} ({'prepared' if prepared else 'live'})")
        state.pop("reports", None)


def housekeeping_due(now: datetime, last_date: str) -> bool:
    if not settings.housekeeping_time:
        return False
    return now.time() >= _hhmm(settings.housekeeping_time) and last_date != now.date().isoformat()


def _housekeeping_loop() -> None:
    """Once a day: delete this bot's short-lived files (see housekeeping.py)."""
    workspace = settings.inbox_path.parent
    tz = ZoneInfo(settings.cron_timezone or "UTC")
    last = ""
    try:
        last = json.loads((workspace / ".openclaw" / "housekeeping.json").read_text(encoding="utf-8")).get("date", "")
    except (OSError, json.JSONDecodeError):
        pass
    while RUNNING:
        now = datetime.now(tz)
        if housekeeping_due(now, last):
            last = now.date().isoformat()
            try:
                result = run_cleanup(workspace, audio_days=settings.audio_retention_days)
                write_status(workspace, result)
                status_path = workspace / ".openclaw" / "housekeeping.json"
                data = json.loads(status_path.read_text(encoding="utf-8"))
                data["date"] = last
                status_path.write_text(json.dumps(data), encoding="utf-8")
                log(f"[housekeeping] removed files={result.files} bytes={result.bytes} {result.by_area}")
            except Exception:
                log(f"[housekeeping] error: {traceback.format_exc()}")
        time.sleep(600)


def _night_ritual_loop() -> None:
    while RUNNING:
        try:
            state = _load_night_state()
            before = json.dumps(state, sort_keys=True)
            try:
                night_tick(_night_now(), state)
            finally:
                if json.dumps(state, sort_keys=True) != before:
                    _save_night_state(state)
            alerter.resolve("night-ritual", "The night ritual's schedule works again.")
        except Exception:
            log(f"[night] loop error: {traceback.format_exc()}")
            alerter.alert("night-ritual", "The night ritual's schedule hit an error.", traceback.format_exc())
        time.sleep(30)


def _english_bot_send_message(owner: str, text: str) -> None:
    """Daily task cards. With the word list enabled, a Word review block is
    added at the end for whoever has saved words due -- counted per owner,
    since each person's list is their own."""
    if settings.dictionary_enabled:
        try:
            text += render_review_reminder(
                count_due_words(qdrant, settings.tracker_collection, owner, _vocab_today())
            )
        except Exception as exc:  # noqa: BLE001 - never let the reminder block the day's task
            log(f"[vocab] due-word count failed owner={owner}: {exc}")
    send_html(int(owner), text)


def _english_bot_send_report(owner: str, html: str) -> None:
    """Cards that aren't the day's task (the Sunday recap): no Word review
    block appended."""
    send_html(int(owner), html)


def _english_bot_send_audio(owner: str, path: Path, caption: str) -> None:
    # An .ogg clip is Opus, so it goes out as a voice message; MP3 as audio.
    if path.suffix.lower() in (".ogg", ".oga"):
        send_voice_file(int(owner), path, caption)
    else:
        send_audio_file(int(owner), path, caption)


def _english_bot_set_pending_answer(owner: str, item: dict) -> None:
    set_pending_answer(int(owner), item)
    if item.get("kind") == "eng_mon":
        _send_gist_card(int(owner), int(item.get("week_number") or 0))
    if item.get("kind") in ("eng_mon", "eng_fri"):
        _send_chunk_pronunciation(int(owner), int(item.get("week_number") or 0))


def _send_chunk_pronunciation(chat_id: int, week_number: int) -> None:
    """🔊 for this week's chunks and their examples (Monday and Friday --
    not Saturday, where hearing them would give the cloze away)."""
    if not settings.tts_enabled:
        return
    try:
        chunks = read_this_week_chunks(qdrant, settings.tracker_collection, week_number)
    except Exception as exc:  # noqa: BLE001
        log(f"[tts] chunk pronunciation skipped: {exc}")
        return
    rows = []
    for chunk in chunks:
        phrase = (chunk.get("phrase") or "").strip()
        if not phrase:
            continue
        key = _tts_key(phrase)
        rows.append([{"text": f"🔊 UK · {phrase}", "callback_data": f"{TTS_PREFIX}{key}:uk"},
                     {"text": "🔊 US", "callback_data": f"{TTS_PREFIX}{key}:us"}])
        example = (chunk.get("context_sentence") or "").strip()
        if example:
            key = _tts_key(example)
            rows.append([{"text": "🔊 UK example", "callback_data": f"{TTS_PREFIX}{key}:uk"},
                         {"text": "🔊 US example", "callback_data": f"{TTS_PREFIX}{key}:us"}])
    if rows:
        send_card_with_buttons(
            chat_id, f"<b>【語塊發音】</b>· Week {week_number}\n{RULE}\nHear this week's chunks and examples.", rows
        )


def _send_gist_card(chat_id: int, week_number: int) -> None:
    """Monday's gist as A/B/C buttons, sent as the task opens (both for a
    prepared and a live push). Weeks without stored options keep the typed
    gist and get no card."""
    try:
        payload = read_this_week_payload(qdrant, settings.tracker_collection, week_number)
    except ValueError:
        return
    options = list(payload.get("gist_options") or [])
    if len(options) != 3:
        return
    send_card_with_buttons(
        chat_id,
        render_gist_card(week_number, options),
        [[{"text": letter, "callback_data": f"gist:{week_number}:{index}"} for index, letter in enumerate("ABC")]],
    )


def handle_gist_callback(chat_id: int, message_id: int | None, data: str, answer) -> None:
    try:
        _, week_text, choice_text = data.split(":")
        week_number, choice = int(week_text), int(choice_text)
        payload = read_this_week_payload(qdrant, settings.tracker_collection, week_number)
    except ValueError:
        answer()
        return
    options = list(payload.get("gist_options") or [])
    correct_index = int(payload.get("gist_answer", -1))
    if len(options) != 3 or not 0 <= choice < 3:
        answer()
        return
    first = record_gist_choice(
        qdrant, embeddings, settings.tracker_collection, str(chat_id), week_number, choice, correct_index
    )
    if not first:
        answer("You've already answered this one.")
    else:
        answer("Right!" if choice == correct_index else f"Not quite -- it's {'ABC'[correct_index]}")
        log(f"[english_bot] gist answered chat_id={chat_id} week={week_number} correct={choice == correct_index}")
    if message_id and first:
        _edit_card(chat_id, message_id, render_gist_card(week_number, options, chosen=choice, answer=correct_index))


def _english_bot_scheduler_loop() -> None:
    """Background daily push/sweep loop for the English-learning bot
    (bot4 only -- gated on OPENCLAW_ENGLISH_BOT_ENABLED in main(), never
    runs for the other 3 bot profiles sharing this codebase). Lives in
    this process (not the separate openclaw-cron container) so it can
    call directly into llm/qdrant/embeddings/send_message and PENDING_ANSWER
    without any cross-container RPC -- see openclaw_eng_spec.md's wiring
    design. Polls roughly once a minute; english_bot_scheduler's own
    owner/chat_id convention is str (matches owned_records.py), so every
    call across this boundary converts str<->int explicitly (see the three
    wrapper functions above and str(o) below) -- English-learning content
    and per-user tracker state all live in Qdrant already, this loop's
    own local state file only tracks "did today's push/sweep already run"
    to survive restarts without double-firing."""
    owners = [str(o) for o in settings.english_bot_owners]
    if not owners:
        log("[english_bot] scheduler enabled but OPENCLAW_ENGLISH_BOT_OWNERS is empty -- nothing to do")
        return
    tz = ZoneInfo(settings.english_bot_timezone)
    while RUNNING:
        try:
            now = datetime.now(tz)
            state = load_english_bot_state(settings.english_bot_state_path, {})
            day_code = settings.english_bot_force_day_code or day_code_for(now)
            generation = dict(
                day_code=day_code,
                llm=llm,
                qdrant=qdrant,
                embeddings=embeddings,
                collection=settings.tracker_collection,
                owners=owners,
                clip_client=english_bot_clip_client,
                transcription_client=transcriber,
                workspace_root=settings.inbox_path,
                vocab_enabled=settings.dictionary_enabled,
            )
            if should_prepare_today(now, settings.english_bot_prepare_time, state):
                # Off-peak: generate now, deliver at push time. One attempt a
                # day; if it fails, the push generates live as before.
                mark_prepare_attempted(now, state)
                write_english_bot_state(settings.english_bot_state_path, state)
                try:
                    items = prepare_todays_push(**generation)
                    store_prepared(now, day_code, items, state)
                    write_english_bot_state(settings.english_bot_state_path, state)
                    log(f"[english_bot] prepared day={day_code} items={len(items)}")
                    alerter.resolve("english-prepare", "The English bot's off-peak preparation works again.")
                except Exception:
                    log(f"[english_bot] prepare failed day={day_code}, will generate at push time: {traceback.format_exc()}")
                    alerter.alert(
                        "english-prepare",
                        f"The English bot's off-peak preparation failed ({day_code}); "
                        "today's task will be generated at push time instead.",
                        traceback.format_exc(),
                    )
            if should_push_today(now, settings.english_bot_push_time, state):
                prepared = prepared_for(now, day_code, state)
                if prepared is not None:
                    deliver_outbox(
                        prepared,
                        send_message=_english_bot_send_message,
                        send_audio=_english_bot_send_audio,
                        send_report=_english_bot_send_report,
                        set_pending_answer=_english_bot_set_pending_answer,
                    )
                else:
                    run_todays_push(
                        send_message=_english_bot_send_message,
                        send_audio=_english_bot_send_audio,
                        set_pending_answer=_english_bot_set_pending_answer,
                        send_report=_english_bot_send_report,
                        **generation,
                    )
                clear_prepared(state)
                mark_pushed_today(now, state)
                write_english_bot_state(settings.english_bot_state_path, state)
                source = "prepared" if prepared is not None else "live"
                log(f"[english_bot] pushed day={day_code} owners={owners} ({source})")
                if day_code == "sun" and settings.dictionary_enabled:
                    offer_weekly_quiz(owners)
                alerter.resolve("english-scheduler", "The English bot pushed today's task.")
            if should_sweep_today(now, settings.english_bot_sweep_time, state):
                swept = run_todays_sweep(qdrant, settings.tracker_collection, owners)
                mark_swept_today(now, state)
                write_english_bot_state(settings.english_bot_state_path, state)
                log(f"[english_bot] sweep done swept={swept}")
        except Exception:
            log(f"[english_bot] scheduler loop error: {traceback.format_exc()}")
            alerter.alert(
                "english-scheduler",
                "The English bot's scheduler hit an error; today's push or sweep may not have run.",
                traceback.format_exc(),
            )
        time.sleep(60)


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
    restore_pending_state()

    threading.Thread(target=_housekeeping_loop, daemon=True).start()

    if settings.night_ritual_enabled and settings.night_ritual_owner:
        threading.Thread(target=_night_ritual_loop, daemon=True).start()
        log(
            f"[night] scheduler started timezone={settings.night_ritual_timezone} "
            f"start={settings.night_ritual_start_time} days={settings.night_ritual_days}"
        )

    if settings.english_bot_enabled:
        english_bot_thread = threading.Thread(target=_english_bot_scheduler_loop, daemon=True)
        english_bot_thread.start()
        log(
            f"[english_bot] scheduler started timezone={settings.english_bot_timezone} "
            f"push={settings.english_bot_push_time} sweep={settings.english_bot_sweep_time} "
            f"owners={sorted(settings.english_bot_owners)}"
        )

    while RUNNING:
        try:
            updates = telegram(
                "getUpdates",
                {"offset": offset, "timeout": settings.telegram_poll_timeout, "allowed_updates": ["message", "callback_query"]},
                timeout=settings.telegram_poll_timeout + 10,
            )
            for update in updates.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                message = update.get("message")
                if message:
                    handle_message(message)
                elif update.get("callback_query"):
                    handle_callback_query(update["callback_query"])
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
