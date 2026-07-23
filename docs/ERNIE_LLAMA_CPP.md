# ERNIE 4.5 on Radxa Orion O6 with llama.cpp

This guide describes the `arm-cpu-only` deployment path for OpenClaw Arm Continuum.

The target stack is:

```text
Radxa Orion O6
Debian 12 aarch64
llama.cpp server on the host
Baidu ERNIE 4.5 GGUF model
OpenClaw runtime containers
Ollama embeddings on the host
Qdrant on the host
```

The real `.env` file is private host state. Do not commit it.

## 1. Prepare llama.cpp

Build llama.cpp on the Orion O6:

```bash
cd $HOME
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build
cmake --build build -j
```

If you are following the Arm learning path and already have a tuned build directory, keep using it.

## 2. Download ERNIE 4.5 GGUF

Create a model directory:

```bash
mkdir -p $HOME/ernie_lp/model
cd $HOME/ernie_lp/model
```

Download the Thinking Q4 GGUF model:

```bash
curl -L -C - \
  -o ERNIE-4.5-21B-A3B-Thinking-Q4_0.gguf \
  "https://modelscope.cn/models/unsloth/ERNIE-4.5-21B-A3B-Thinking-GGUF/resolve/master/ERNIE-4.5-21B-A3B-Thinking-Q4_0.gguf"
```

Verify the file before running it:

```bash
ls -lh ERNIE-4.5-21B-A3B-Thinking-Q4_0.gguf
head -c 4 ERNIE-4.5-21B-A3B-Thinking-Q4_0.gguf
echo
```

The header should be:

```text
GGUF
```

## 3. Smoke Test llama.cpp

Run a short local generation test:

```bash
cd $HOME/ernie_lp/llama.cpp/build_v9_on

./bin/llama-cli \
  --jinja \
  -m $HOME/ernie_lp/model/ERNIE-4.5-21B-A3B-Thinking-Q4_0.gguf \
  -p "Explain mixture-of-experts models in one sentence." \
  -c 2048 \
  -t 12 \
  -n 80
```

Success means the model loads and generates text. On Orion O6, CPU-only generation is expected to be much slower than DGX Spark.

## 4. Start llama-server

Start the OpenAI-compatible server on the host:

```bash
cd $HOME/ernie_lp/llama.cpp/build_v9_on

./bin/llama-server \
  --jinja \
  -m $HOME/ernie_lp/model/ERNIE-4.5-21B-A3B-Thinking-Q4_0.gguf \
  -c 2048 \
  -t 12 \
  --host 127.0.0.1 \
  --port 8080
```

Verify from another shell:

```bash
curl http://127.0.0.1:8080/v1/models
```

Test chat completions:

```bash
curl -sS http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ernie-o6",
    "messages": [
      {
        "role": "user",
        "content": "In one sentence, describe a local AI workload suitable for Orion O6."
      }
    ],
    "max_tokens": 80,
    "temperature": 0.2
  }'
```

## 5. Prepare Ollama and Qdrant

OpenClaw uses Ollama for embeddings and Qdrant for vector memory/RAG.

Ollama should listen on:

```text
http://127.0.0.1:11434
```

Pull an embedding model:

```bash
ollama pull nomic-embed-text
```

Qdrant should listen on:

```text
http://127.0.0.1:6333
```

One simple Docker option for Qdrant is:

```bash
docker run -d \
  --name qdrant \
  --restart unless-stopped \
  -p 6333:6333 \
  -v $HOME/qdrant_storage:/qdrant/storage \
  qdrant/qdrant:latest
```

## 6. Configure OpenClaw

Clone the public repository:

```bash
cd $HOME
git clone https://github.com/odincodeshen/openclaw-arm-continuum.git
cd openclaw-arm-continuum
```

Create the private local environment file:

```bash
cp .env.arm-cpu-only.example .env
openssl rand -hex 32
```

Edit `.env` and fill in only local private values:

```text
OPENCLAW_TELEGRAM_BOT_TOKEN=<your-o6-telegram-bot-token>
OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS=<your-chat-id>
OPENCLAW_CRON_CHAT_IDS=<your-chat-id>
OPENCLAW_GATEWAY_TOKEN=<random-token-from-openssl>
```

The `.env` file is ignored by git and must stay on the Orion O6.

If `/search` reports that no usable web result was found while the host can
access the internet, check the DNS settings used by Docker containers. On some
Arm boards, Docker may generate an empty container `/etc/resolv.conf` after
NetworkManager or reboot events. Set these values to your router or internal DNS
server first, with a fallback resolver second:

```text
OPENCLAW_DNS_SERVER_1=192.168.0.1
OPENCLAW_DNS_SERVER_2=1.1.1.1
```

For small CPU-only LLM context windows, keep web-search summarization compact:

```text
OPENCLAW_SCRAPER_LIMIT=2
OPENCLAW_WEB_CONTEXT_CHARS=1800
```

## 7. Start OpenClaw

Start the full CPU-only demo services:

```bash
docker compose --env-file .env -f compose.arm-cpu-only.yaml --profile web --profile gateway --profile voice up -d
```

For a lighter core-only start without scraper, Gateway dashboard, or voice
service, set `OPENCLAW_WEB_ENABLED=false` and `OPENCLAW_WHISPER_ENABLED=false`
in `.env`, then run:

```bash
docker compose --env-file .env -f compose.arm-cpu-only.yaml up -d
```

Check status:

```bash
docker compose --env-file .env -f compose.arm-cpu-only.yaml ps
docker logs --tail 80 openclaw-telegram
docker logs --tail 80 openclaw-memory-watcher
docker logs --tail 80 openclaw-cron
```

Verify that the scraper container can resolve domains when the `web` profile is
enabled:

```bash
docker exec openclaw-browser-scraper cat /etc/resolv.conf
docker exec openclaw-browser-scraper python - <<'PY'
import socket
for host in ("duckduckgo.com", "google.com"):
    print(host, socket.gethostbyname(host))
PY
```

The Gateway dashboard is available when the `gateway` profile is enabled:

```bash
curl -I http://127.0.0.1:18789/
```

The Gateway image must support `linux/arm64` for this optional profile to work.
The profile enables the bundled `admin-http-rpc` plugin before starting Gateway
so OpenClaw can sync Telegram cron jobs with the official dashboard.

## 8. Telegram Smoke Test

Use the dedicated O6 bot:

```text
/help
```

Test chat:

```text
In one sentence, describe a local AI workload suitable for Orion O6.
```

Test memory:

```text
/mem #test The O6 runtime uses llama.cpp for local generation.
/rag memory: Which backend does the O6 runtime use for local generation?
```

Success criteria:

```text
1. The O6 bot replies to /help.
2. The O6 bot answers through ERNIE 4.5 on llama.cpp.
3. /mem writes to the O6 Qdrant collections.
4. /rag memory: retrieves the O6 memory.
5. /search works when the web profile is enabled.
6. Voice input works when the voice profile is enabled.
7. The DGX Spark / GB10 bot is unaffected.
```

## Notes

- Keep `OPENCLAW_VISION_ENABLED=false` for the first CPU-only profile.
- Keep `OPENCLAW_WHISPER_ENABLED=true` only when the `voice` profile is running;
  set it to `false` for core-only deployments.
- The Thinking model may return `<response>` tags or thinking text. A future runtime adapter should sanitize ERNIE-specific response wrappers.
- Treat `OPENCLAW_VLLM_BASE_URL` as the generation endpoint name, even when the backend is llama.cpp instead of vLLM.
