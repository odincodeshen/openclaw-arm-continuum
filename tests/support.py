from pathlib import Path

from openclaw_runtime.config import Settings

UNREACHABLE_URL = "http://127.0.0.1:1"


def build_settings(**overrides) -> Settings:
    base = dict(
        runtime_label="test",
        telegram_bot_token="t",
        telegram_allowed_chat_ids=set(),
        telegram_poll_timeout=30,
        max_reply_chars=3500,
        vllm_base_url=UNREACHABLE_URL,
        vllm_model="x",
        system_prompt="x",
        max_tokens=1,
        request_timeout=1,
        web_enabled=True,
        web_timeout=1,
        scraper_base_url=UNREACHABLE_URL,
        scraper_limit=1,
        web_context_chars=1,
        default_weather_location="",
        skills_config_path=Path("skills.json"),
        vision_enabled=False,
        vision_max_tokens=1,
        whisper_enabled=False,
        whisper_base_url="http://x",
        whisper_timeout=1,
        memory_enabled=True,
        ollama_base_url="http://x",
        embedding_model="x",
        embedding_vector_size=1,
        qdrant_base_url=UNREACHABLE_URL,
        tracker_collection="tracker_coll",
        knowledge_collection="knowledge_coll",
        retrieval_limit=1,
        inbox_path=Path("inbox"),
        watcher_state_path=Path("watcher_state.json"),
        watcher_poll_seconds=1,
        ingest_chunk_chars=100,
        ingest_chunk_overlap=10,
        cron_enabled=False,
        cron_timezone="UTC",
        cron_daily_report_time="07:00",
        cron_poll_seconds=30,
        cron_due_window_minutes=15,
        cron_run_on_start=False,
        cron_chat_ids=set(),
        cron_tasks_config_path=Path("cron_tasks.json"),
        cron_jobs_path=Path("cron_jobs.json"),
        cron_state_path=Path("cron_state.json"),
        task_history_path=Path("task_history.jsonl"),
        gateway_rpc_url="http://x",
        gateway_token="x",
        gateway_state_db_path=Path("openclaw.sqlite"),
    )
    base.update(overrides)
    return Settings(**base)
