# Future TODO List

This document tracks post-v1.0 ideas and candidate work items for
`openclaw-arm-continuum`.

The v1.0 release is intentionally stable and text-first. The items below are
future-facing and should be implemented incrementally without breaking the
existing Telegram, memory, RAG, search, cron, and Gateway workflows.

## v1.1 Candidate: O6 Multimodal Skill With MNN Omni

Goal: add a local multimodal capability to Radxa Orion O6 while preserving the
current ERNIE + llama.cpp text runtime.

Recommended architecture:

```text
OpenClaw on O6
  |
  +-- text / RAG / search / cron -> ERNIE + llama.cpp
  |
  +-- image / multimodal analysis -> MNN Omni worker
```

The MNN Omni path should be implemented as a specialist skill, not as a
replacement for the main O6 text model.

### Implementation Path

Implement this capability in three stages:

```text
specialist skill -> registered agent -> TaskDispatcher integration
```

#### Stage 1: Specialist Skill

Goal: prove the capability works reliably before making the orchestration layer
more complex.

In this stage, add a direct skill such as `mnn_omni_analyze`:

```text
Telegram image
  -> save image to workspace
  -> call MNN Omni worker
  -> return image summary
```

Validation:

- The MNN Omni model can run on O6.
- Images can be passed into the worker.
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
runtime: o6-mnn-omni
backend: MNN
model: Qwen2.5-Omni-7B-MNN
capabilities:
  - image.describe
  - image.ocr
  - screenshot.explain
  - multimodal.summarize
limits:
  max_concurrency: 1
  timeout_seconds: 300
preferred_platform: o6
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

- Add `openclaw-mnn-omni` worker.
- Wrap MNN Omni inference behind a small local HTTP API.
- Add a new OpenClaw skill, for example `mnn_omni_analyze`.
- Route Telegram image uploads to the MNN Omni skill when enabled.
- Keep text chat, `/mem`, `/rag`, `/search`, and cron on the existing ERNIE
  endpoint.

### Proposed API

```text
GET  /health
POST /analyze-image
POST /analyze-audio
POST /analyze-multimodal
```

### Proposed Environment Variables

```env
OPENCLAW_MNN_OMNI_ENABLED=false
OPENCLAW_MNN_OMNI_BASE_URL=http://127.0.0.1:8790
OPENCLAW_MNN_OMNI_MODEL_DIR=/home/radxa/models/Qwen2.5-Omni-7B-MNN
OPENCLAW_MNN_OMNI_TIMEOUT=300
OPENCLAW_MNN_OMNI_MAX_IMAGE_SIZE=1280
```

`OPENCLAW_VISION_ENABLED=true` should only be enabled after the MNN Omni worker
passes health checks and image smoke tests.

### MVP Scope

- Image input only.
- Keep Whisper tiny for speech-to-text.
- Do not replace ERNIE as the O6 main text model.
- Use a single-flight queue or lock to avoid concurrent multimodal inference
  overloading the CPU-only host.
- Return clear timeout/error messages without blocking the Telegram runtime.

### MVP Validation

- `openclaw-mnn-omni /health` returns `200`.
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
multi-agent runtime while keeping the v1.0 skill architecture stable.

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

## Future: Platform Presets

Goal: make deployment easier across the Arm continuum.

Candidate profiles:

- DGX Spark / GB10 local GPU profile.
- Arm CPU-only profile.
- Arm host + remote local vLLM profile.
- O6 ERNIE + llama.cpp profile.
- O6 ERNIE + MNN Omni profile.

Expected work:

- Add clearer `.env` examples.
- Add platform-specific smoke tests.
- Add resource and performance notes.
- Keep secrets and runtime state out of git.
