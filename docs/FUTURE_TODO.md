# Future TODO List

This document tracks post-v1.1 ideas and candidate work items for
`openclaw-arm-continuum`.

The v1.1 release is intentionally stable and text-first. The items below are
future-facing and should be implemented incrementally without breaking the
existing Telegram, memory, RAG, search, cron, and Gateway workflows.

## v2.0 Candidate: Platform-Aware MultimodalAnalysisAgent

Goal: add a reusable multimodal analysis agent that can select the best local
backend for each Arm Continuum deployment profile.

Recommended architecture:

```text
MultimodalAnalysisAgent
  |
  +-- DGX / GB10 backend -> vLLM + formal VLM
  |
  +-- Arm CPU backend -> MNN Omni
  |
  +-- Arm gateway backend -> remote local VLM
```

MNN Omni should be treated as an Arm CPU-friendly multimodal backend, not as an
O6-only feature and not as the primary DGX / GB10 multimodal path.

Recommended backend strategy:

- DGX / GB10: use `vllm-vlm` with a formal VLM such as Qwen2.5-VL, Qwen3-VL, or
  a Llama Vision family model.
- Arm CPU-only: use `mnn-omni` as a local CPU-friendly multimodal backend.
- Arm gateway / edge host: use `remote-vlm` to route multimodal tasks to a
  trusted local GB10 / DGX VLM endpoint on the private network.

Example platform settings:

```env
# Common switch
OPENCLAW_VISION_ENABLED=true
OPENCLAW_MULTIMODAL_BACKEND=mnn-omni

# Arm CPU-only, for example Orion O6
OPENCLAW_MULTIMODAL_BACKEND=mnn-omni
OPENCLAW_MNN_OMNI_BASE_URL=http://127.0.0.1:8790

# DGX / GB10
OPENCLAW_MULTIMODAL_BACKEND=vllm-vlm
OPENCLAW_VLLM_BASE_URL=http://openclaw-vllm:8000/v1
OPENCLAW_VLLM_MODEL=Qwen3-VL...

# Arm gateway routing to local GB10 / DGX VLM
OPENCLAW_MULTIMODAL_BACKEND=remote-vlm
OPENCLAW_REMOTE_VLM_BASE_URL=http://gb10.local:8000/v1
```

### Implementation Path

Implement this capability in three stages:

```text
specialist skill -> registered agent -> TaskDispatcher integration
```

#### Stage 1: Specialist Skill

Goal: prove the capability works reliably before making the orchestration layer
more complex.

In this stage, add explicit backend skills, starting with a direct skill such
as `mnn_omni_analyze` for Arm CPU-only platforms:

```text
Telegram image
  -> save image to workspace
  -> call selected multimodal backend
  -> return image summary
```

Validation:

- The selected backend can run on its target platform.
- Images can be passed into the backend worker.
- Timeouts and errors are handled clearly.
- Telegram remains responsive.
- Existing `/mem`, `/rag`, `/search`, and cron flows are not affected.

#### Stage 2: Registered Agent

Goal: turn the working skill into a first-class OpenClaw agent with explicit
identity, capabilities, and limits.

Proposed agent metadata:

```yaml
agent_id: multimodal_analysis
name: Multimodal Analysis Agent
capabilities:
  - image.describe
  - image.ocr
  - screenshot.explain
  - multimodal.summarize
backends:
  - id: vllm-vlm
    preferred_platforms:
      - dgx-spark
      - gb10
    runtime: vllm
    model_family: vlm
  - id: mnn-omni
    preferred_platforms:
      - arm-cpu-only
      - orion-o6
    runtime: mnn
    model: Qwen2.5-Omni-7B-MNN
    limits:
      max_concurrency: 1
      timeout_seconds: 300
  - id: remote-vlm
    preferred_platforms:
      - arm-gateway
      - rpi5
    runtime: openai-compatible
    target: trusted-local-vlm
```

Validation:

- AgentRegistry can list the agent and its capabilities.
- Health checks expose whether the agent is available.
- Task history can identify which agent handled the image.
- The system can keep deterministic command routes while still understanding
  this agent as a reusable capability.

#### Stage 3: TaskDispatcher Integration

Goal: let OpenClaw automatically choose the multimodal agent when the task
requires it.

Example route:

```text
User uploads a screenshot and asks what is wrong
  -> TaskDispatcher detects image input + troubleshooting intent
  -> dispatch to MultimodalAnalysisAgent
  -> optionally pass summary to RAG Agent
  -> final response through Chat Agent
```

Validation:

- Image, screenshot, and mixed text/image prompts route to the multimodal agent.
- Text-only `/mem`, `/rag`, `/search`, `/doc`, and `/cron` commands remain
  deterministic.
- Dispatcher failures fall back to explicit skill routes instead of blocking
  the Telegram runtime.
- Logs show the selected agent, reason, status, and runtime duration.

### Proposed Components

- Add backend-specific multimodal workers where needed.
- Add `openclaw-mnn-omni` worker for Arm CPU-only platforms.
- Add `vllm-vlm` backend support for DGX / GB10 formal VLM deployments.
- Add `remote-vlm` backend support for Arm gateway devices that route to a
  trusted local VLM endpoint.
- Wrap each backend behind a consistent local HTTP/API contract.
- Add a new OpenClaw skill, for example `multimodal_analyze`, with
  backend-specific adapters.
- Route Telegram image uploads to `MultimodalAnalysisAgent` when enabled.
- Keep text chat, `/mem`, `/rag`, `/search`, and cron on the existing text
  runtime unless the task explicitly requires multimodal analysis.

### Proposed API

```text
GET  /health
POST /analyze-image
POST /analyze-audio
POST /analyze-multimodal
```

### Proposed Environment Variables

```env
OPENCLAW_VISION_ENABLED=false
OPENCLAW_MULTIMODAL_BACKEND=mnn-omni

OPENCLAW_MNN_OMNI_ENABLED=false
OPENCLAW_MNN_OMNI_BASE_URL=http://127.0.0.1:8790
OPENCLAW_MNN_OMNI_MODEL_DIR=/home/radxa/models/Qwen2.5-Omni-7B-MNN
OPENCLAW_MNN_OMNI_TIMEOUT=300
OPENCLAW_MNN_OMNI_MAX_IMAGE_SIZE=1280

OPENCLAW_REMOTE_VLM_BASE_URL=
OPENCLAW_REMOTE_VLM_MODEL=
```

`OPENCLAW_VISION_ENABLED=true` should only be enabled after the selected
multimodal backend passes health checks and image smoke tests.

### MVP Scope

- Image input only for the first implementation.
- Start with one backend, likely `mnn-omni` on Arm CPU-only or `vllm-vlm` on
  GB10 depending on available hardware and model readiness.
- Keep Whisper tiny for speech-to-text on CPU-only platforms until direct
  multimodal audio is explicitly validated.
- Do not replace the platform's main text model.
- Use a single-flight queue or lock to avoid concurrent multimodal inference
  overloading CPU-only hosts.
- Return clear timeout/error messages without blocking the Telegram runtime.

### MVP Validation

- The selected backend health endpoint returns `200`.
- A Telegram image upload produces a useful local image summary.
- The response includes:
  - main visual content
  - visible text/OCR summary when possible
  - 1-3 suggested follow-up actions or questions
- `/mem` still writes memory successfully.
- `/rag` still retrieves context successfully.
- `/search` still works.
- Cron jobs still run and push results.

## Future: GB10 Formal VLM Runtime

Goal: replace the current text-first GB10 model with a production-grade VLM for
image, PDF, screenshot, and diagram reasoning.

Candidate model families:

- Qwen2.5-VL
- Qwen3-VL
- Llama Vision family

Expected work:

- Update vLLM model configuration.
- Verify OpenAI-compatible multimodal request formatting.
- Add image/PDF smoke tests.
- Update `/help` and platform docs.

## Future: Richer Multi-Agent Runtime

Goal: evolve the current thin AgentRegistry / TaskDispatcher into a richer
multi-agent runtime while keeping the v1.1 skill architecture stable.

Candidate agents:

- Telegram Gateway Agent
- Semantic Memory Agent
- Browser Scraper Agent
- Document Review Agent
- Cron Scheduler Agent
- Multimodal Analysis Agent

Expected work:

- Add explicit agent capability metadata.
- Add task routing policies.
- Add task history and failure reporting.
- Keep deterministic routes for commands such as `/mem`, `/rag`, `/doc`,
  `/search`, and `/cron`.

## Future: Personal Memory Deepening

Goal: make OpenClaw more useful as a long-running personal AI system.

Candidate work:

- Profile show/set flows.
- Task deadline review.
- Todo completion and snooze controls.
- Better metadata parsing for `/mem`.
- More precise `/rag` scope filters.
- Memory aging, archival, and explicit user review.

## Future: Runtime Lifecycle and Resource Control

Goal: keep OpenClaw useful even when the main model engine is stopped for other
projects, especially on shared GB10 / DGX GPU workstations and Arm CPU-only
hosts.

Core design principle:

```text
OpenClaw core != model engine
```

Recommended split:

- Always-on core:
  - Telegram gateway
  - Gateway dashboard
  - Cron worker
  - Qdrant
  - Ollama embedding service
  - Browser scraper
  - Memory watcher
- Optional model engine:
  - GB10 / DGX: vLLM
  - Arm CPU-only: llama.cpp / ERNIE server
  - Arm gateway: trusted remote local vLLM endpoint

Expected behavior:

- OpenClaw core can stay online while vLLM or llama.cpp is stopped.
- Telegram should return a clear message when the model engine is paused.
- Memory, dashboard, cron schedule management, document intake, and scraper
  health checks should remain available when possible.
- Restarting the model engine should not require recreating all OpenClaw
  services.

Proposed boot modes:

```env
OPENCLAW_BOOT_MODE=core
OPENCLAW_BOOT_MODE=full
OPENCLAW_BOOT_MODE=manual
```

Mode semantics:

- `core`: start OpenClaw core services only.
- `full`: start core services and the local model engine.
- `manual`: do not auto-start OpenClaw services.

Recommended platform defaults:

- GB10 / DGX: default to `core` so GPU resources remain easy to reclaim for
  other projects.
- O6 / Arm CPU-only: default to `full` when used as a small always-on assistant,
  or `core` when CPU/RAM must be shared with other workloads.
- Arm gateway + remote local vLLM: default to `core`, because the gateway should
  not own the remote model server lifecycle.

Proposed CLI:

```bash
openclawctl status
openclawctl start core
openclawctl stop core
openclawctl restart core

openclawctl start model
openclawctl stop model
openclawctl restart model
openclawctl status model

openclawctl start full
openclawctl stop full
```

Platform-specific model actions:

- GB10 / DGX:
  - `openclawctl stop model` stops `openclaw-vllm`.
  - `openclawctl start model` starts `openclaw-vllm`.
- O6 / Arm CPU-only:
  - `openclawctl stop model` stops `openclaw-ernie-llama.service`.
  - `openclawctl start model` starts `openclaw-ernie-llama.service`.
- Arm gateway:
  - `openclawctl status model` checks the remote local vLLM endpoint.
  - start/stop may be disabled unless the gateway has explicit permission.

Validation:

- `openclawctl status` clearly distinguishes core status and model status.
- Stopping the model releases GPU/CPU memory without stopping Telegram.
- Telegram responds clearly when the model engine is paused.
- `/help`, `/cron`, document intake, and dashboard access still work with core
  services only.
- Starting the model again restores normal chat, `/rag`, and `/search`
  summarization without rebuilding the whole stack.
- Reboot behavior follows `OPENCLAW_BOOT_MODE`.

## Future: Platform Presets

Goal: make deployment easier across the Arm continuum.

Candidate profiles:

- DGX Spark / GB10 local GPU profile.
- Arm CPU-only profile.
- Arm host + remote local vLLM profile.
- O6 ERNIE + llama.cpp profile.
- Arm CPU-only + MNN Omni multimodal profile.
- Arm gateway + remote VLM profile.

Expected work:

- Add clearer `.env` examples.
- Add platform-specific smoke tests.
- Add resource and performance notes.
- Keep secrets and runtime state out of git.
