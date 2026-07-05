# OpenClaw Arm Continuum

Version: `v1.1`

License: Apache-2.0

[繁體中文 README](README.zh-TW.md)

Local-first personal AI runtime for Arm, from edge to server.

OpenClaw Arm Continuum is built on a simple idea: personal AI should not be tied to one machine class. On Arm, the same local-first assistant runtime can live across a continuum, from low-power edge gateways, to CPU-only Arm servers, to DGX Spark-class AI workstations.

Each tier offers a different balance of privacy, power efficiency, and inference capability, while keeping the user's data inside a local host or trusted private LAN.

## What This Release Includes

- Local vLLM inference through an OpenAI-compatible endpoint.
- Telegram long polling gateway with allowlist support.
- Local memory and document RAG through Ollama embeddings and Qdrant.
- Playwright/Chromium scraper worker for `/search` and cron web tasks.
- Local Whisper service for Telegram voice transcription.
- Dynamic cron tasks with Telegram push delivery and Gateway dashboard integration.
- Thin `AgentRegistry` and `TaskDispatcher` for skill-based routing.

## Deployment Profiles

| Profile | Status | Inference | Target hardware |
|---|---|---|---|
| `dgx-spark` | Stable | Local vLLM on NVIDIA GPU | DGX Spark / GB10 class workstation. |
| `arm-cpu-only` | Experimental | Local CPU LLM through llama.cpp or another OpenAI-compatible local endpoint | Radxa Orion O6 or similar Armv9 server-class board. |
| `arm-remote-llm` | Planned | Runtime on local Arm host, inference on private LAN vLLM server | RPi5 / small Arm gateway plus local Arm/GPU inference server. |

The immediate next hardware target after DGX Spark is `arm-cpu-only` on Radxa Orion O6, using Baidu ERNIE 4.5 models and llama.cpp for better CPU inference efficiency. See `docs/PLATFORMS.md` and `docs/ERNIE_LLAMA_CPP.md`.

## Why Arm Continuum

Arm gives OpenClaw a flexible deployment continuum:

- Edge devices can stay always-on as low-power Telegram, cron, memory, and RAG gateways.
- CPU-only Arm servers can run small local LLMs efficiently for private lightweight assistance.
- DGX Spark / GB10 class workstations can run larger local models with high-throughput vLLM.
- Private LAN inference servers can be shared by multiple Arm edge gateways without using public cloud APIs.

Same runtime, different capability tiers.

## Prerequisites

For the stable `dgx-spark` profile, this project expects a Linux workstation with NVIDIA GPU support. The four Telegram/Gateway settings in `.env` are usually enough only after the host software below is already installed and working.

Required host software:

- NVIDIA driver compatible with your GPU.
- NVIDIA Container Toolkit for Docker GPU passthrough.
- Docker Engine with the Docker Compose plugin.
- Git, curl, and a shell environment such as bash.
- Ollama, running on the host at `127.0.0.1:11434`.
- Qdrant, running on the host at `127.0.0.1:6333`.
- A Telegram bot token from BotFather.
- Enough disk space for model weights, Docker images, and local workspace data.

Required model/service setup:

- vLLM is started by this compose stack.
- Ollama must have an embedding model pulled, for example `nomic-embed-text`.
- The selected vLLM model must be downloadable by the host. If the model is gated, configure Hugging Face access before starting the stack.

The compose file binds vLLM and the Gateway dashboard to `127.0.0.1` by default.

## Quick Start

```bash
cp .env.example .env
```

Edit `.env`:

```text
OPENCLAW_TELEGRAM_BOT_TOKEN=<your-telegram-bot-token>
OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS=<your-chat-id>
OPENCLAW_CRON_CHAT_IDS=<your-chat-id>
OPENCLAW_GATEWAY_TOKEN=<generate-a-long-random-token>
```

These are the minimum personal settings for an already prepared host. Different GPU, memory, model, or cache setups may also need:

```text
OPENCLAW_VLLM_MODEL=
OPENCLAW_GPU_MEMORY_UTILIZATION=
OPENCLAW_MAX_MODEL_LEN=
OPENCLAW_MAX_NUM_SEQS=
HF_CACHE_DIR=
```

If Ollama or Qdrant run as containers on the same Docker network instead of
host-loopback services, set these in `.env` to match your topology:

```text
OPENCLAW_OLLAMA_BASE_URL=http://ollama:11434
OPENCLAW_QDRANT_BASE_URL=http://qdrant:6333
```

The DGX compose stack talks to its bundled vLLM service through
`http://openclaw-vllm:8000/v1` by default. Set `OPENCLAW_VLLM_BASE_URL` only
when routing to an external or remote OpenAI-compatible endpoint.

Start the stack:

```bash
docker compose --env-file .env -f compose.yaml up -d
```

For the experimental Radxa Orion O6 CPU-only profile, use:

```bash
cp .env.arm-cpu-only.example .env
docker compose --env-file .env -f compose.arm-cpu-only.yaml up -d
```

The O6 profile expects a local OpenAI-compatible llama.cpp server at `127.0.0.1:8080`. See `docs/ERNIE_LLAMA_CPP.md`.

Check containers:

```bash
docker compose --env-file .env -f compose.yaml ps
docker logs --tail 80 openclaw-telegram
docker logs --tail 80 openclaw-cron
```

Check local endpoints from the host:

```bash
curl http://127.0.0.1:8000/v1/models
curl -I http://127.0.0.1:18789/
```

## Telegram Commands

Use `/help` in Telegram for the mobile command card.

Main commands:

```text
/mem      capture personal memory
/rag      retrieve memory or document context
/doc      import public Google Docs
/search   browse with local Playwright worker
/cron     create and manage proactive push tasks
/agents   list the active local agents
/tasks    inspect recent task history
```

Currently implemented help commands:

```text
/help
/doc
/cron
```

Weather is not a slash command. Ask in plain language and it is routed to
the local weather skill automatically, for example `Cambridge weather today`.
Do not prefix a weather question with `/search`: an explicit `/search`
always wins the routing priority, so `/search Cambridge weather today` runs
a general web search instead of the purpose-built weather lookup.

## Memory And RAG

Qdrant uses two primary collections:

| Collection | Telegram scope | Purpose |
|---|---|---|
| `personal_tracker_memory` | `memory:` | Dynamic personal memory, todos, tracker files, scraped pages, short-term context. |
| `personal_knowledge_base` | `knowledge:` | Long-term documents, manuals, white papers, formally ingested files. |

Examples:

```text
/mem #todo due:2026-07-05 Check the cron dashboard
/rag memory: What OpenClaw settings did I ask you to remember?
/rag knowledge: Summarize the uploaded architecture manual
/rag doc:260702_01 What is this file about?
```

## Document Intake

Supported Telegram uploads:

```text
.pdf .md .txt .log .json .csv .tsv
```

Current upload flow:

1. Telegram upload is saved into the local inbox.
2. `openclaw-memory-watcher` indexes supported files into Qdrant.
3. Query the indexed content with `/rag`.

Commands:

```text
/doc url https://docs.google.com/document/d/.../edit
/doc url https://docs.google.com/document/d/.../edit tracker
```

Public Google Docs can be imported with `/doc url`. The default target is `knowledge`; append `tracker` for dynamic tracker memory.

Caption shortcuts:

```text
/rag        ingest uploaded file directly into knowledge
/knowledge  ingest uploaded file directly into knowledge
/mem        ingest uploaded file directly into tracker memory
/tracker    ingest uploaded file directly into tracker memory
```

The review-first document workflow is planned, but not yet implemented in the
Telegram gateway. Current file uploads are indexed directly after they are
saved into the local inbox.

## Cron

Create dynamic Telegram cron tasks with `name :: prompt` (the `::` separator
is required):

```text
/cron add daily 07:30 Morning weather :: Cambridge weather today
/cron add weekly mon 08:00 AI roundup :: Summarize this week's AI hardware news
/cron add monthly 1 09:00 Monthly review :: Review monthly personal goals
/cron list
/cron run <job_id>
/cron delete <job_id>
```

The prompt after `::` is routed exactly like a normal chat message: a plain
weather question (no `/search` prefix) goes to the weather skill, and
`/search <query>` forces a general web search instead.

Daily, weekly, and monthly fixed-time jobs run only inside their configured execution window, so restarting a container later in the day will not backfill a missed morning job.

## Gateway Dashboard

The official OpenClaw Gateway dashboard is bound to localhost:

```text
http://127.0.0.1:18789/
```

If the workstation is remote, create an SSH tunnel from your laptop:

```bash
ssh -L 18789:127.0.0.1:18789 <user>@<gb10-host>
```

Then open:

```text
http://127.0.0.1:18789/
```

Use `OPENCLAW_GATEWAY_TOKEN` from your private `.env` when prompted.

The compose profiles enable the bundled `admin-http-rpc` Gateway plugin before
starting the Gateway. OpenClaw uses this private operator RPC surface for
`cron.list`, `cron.add`, `cron.update`, `cron.remove`, and run-history sync.
Keep the Gateway behind localhost, SSH tunneling, or a trusted private network;
do not expose the admin RPC route directly to the public internet.

## Security Defaults

- Do not commit `.env`.
- Do not commit `workspace/`, `gateway-data/`, Qdrant snapshots, SQLite files, logs, or backups.
- Keep Telegram allowlist enabled with `OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS`.
- Keep vLLM, Qdrant, Ollama, and Gateway bound to localhost unless you know exactly why LAN exposure is required.
- Use SSH tunnels for dashboard access.

See `docs/SECURITY.md` for the publication checklist.

## Roadmap

The current stable baseline is:

```text
dgx-spark / GB10 + local vLLM
```

Next stage:

```text
Radxa Orion O6 + Baidu ERNIE 4.5 + llama.cpp
```

Future profile:

```text
RPi5 / Arm gateway + private LAN vLLM
```

Formal photo/PDF/diagram parsing should move to an explicit VLM in the future, such as Qwen2.5-VL or Qwen3-VL.

## License

This project is licensed under the Apache License, Version 2.0. See `LICENSE`.

## Production Notes

The current default model is `Qwen/Qwen3.6-27B-FP8`. Photo analysis can be wired through the same local vLLM API, but a production VLM such as Qwen2.5-VL or Qwen3-VL is recommended for formal image/PDF/diagram understanding.

Fuller multi-agent decomposition is intentionally left for later. v1.1 uses a thin dispatcher so the runtime remains easy to inspect, clone, and operate.
