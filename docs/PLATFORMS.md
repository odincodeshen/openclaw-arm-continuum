# Platform Profiles

OpenClaw is designed as one runtime with multiple deployment profiles. The Python runtime should stay endpoint-driven: model serving, embedding, Qdrant, Whisper, scraper, and Gateway are configured through `.env` rather than hard-coded hardware assumptions.

## Profile Matrix

| Profile | Status | LLM serving | Best fit | Privacy boundary |
|---|---|---|---|---|
| `dgx-spark` | Stable | Local vLLM on NVIDIA GPU | DGX Spark / GB10 class workstation | Single local host |
| `arm-cpu-only` | Experimental | Local CPU LLM endpoint, preferably llama.cpp | Radxa Orion O6 or similar Armv9 board/server | Single local host |
| `arm-remote-llm` | Planned / beta | Remote private LAN vLLM endpoint | RPi5 or small Arm gateway plus local inference server | Trusted private LAN |

## Current Stable Profile: `dgx-spark`

`dgx-spark` is the currently validated profile.

Characteristics:

- `openclaw-vllm` is started by Docker Compose.
- vLLM uses local NVIDIA GPU acceleration.
- Ollama provides local embeddings.
- Qdrant stores local memory and RAG vectors.
- Telegram, cron, scraper, Whisper, Gateway, memory watcher, and dispatcher run on the same host.

This is the flagship path for full local privacy and large-model throughput.

## Experimental Profile: `arm-cpu-only`

The current experimental Arm CPU-only target is:

```text
Radxa Orion O6 + llama.cpp + Baidu ERNIE 4.5
```

Goal:

- Keep OpenClaw fully local on an Arm CPU host.
- Replace GPU vLLM with a CPU-friendly local OpenAI-compatible endpoint.
- Use llama.cpp for ERNIE 4.5 model serving where possible.
- Keep Telegram, cron, memory watcher, scraper, Qdrant, and Gateway mostly unchanged.

Why this is feasible:

- The runtime already calls the LLM through `OPENCLAW_VLLM_BASE_URL` and `OPENCLAW_VLLM_MODEL`.
- The Arm learning path for ERNIE 4.5 focuses on deploying Mixture-of-Experts models on Armv9 with llama.cpp.
- MoE-style ERNIE 4.5 models can expose large total parameter counts while activating a smaller fraction per token, which makes them worth testing on resource-constrained Arm hosts.

Planned target stack:

```text
Hardware: Radxa Orion O6
LLM runtime: llama.cpp server, OpenAI-compatible if possible
Model family: Baidu ERNIE 4.5
Embedding: Ollama or llama.cpp-compatible embedding endpoint
Vector DB: Qdrant local
Telegram/Cron/Scraper/Whisper/Gateway: OpenClaw runtime containers
```

Initial model candidates:

```text
baidu/ERNIE-4.5-0.3B-PT
baidu/ERNIE-4.5-21B-A3B-PT
baidu/ERNIE-4.5-21B-A3B-Thinking
```

Expected constraints:

- CPU-only inference will be slower than DGX Spark.
- Context length, retrieval count, and generation length should be reduced.
- Vision should be disabled by default.
- Whisper should default to `tiny` or `base`.
- ERNIE model conversion, quantization, tokenizer compatibility, and llama.cpp server behavior must be verified before this profile can be marked stable.

Initial `.env` direction:

```text
OPENCLAW_VLLM_BASE_URL=http://host.docker.internal:<llama-cpp-port>/v1
OPENCLAW_VLLM_MODEL=<ernie-llama-cpp-model-name>
OPENCLAW_MAX_TOKENS=256
OPENCLAW_MAX_MODEL_LEN=8192
OPENCLAW_RETRIEVAL_LIMIT=3
OPENCLAW_VISION_ENABLED=false
OPENCLAW_WHISPER_MODEL=tiny
```

Planned implementation artifacts:

```text
compose.arm-cpu-only.yaml
.env.arm-cpu-only.example
docs/ERNIE_LLAMA_CPP.md
```

The first implementation artifacts now exist. See `docs/ERNIE_LLAMA_CPP.md` for the Orion O6 + ERNIE 4.5 + llama.cpp bring-up flow.

## Future Profile: `arm-remote-llm`

`arm-remote-llm` keeps the personal assistant runtime on a small Arm host but sends generation requests to a private LAN inference server.

Good fit:

- RPi5 as a Telegram/Cron/RAG gateway.
- Arm CPU host as a low-power always-on assistant controller.
- Separate private LAN vLLM server for heavier models.

Likely `.env` direction:

```text
OPENCLAW_VLLM_BASE_URL=http://<lan-vllm-host>:8000/v1
OPENCLAW_VLLM_MODEL=<remote-model-name>
```

This is still local-first, but the privacy boundary changes from "single host" to "trusted private LAN." Users should not expose the remote vLLM endpoint directly to the public internet.

Planned implementation artifacts:

```text
compose.arm-remote-llm.yaml
.env.arm-remote-llm.example
docs/REMOTE_LLM.md
```

## Runtime Flexibility Rules

To keep all profiles healthy:

- Do not hard-code hardware checks into the Python runtime.
- Keep endpoints configurable through `.env`.
- Keep model-specific quirks inside deployment docs or small adapter modules.
- Treat `OPENCLAW_VLLM_BASE_URL` as "generation endpoint," even if the backend is vLLM, llama.cpp, Ollama, or another OpenAI-compatible server.
- Keep RAG collections and document metadata stable across profiles.

## Roadmap Order

1. Keep `dgx-spark` stable.
2. Bring up `arm-cpu-only` on Radxa Orion O6 with ERNIE 4.5 and llama.cpp.
3. Add repeatable smoke tests for CPU-only chat, `/mem`, `/rag`, `/search`, and `/cron`.
4. Add `arm-remote-llm` after the local CPU-only profile has clean deployment docs.
