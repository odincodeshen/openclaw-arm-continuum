# GitHub Repository Content

## Repository Name

```text
openclaw-arm-continuum
```

## Short Description

```text
Local-first personal AI runtime for Arm, from edge to server.
```

## Longer Description

```text
OpenClaw deployment continuum for local-first personal AI across Arm edge devices, CPU-only Arm servers, and DGX Spark-class AI workstations.
```

## Topics

```text
openclaw
arm
local-ai
personal-ai
edge-ai
arm64
dgx-spark
vllm
llama-cpp
qdrant
ollama
telegram-bot
rag
privacy
self-hosted
```

## One-Paragraph Project Intro

OpenClaw Arm Continuum explores how one local-first personal AI runtime can span Arm edge devices, CPU-only Arm servers, and DGX Spark-class AI workstations. The same assistant runtime can run Telegram control, cron automation, memory, RAG, document intake, browser search, and Gateway dashboard services, while routing inference to the best available local or trusted LAN model endpoint.

## Chinese Project Intro

OpenClaw Arm Continuum 是一套以 Arm 架構為核心、強調地端優先、隱私優先、彈性部署的個人 AI 助理執行環境。它不是只針對單一硬體，而是把 OpenClaw 放在一條從 edge 到 server 的 Arm 部署連續帶上：低功耗 edge device 可以做常駐入口，Arm CPU server 可以執行輕量本地模型，DGX Spark / GB10 類工作站可以承擔高吞吐本地推理，而內網 vLLM server 可以成為共享的私有推理資源。

## Suggested Initial Release Title (v1.0)

```text
OpenClaw Arm Continuum v1.0 - DGX Spark stable baseline
```

## Suggested Initial Release Notes (v1.0)

```text
v1.0 establishes the DGX Spark / GB10 stable baseline for OpenClaw Arm Continuum.

Included:
- Telegram-first personal AI interface
- Local vLLM inference on DGX Spark
- Ollama embeddings and Qdrant-backed memory/RAG
- Playwright scraper worker
- Local Whisper voice transcription
- Dynamic cron push tasks
- Gateway dashboard integration
- Thin AgentRegistry / TaskDispatcher runtime

Roadmap:
- Radxa Orion O6 + ERNIE 4.5 + llama.cpp CPU-only profile
- Arm gateway + private LAN vLLM profile
- Production VLM model profile
```

## Suggested Release Title (v1.1)

```text
OpenClaw Arm Continuum v1.1 - Correctness and hardening pass
```

## Suggested Release Notes (v1.1)

```text
v1.1 is a correctness and hardening pass over the v1.0 DGX Spark baseline. No
new user-facing features; focus is on fixing bugs found during a full
README-vs-implementation audit and adding regression coverage so they stay
fixed.

Fixed:
- Cron daily/weekly/monthly jobs no longer backfill a missed run when a
  container restarts later in the day (new due-window guard, configurable via
  OPENCLAW_CRON_DUE_WINDOW_MINUTES).
- Gateway-synced dynamic cron jobs now use the configured OPENCLAW_CRON_TIMEZONE
  instead of a hardcoded Europe/London.
- Telegram document uploads captioned /mem now route to tracker memory as
  documented, instead of silently landing in knowledge.
- arm-cpu-only Gateway now binds to loopback only and requires
  OPENCLAW_GATEWAY_TOKEN to be set, matching the DGX profile's security
  posture instead of relying on network_mode: host to expose it LAN-wide.
- /agents now reports each agent's real dependency health (vLLM, Qdrant,
  browser scraper) instead of always showing "ready".

Improved:
- Memory watcher skips a full SHA256 hash for files whose mtime/size are
  unchanged, cutting continuous CPU/disk I/O on CPU-only Arm hosts.
- Browser scraper worker reuses a persistent Chromium instance across
  requests instead of launching a new browser per request.
- README.md and README.zh-TW.md brought back into parity and corrected to
  match actual behavior.
- Added regression tests for every fix above.

Roadmap items originally slated for v1.1 (platform-aware multimodal analysis
agent, richer multi-agent runtime, personal memory deepening, runtime
lifecycle control, platform presets) move to v2.0. See
docs/FUTURE_TODO.md.
```

## Suggested Release Title (v1.4)

```text
OpenClaw Arm Continuum v1.4 - Category RAG and source attribution
```

## Suggested Release Notes (v1.4)

```text
v1.4 adds per-category knowledge bases, decouples image analysis from the
model, and makes every /rag answer cite its sources. Text-only flows are
unchanged; the feature is gated by OPENCLAW_CATEGORY_RAG_ENABLED (default on).

New: Category RAG
- Upload a photo or document to Telegram, then name a category with a
  "#name" caption (half-width # or full-width ＃, "#[multi word]" too) or by
  replying with the name after the file. Each category is its own Qdrant
  collection (oc_cat_<slug>), so unrelated material never cross-matches.
- Query one category with "/rag #name <question>", every category with
  "/rag #all <question>". A plain "/rag" is unchanged and never touches
  category collections.
- "/cat list" shows categories and their chunk counts. "/cancel" drops a
  file that is waiting for a category; a document with no category given
  within OPENCLAW_CATEGORY_PENDING_TTL_SECONDS falls back to the general
  knowledge base.
- Photos are indexed by embedding a vision-model description plus any
  transcribed text; the original image is kept alongside it.
- See docs/CATEGORY_RAG.md and docs/CATEGORY_RAG_MANUAL_TEST.md.

New: source attribution in /rag
- Every /rag answer ends with a "來源：" line naming the documents it drew
  from. The name shown is the uploader's original filename when known,
  otherwise the document's first heading, otherwise the stored filename.
- The gateway records the original filename in a <file>.meta.json sidecar at
  upload time (Telegram-stored names are timestamped and ASCII-sanitised, so
  a CJK filename would otherwise be lost). Imported Google Docs cite their
  title.

New: vision endpoint decoupling
- A VisionClient seam is the only place OpenClaw talks to a vision model.
- OPENCLAW_VLM_BASE_URL / OPENCLAW_VLM_MODEL / OPENCLAW_VLM_MAX_TOKENS
  (default back to the main vLLM). Point them at any OpenAI-compatible
  endpoint to switch models with no code change.
- compose.yaml has an optional "vision" profile with a dedicated
  openclaw-vllm-vision service (port 8001, off by default).
- Image indexing quality depends on this model; the default text model
  cannot read images.

Other:
- Memory watcher skips dotfiles/dot-directories under the inbox.
- Full unit coverage for the above (categories, vision client, retrieval
  routing, gateway upload flow).

Note: an independent v1.3 line of work (multi-model catalog / engineering
review) lives on its own branch and is not part of this release.
```
