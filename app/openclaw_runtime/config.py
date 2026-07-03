from dataclasses import dataclass
import os
from pathlib import Path


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    runtime_label: str

    telegram_bot_token: str
    telegram_allowed_chat_ids: set[int]
    telegram_poll_timeout: int
    max_reply_chars: int

    vllm_base_url: str
    vllm_model: str
    system_prompt: str
    max_tokens: int
    request_timeout: int

    web_enabled: bool
    web_timeout: int
    scraper_base_url: str
    scraper_limit: int
    skills_config_path: Path
    vision_enabled: bool
    vision_max_tokens: int
    whisper_enabled: bool
    whisper_base_url: str
    whisper_timeout: int

    memory_enabled: bool
    ollama_base_url: str
    embedding_model: str
    embedding_vector_size: int
    qdrant_base_url: str
    tracker_collection: str
    knowledge_collection: str
    retrieval_limit: int
    inbox_path: Path
    watcher_state_path: Path
    watcher_poll_seconds: int
    ingest_chunk_chars: int
    ingest_chunk_overlap: int

    cron_enabled: bool
    cron_timezone: str
    cron_daily_report_time: str
    cron_poll_seconds: int
    cron_run_on_start: bool
    cron_chat_ids: set[int]
    cron_tasks_config_path: Path
    cron_jobs_path: Path
    cron_state_path: Path
    task_history_path: Path
    gateway_rpc_url: str
    gateway_token: str
    gateway_state_db_path: Path


def load_settings() -> Settings:
    allowed_chat_ids = {
        int(item.strip())
        for item in os.environ.get("OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
        if item.strip()
    }
    cron_chat_ids = {
        int(item.strip())
        for item in os.environ.get("OPENCLAW_CRON_CHAT_IDS", "").split(",")
        if item.strip()
    }
    return Settings(
        runtime_label=os.environ.get("OPENCLAW_RUNTIME_LABEL", "OpenClaw local runtime").strip()
        or "OpenClaw local runtime",
        telegram_bot_token=os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_allowed_chat_ids=allowed_chat_ids,
        telegram_poll_timeout=env_int("OPENCLAW_POLL_TIMEOUT", 30),
        max_reply_chars=env_int("OPENCLAW_MAX_REPLY_CHARS", 3500),
        vllm_base_url=os.environ.get("OPENCLAW_VLLM_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/"),
        vllm_model=os.environ.get("OPENCLAW_VLLM_MODEL", "Qwen/Qwen3.6-27B-FP8"),
        system_prompt=os.environ.get(
            "OPENCLAW_SYSTEM_PROMPT",
            "你是 OpenClaw，運行在使用者本地 Arm Continuum 環境中的地端個人 AI 助理。回答時使用繁體中文，保持精準、務實、可操作。直接給最終答案，不要輸出推理過程。",
        ),
        max_tokens=env_int("OPENCLAW_MAX_TOKENS", 160),
        request_timeout=env_int("OPENCLAW_REQUEST_TIMEOUT", 60),
        web_enabled=env_bool("OPENCLAW_WEB_ENABLED", True),
        web_timeout=env_int("OPENCLAW_WEB_TIMEOUT", 20),
        scraper_base_url=os.environ.get("OPENCLAW_SCRAPER_BASE_URL", "http://openclaw-browser-scraper:8787").rstrip("/"),
        scraper_limit=env_int("OPENCLAW_SCRAPER_LIMIT", 3),
        skills_config_path=Path(os.environ.get("OPENCLAW_SKILLS_CONFIG", "/app/skills.json")),
        vision_enabled=env_bool("OPENCLAW_VISION_ENABLED", True),
        vision_max_tokens=env_int("OPENCLAW_VISION_MAX_TOKENS", 500),
        whisper_enabled=env_bool("OPENCLAW_WHISPER_ENABLED", True),
        whisper_base_url=os.environ.get("OPENCLAW_WHISPER_BASE_URL", "http://openclaw-whisper:8765").rstrip("/"),
        whisper_timeout=env_int("OPENCLAW_WHISPER_TIMEOUT", 180),
        memory_enabled=env_bool("OPENCLAW_MEMORY_ENABLED", True),
        ollama_base_url=os.environ.get("OPENCLAW_OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/"),
        embedding_model=os.environ.get("OPENCLAW_EMBEDDING_MODEL", "nomic-embed-text:latest"),
        embedding_vector_size=env_int("OPENCLAW_EMBEDDING_VECTOR_SIZE", 768),
        qdrant_base_url=os.environ.get("OPENCLAW_QDRANT_BASE_URL", "http://host.docker.internal:6333").rstrip("/"),
        tracker_collection=os.environ.get("OPENCLAW_TRACKER_COLLECTION", "personal_tracker_memory"),
        knowledge_collection=os.environ.get("OPENCLAW_KNOWLEDGE_COLLECTION", "personal_knowledge_base"),
        retrieval_limit=env_int("OPENCLAW_RETRIEVAL_LIMIT", 5),
        inbox_path=Path(os.environ.get("OPENCLAW_INBOX_PATH", "/workspace/inbox")),
        watcher_state_path=Path(os.environ.get("OPENCLAW_WATCHER_STATE_PATH", "/workspace/.openclaw/watcher_state.json")),
        watcher_poll_seconds=env_int("OPENCLAW_WATCHER_POLL_SECONDS", 10),
        ingest_chunk_chars=env_int("OPENCLAW_INGEST_CHUNK_CHARS", 1800),
        ingest_chunk_overlap=env_int("OPENCLAW_INGEST_CHUNK_OVERLAP", 200),
        cron_enabled=env_bool("OPENCLAW_CRON_ENABLED", True),
        cron_timezone=os.environ.get("OPENCLAW_CRON_TIMEZONE", "Europe/London"),
        cron_daily_report_time=os.environ.get("OPENCLAW_CRON_DAILY_REPORT_TIME", "07:00"),
        cron_poll_seconds=env_int("OPENCLAW_CRON_POLL_SECONDS", 30),
        cron_run_on_start=env_bool("OPENCLAW_CRON_RUN_ON_START", False),
        cron_chat_ids=cron_chat_ids,
        cron_tasks_config_path=Path(os.environ.get("OPENCLAW_CRON_TASKS_CONFIG", "/app/cron_tasks.json")),
        cron_jobs_path=Path(os.environ.get("OPENCLAW_CRON_JOBS_PATH", "/workspace/.openclaw/cron_jobs.json")),
        cron_state_path=Path(os.environ.get("OPENCLAW_CRON_STATE_PATH", "/workspace/.openclaw/cron_state.json")),
        task_history_path=Path(os.environ.get("OPENCLAW_TASK_HISTORY_PATH", "/workspace/.openclaw/task_history.jsonl")),
        gateway_rpc_url=os.environ.get("OPENCLAW_GATEWAY_RPC_URL", "http://openclaw-gateway:18789/api/v1/admin/rpc").rstrip("/"),
        gateway_token=os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip(),
        gateway_state_db_path=Path(os.environ.get("OPENCLAW_GATEWAY_STATE_DB", "/gateway-state/openclaw.sqlite")),
    )
