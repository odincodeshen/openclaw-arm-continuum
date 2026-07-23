# Deployment

OpenClaw currently ships with the stable `dgx-spark` deployment path and an
experimental `arm-cpu-only` path verified on Radxa Orion O6 with Baidu ERNIE 4.5
served through llama.cpp. See `docs/PLATFORMS.md` for the platform roadmap.

## 1. Install Host Software

Install and verify the host software before configuring OpenClaw:

- Linux workstation with an NVIDIA GPU.
- NVIDIA driver compatible with the GPU.
- NVIDIA Container Toolkit.
- Docker Engine.
- Docker Compose plugin.
- Git and curl.
- Ollama.
- Qdrant.
- Telegram bot created through BotFather.

Optional but useful:

- SSH server for remote access and Gateway dashboard tunneling.
- Hugging Face CLI/login if your selected vLLM model is gated.
- Open WebUI if you also want a separate local web chat UI.

## 2. Prepare Host Services

Ollama should expose:

```text
http://127.0.0.1:11434
```

Qdrant should expose:

```text
http://127.0.0.1:6333
```

Pull an embedding model:

```bash
ollama pull nomic-embed-text
```

Qdrant should have persistent storage configured according to your host preference. OpenClaw will create the configured collections when it writes memory/RAG content.

The vLLM service is started by `compose.yaml`. If you change model size or hardware, review these `.env` fields:

```text
OPENCLAW_VLLM_MODEL=
OPENCLAW_GPU_MEMORY_UTILIZATION=
OPENCLAW_MAX_MODEL_LEN=
OPENCLAW_MAX_NUM_SEQS=
HF_CACHE_DIR=
```

## 3. Configure OpenClaw

```bash
cp .env.example .env
```

Minimum personal settings for an already prepared host:

```text
OPENCLAW_TELEGRAM_BOT_TOKEN=
OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS=
OPENCLAW_CRON_CHAT_IDS=
OPENCLAW_GATEWAY_TOKEN=
```

These four values configure Telegram access and Gateway dashboard auth. They do not install Docker, GPU runtime, Ollama, Qdrant, or model files.

Set an IANA timezone for scheduled jobs. The runtime uses UTC when this value is omitted:

```text
OPENCLAW_CRON_TIMEZONE=Asia/Singapore
```

Weather questions should include a location. To support questions that omit it, configure an optional fallback:

```text
OPENCLAW_DEFAULT_WEATHER_LOCATION=Singapore
```

## 4. Start

```bash
docker compose --env-file .env -f compose.yaml up -d
```

## 5. Verify

```bash
docker compose --env-file .env -f compose.yaml ps
curl http://127.0.0.1:8000/v1/models
curl -I http://127.0.0.1:18789/
```

Telegram smoke test:

```text
/help
/mem #test OpenClaw local memory smoke test
/rag memory: OpenClaw local memory smoke test
```
