# OpenClaw Arm Continuum

版本：`v1.2`

授權：Apache-2.0

[English README](README.md)

OpenClaw Arm Continuum 是一套面向 Arm 架構的地端優先個人 AI runtime，目標是讓同一套 OpenClaw 助理系統可以從 edge device 延伸到 Arm CPU server，再到 DGX Spark / GB10 類本地 AI 工作站。

英文使用情境請看 [docs/USE_CASES.md](docs/USE_CASES.md)。可直接複製到
Telegram 的範例指令放在 [examples/](examples/)。

這個專案的核心觀點是：

```text
Same runtime, different capability tiers.
```

也就是同一套 runtime，可以依照硬體能力選擇不同推理方式、部署位置與隱私邊界。低功耗 edge device 可以常駐接收 Telegram、執行 cron、管理記憶與 RAG；Arm CPU server 可以執行較小的本地模型；DGX Spark / GB10 則可以用本機 NVIDIA GPU 與 vLLM 承擔大型模型推理。若需要集中算力，也可以讓 Arm gateway 把推理請求導向可信任內網中的 vLLM server。

## 專案特色

- 地端優先：日常對話、RAG、文件、記憶與任務歷史預設留在本機或可信任內網。
- Arm continuum：同一套助理 runtime 可以橫跨 edge、CPU-only Arm server、DGX Spark / GB10。
- Telegram-first：用 Telegram 作為手機入口，支援文字、文件、圖片、語音與 cron 管理。
- 本地 RAG：使用 Ollama embedding 與 Qdrant 建立個人記憶與文件知識庫。
- 網頁查詢：透過 Playwright / Chromium worker 執行本地瀏覽器搜尋與頁面擷取。
- 語音輸入：透過本地 Whisper service 轉錄 Telegram voice message。
- 主動推播：支援 cron 任務與 Telegram 推播。
- 薄型 agent runtime：使用 `AgentRegistry` / `TaskDispatcher` 做 skill-based routing，避免過早引入沉重框架。

## 部署 Profile

| Profile | 狀態 | 推理方式 | 目標硬體 |
|---|---|---|---|
| `dgx-spark` | Stable | 本機 NVIDIA GPU + vLLM | DGX Spark / GB10 類工作站 |
| `arm-cpu-only` | Experimental，已在 Orion O6 驗證 | 本機 CPU LLM，使用 llama.cpp 或其他 OpenAI-compatible endpoint | Radxa Orion O6 或類似 Armv9 CPU server |
| `arm-remote-llm` | Planned | Arm host 執行 runtime，推理導向可信任內網 vLLM server | RPi5 / 小型 Arm gateway + 內網推理 server |

目前已驗證並可作為 stable baseline 的是 `dgx-spark`。`arm-cpu-only` 已在 Radxa Orion O6、Baidu ERNIE 4.5 與 llama.cpp 上完成實驗性驗證；它仍標記為 Experimental，因為 CPU-only 效能、模型 preset 與可選服務會依主機差異而變動。詳細規劃請看 [docs/PLATFORMS.md](docs/PLATFORMS.md)，部署步驟請看 [docs/ERNIE_LLAMA_CPP.md](docs/ERNIE_LLAMA_CPP.md)。

## 為什麼是 Arm Continuum

Arm 的優勢不只是在單一裝置上省電，而是同一個 CPU 架構可以從低功耗 edge 一路延伸到 server 與本地 AI workstation。

OpenClaw Arm Continuum 利用這個特性，把個人 AI 助理拆成幾個可以依硬體能力移動的層次：

- Edge device：長時間在線，負責 Telegram、cron、RAG gateway、資料接收與喚醒任務。
- Arm CPU server：用較小模型或 MoE 模型執行本地推理，兼顧隱私與功耗。
- DGX Spark / GB10：使用本地 GPU 與 vLLM 執行高吞吐大模型推理。
- 內網 vLLM server：提供多台 Arm gateway 共用的私有推理資源。

這讓使用者可以依照需求選擇：

- 最強隱私：全部在單機上執行。
- 最低功耗：常駐服務放在小型 Arm device。
- 最大推理能力：導向 DGX Spark / GB10 或內網 vLLM。
- 最佳彈性：同一套 runtime，未來可逐步替換模型與硬體。

## 目前 v1.2 包含什麼

- `openclaw-vllm`：本機 OpenAI-compatible vLLM endpoint。
- `openclaw-telegram`：Telegram long polling gateway 與指令入口。
- `openclaw-cron`：定時任務與 Telegram 主動推播。
- `openclaw-memory-watcher`：監控 inbox，切 chunk，寫入 Qdrant。
- `openclaw-browser-scraper`：Playwright / Chromium 網頁查詢 worker。
- `openclaw-whisper`：本地 Whisper 語音轉錄 service。
- `openclaw-gateway`：官方 OpenClaw Gateway dashboard。
- runtime profiles：用來切分 personal / demo 的 `.env`、workspace、Gateway state 與 Qdrant collections。
- 英文 onboarding 文件與可直接貼進 Telegram 的範例指令。

如果需要在同一台主機上切開個人資料與 demo 資料，請使用 runtime profiles，
讓 `.env`、workspace、Gateway state、Telegram bot/chat ID 與 Qdrant collections
分開。詳細方式請看 [docs/PROFILES.md](docs/PROFILES.md)。

## 前置需求

以目前穩定的 `dgx-spark` profile 來說，主機需要先準備：

- Linux workstation with NVIDIA GPU。
- NVIDIA driver。
- NVIDIA Container Toolkit。
- Docker Engine。
- Docker Compose plugin。
- Git、curl、bash 或相容 shell。
- Ollama，並在 host 上提供 `127.0.0.1:11434`。
- Qdrant，並在 host 上提供 `127.0.0.1:6333`。
- Telegram bot token。
- 足夠磁碟空間存放模型、Docker images 與 workspace data。

Ollama 需要先拉 embedding model，例如：

```bash
ollama pull nomic-embed-text
```

如果模型需要 Hugging Face 權限，請先在主機上完成 Hugging Face login 或設定對應 cache/token。

## 快速開始

複製設定檔：

```bash
cp .env.example .env
```

至少需要填入：

```text
OPENCLAW_TELEGRAM_BOT_TOKEN=<your-telegram-bot-token>
OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS=<your-chat-id>
OPENCLAW_CRON_CHAT_IDS=<your-chat-id>
OPENCLAW_GATEWAY_TOKEN=<generate-a-long-random-token>
```

這四個是「主機軟體與服務都已準備好」之後的最小個人化設定。不同 GPU、記憶體、模型或 cache 位置可能還需要調整：

```text
OPENCLAW_VLLM_MODEL=
OPENCLAW_GPU_MEMORY_UTILIZATION=
OPENCLAW_MAX_MODEL_LEN=
OPENCLAW_MAX_NUM_SEQS=
HF_CACHE_DIR=
```

如果 Ollama 或 Qdrant 是跑在同一個 Docker network 裡的容器，而不是 host loopback
服務，可以在 `.env` 覆寫：

```text
OPENCLAW_OLLAMA_BASE_URL=http://ollama:11434
OPENCLAW_QDRANT_BASE_URL=http://qdrant:6333
```

DGX compose 預設會透過 `http://openclaw-vllm:8000/v1` 連到同一個 compose
stack 內的 vLLM。只有在推理端點是外部或遠端 OpenAI-compatible server 時，
才需要設定 `OPENCLAW_VLLM_BASE_URL`。

啟動：

```bash
docker compose --env-file .env -f compose.yaml up -d
```

Radxa Orion O6 / Arm CPU-only profile 可以先複製範例設定：

```bash
cp .env.arm-cpu-only.example .env
docker compose --env-file .env -f compose.arm-cpu-only.yaml --profile web --profile gateway --profile voice up -d
```

如果只想啟動核心服務，不啟動 scraper、Gateway dashboard 或 voice service，可以省略 compose profiles。這種情況下也請在 `.env` 裡設定
`OPENCLAW_WEB_ENABLED=false` 與 `OPENCLAW_WHISPER_ENABLED=false`：

```bash
docker compose --env-file .env -f compose.arm-cpu-only.yaml up -d
```

檢查 container：

```bash
docker compose --env-file .env -f compose.yaml ps
docker logs --tail 80 openclaw-telegram
docker logs --tail 80 openclaw-cron
```

檢查本機 endpoint：

```bash
curl http://127.0.0.1:8000/v1/models
curl -I http://127.0.0.1:18789/
```

## Telegram 指令

在 Telegram 輸入：

```text
/help
```

會看到手機友善的指令速查。

主要指令：

```text
/mem      寫入個人記憶
/rag      查詢本地記憶或文件
/doc      匯入公開 Google Doc
/search   使用本地 browser worker 查網頁
/cron     建立與管理主動推播任務
/agents   列出目前本地 agents
/tasks    查看最近任務紀錄
```

目前已實作的 help：

```text
/help
/doc
/cron
```

天氣查詢不是 slash command，直接用自然語言問就會自動路由到天氣 skill，
例如「劍橋今天天氣如何」。**不要**在天氣問句前面加 `/search`：明確的
`/search` 指令永遠有最高路由優先權，`/search 劍橋今天天氣如何` 會變成
一般網頁搜尋，而不是走專門處理天氣的查詢邏輯。

## 記憶與 RAG

Qdrant 預設使用兩個 collection。profile 專屬 `.env` 可以覆寫 collection
名稱，例如 demo profile 可以使用 `demo_tracker_memory` 與
`demo_knowledge_base`。

| Collection | Telegram scope | 用途 |
|---|---|---|
| `OPENCLAW_TRACKER_COLLECTION` 預設：`personal_tracker_memory` | `memory:` | 動態個人記憶、todo、決策、追蹤資料、網頁擷取內容 |
| `OPENCLAW_KNOWLEDGE_COLLECTION` 預設：`personal_knowledge_base` | `knowledge:` | 長期文件知識庫、技術手冊、白皮書、正式匯入文件 |

範例：

```text
/mem #todo due:2026-07-05 檢查 cron dashboard
/rag memory: 最近記住了哪些 OpenClaw 設定？
/rag knowledge: 這份架構文件的重點是什麼？
/rag doc:260702_01 這份文件在說什麼？
```

## 文件流程

支援 Telegram 上傳：

```text
.pdf .md .txt .log .json .csv .tsv
```

目前流程：

1. 使用者上傳文件到 Telegram。
2. OpenClaw 保存到本地 inbox。
3. `openclaw-memory-watcher` 自動索引支援格式到 Qdrant。
4. 使用 `/rag` 查詢已索引內容。

常用指令：

```text
/doc url https://docs.google.com/document/d/.../edit
/doc url https://docs.google.com/document/d/.../edit tracker
```

公開 Google Doc 可用 `/doc url` 匯入。預設進 `knowledge`，最後加 `tracker` 則進動態追蹤記憶。

Caption 快捷方式：

```text
/rag        上傳文件直接進 knowledge
/knowledge  上傳文件直接進 knowledge
/mem        上傳文件直接進 tracker memory
/tracker    上傳文件直接進 tracker memory
```

「先審核再決定是否正式匯入」的文件流程是規劃項目，Telegram gateway
尚未完整實作。目前上傳文件會先保存到本地 inbox，支援格式會直接由
memory watcher 索引。

## Cron 主動推播

建立動態排程，`名稱 :: 任務內容`中間的 `::` 是必要的分隔符：

```text
/cron add daily 07:30 早報 :: 劍橋今天天氣如何
/cron add weekly mon 08:00 週報 :: 整理本週 AI 硬體新聞
/cron add monthly 1 09:00 月報 :: 回顧本月個人目標
/cron list
/cron run <job_id>
/cron delete <job_id>
```

`::` 後面的任務內容，路由方式跟一般聊天訊息完全一樣：單純的天氣問句（不要加
`/search`）會走天氣 skill，`/search <查詢>` 則會強制走一般網頁搜尋。

`daily`、`weekly`、`monthly` 固定時間任務只會在指定時間窗口內執行，避免 container 晚上重啟後補跑早上的任務。

## Gateway Dashboard

Gateway dashboard 預設只綁定 localhost：

```text
http://127.0.0.1:18789/
```

如果主機在遠端，可以用 SSH tunnel：

```bash
ssh -L 18789:127.0.0.1:18789 <user>@<host>
```

然後在本機瀏覽器開：

```text
http://127.0.0.1:18789/
```

登入時使用 `.env` 裡的：

```text
OPENCLAW_GATEWAY_TOKEN
```

Compose profile 會在 Gateway 啟動前啟用官方 `admin-http-rpc` plugin。
OpenClaw 透過這個私有 operator RPC surface 同步 `cron.list`、`cron.add`、
`cron.update`、`cron.remove` 與 run history。請把 Gateway 保持在 localhost、
SSH tunnel 或可信任私有網路後面，不要把 admin RPC route 直接暴露到公網。

## 安全預設

- 不要 commit `.env`。
- 不要 commit `workspace/`、`gateway-data/`、Qdrant snapshot、SQLite、log、backup。
- Telegram 必須設定 allowlist：`OPENCLAW_TELEGRAM_ALLOWED_CHAT_IDS`。
- vLLM、Qdrant、Ollama、Gateway 預設應只綁定 localhost。
- 遠端使用 Gateway dashboard 時優先使用 SSH tunnel。

發布前請看：

- [docs/SECURITY.md](docs/SECURITY.md)
- [docs/PUBLISH_CHECKLIST.md](docs/PUBLISH_CHECKLIST.md)
- [PUBLICATION_PRIVACY_REVIEW.md](PUBLICATION_PRIVACY_REVIEW.md)

## 路線圖

目前 stable baseline 是：

```text
dgx-spark / GB10 + local vLLM
```

已驗證的實驗性 Arm CPU-only 路徑：

```text
Radxa Orion O6 + Baidu ERNIE 4.5 + llama.cpp
```

未來 profile：

```text
RPi5 / Arm gateway + private LAN vLLM
```

未來多模態能力應使用明確的 specialist backend：DGX / GB10 上用正式 VLM
模型與 vLLM，Arm CPU-only 上則在驗證後接入 MNN Omni 類型的圖片/截圖分析
worker。

## 授權

本專案使用 Apache License 2.0。請看 [LICENSE](LICENSE)。
