#!/usr/bin/env python3
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
from datetime import datetime, timezone

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
from openclaw_runtime.file_ingest import META_SIDECAR_SUFFIX, SUPPORTED_SUFFIXES
from openclaw_runtime.engineering_review import EngineeringReviewAgent
from openclaw_runtime.http_client import request_json
from openclaw_runtime.llm_client import VLLM_NOT_READY_MESSAGE
from openclaw_runtime.model_catalog import load_model_registry
from openclaw_runtime.model_client_factory import ModelClientFactory
from openclaw_runtime.qdrant_client import QdrantClient
from openclaw_runtime.skill_router import SkillRouter
from openclaw_runtime.source_ingest import save_google_doc
from openclaw_runtime.task_history import TaskHistory
from openclaw_runtime.transcription_client import TranscriptionClient
from openclaw_runtime.vision_client import DEFAULT_DESCRIBE_INSTRUCTION, VisionClient, VisionError


settings = load_settings()
model_registry = load_model_registry(settings)
model_clients = ModelClientFactory(settings, model_registry)
llm = model_clients.get("local_default")
vision = VisionClient(model_clients.get_or_default("vision"))
qdrant = QdrantClient(settings)
transcriber = TranscriptionClient(settings)
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
- send the file first, then reply with the category name.
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
        return len(pending["items"])


def pop_pending_category(chat_id: int) -> dict | None:
    with PENDING_CATEGORY_LOCK:
        return PENDING_CATEGORY.pop(chat_id, None)


def has_pending_category(chat_id: int) -> bool:
    with PENDING_CATEGORY_LOCK:
        return chat_id in PENDING_CATEGORY


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
    for chat_id, pending in expired:
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
        send_message(chat_id, f"That category name will not work: {exc}. Send another name, or /cancel.")
        return True

    entry = upsert_registry_entry(settings, display)
    worker = threading.Thread(
        target=_run_category_ingest,
        args=(chat_id, pending["items"], entry),
        daemon=True,
    )
    worker.start()
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
    send_message(
        chat_id,
        "To also index this image for retrieval, reply with just the category "
        "name, e.g. trip (or /cancel to skip). The analysis above is sent regardless.",
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
    send_message(
        chat_id,
        f"To also index {'this image' if len(paths) == 1 else f'these {len(paths)} images'} for "
        "retrieval, reply with just the category name, e.g. trip (or /cancel to skip). "
        "The analysis above is sent regardless.",
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
        "- send the file first, then reply with the category name.\n\n"
        "/cat list   Show categories and their document counts\n"
        "/cat rename <old> <new>   Rename a category (its files/index are untouched)\n"
        "/cat merge <source> <target>   Move everything from source into target, "
        "then delete source\n"
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
                "Usage: /cat rename <old name> <new name> "
                "(use [brackets] for a multi-word name, e.g. /cat rename trip [Work Notes])",
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

    send_message(chat_id, category_command_text())
    return True


def _merge_category_files(from_slug: str, into_entry: dict) -> int:
    """Move every file from one category's inbox directory into another's,
    updating each .meta.json sidecar to point at the new category. The memory
    watcher re-indexes moved files at its next poll -- merge does not touch
    Qdrant data directly, so there is only ever one ingest path to reason
    about."""
    from_dir = category_dir(from_slug)
    if not from_dir.is_dir():
        return 0
    into_dir = category_dir(into_entry["slug"])
    into_media = into_dir / "media"
    moved = 0
    for md_path in sorted(from_dir.glob("*.md")):
        meta_path = md_path.with_name(md_path.name + META_SIDECAR_SUFFIX)
        target_md = unique_path(into_dir, md_path.name)
        shutil.move(str(md_path), str(target_md))

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
        _write_meta_sidecar(target_md, data)
        moved += 1
    shutil.rmtree(from_dir, ignore_errors=True)
    return moved


def _run_category_merge(chat_id: int, from_entry: dict, into_entry: dict) -> None:
    try:
        moved_files = _merge_category_files(from_entry["slug"], into_entry)
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
        f"into={into_entry['slug']} files={moved_files}"
    )
    if moved_files:
        send_message(
            chat_id,
            f'Merged "{from_entry["display"]}" into "{into_entry["display"]}": {moved_files} file(s). '
            "Give the indexer ~10 seconds to catch up, then query with "
            f'/rag #{into_entry["display"]}.',
        )
    else:
        send_message(
            chat_id,
            f'"{from_entry["display"]}" had no files to move. "{into_entry["display"]}" is unchanged; '
            f'"{from_entry["display"]}" no longer exists.',
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
            send_message(
                chat_id,
                f"Got {staged.name}. Which knowledge category should it go in?\n"
                "Reply with just the category name, e.g. trip (no # and no extra "
                "note -- for that, use the #<name> note caption instead), or "
                "/cancel to file it into the general knowledge base.",
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

    if settings.category_rag_enabled:
        try:
            sweep_expired_pending()
        except Exception as exc:  # noqa: BLE001 - never let housekeeping drop a message
            log(f"[category] sweep error: {exc}")

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
        if text.lower() in {"/cancel", "/skip"}:
            dropped = pop_pending_category(chat_id)
            if dropped:
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
