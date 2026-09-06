# 分類式 RAG（Category RAG）實作計畫

> Branch: `dev` · 狀態：**Stage 1–5 已實作，測試通過（161 tests）**，待主機 e2e · 建立：2026-09-05
>
> 使用者說明見 `docs/CATEGORY_RAG.md`。以下決策全部採用計畫的預設方案；
> `OPENCLAW_CATEGORY_ALWAYS_PROMPT` 於實作時移除（`category_rag_enabled` 單一開關即可）。

## 1. 目標

使用者從 Telegram 傳一張圖片或一份文件，接著（或用 caption）給一段文字當「類別」。
系統把該資料 embed 後寫進**該類別專屬的 Qdrant collection**，使不同類型的資料
在檢索時彼此隔離，不做跨類別關聯。

## 2. 現況（要接上去的地方）

| 元件 | 現況 |
|------|------|
| Qdrant collection | 只有兩個固定：`personal_tracker_memory`、`personal_knowledge_base` |
| 文件流程 | Telegram → `inbox/knowledge\|tracker/telegram/` → `memory_watcher` 掃描 → 依最上層資料夾決定 collection → chunk → ollama embed → Qdrant |
| 圖片流程 | 存到 `inbox/media/telegram/` → 丟 vLLM/VLM 分析並回覆，**不進 RAG** |
| `/rag` | 每次同時查兩個固定 collection，無「指定類別」概念 |
| 支援副檔名 | `.md .txt .log .json .csv .tsv .pdf` |
| 佈署 | `gateway` 與 `memory-watcher` 兩個 container 共用 `/workspace` volume、共用 `.env` |

## 3. 設計決策（預設值，審核時可改）

| # | 決策點 | 採用方案 | 備選 |
|---|--------|----------|------|
| D1 | 輸入方式 | **兩段式為主**：傳檔後 bot 提示輸入類別；下一則純文字（非 `/` 指令）即類別。**caption `#類別名` 為捷徑**（單則、無狀態）。 | 只做 caption |
| D2 | 類別管理 | **自由新增**：新類別名首次使用即自動建立 collection。維護 `inbox/.openclaw/categories.json` 註冊表（顯示名 ↔ slug ↔ collection）。 | 固定白名單／二次確認 |
| D3 | collection 命名 | 決定性推導：`oc_cat_<ascii_slug|x>_<sha1(正規化類別名)[:8]>`。中文名靠 hash 後綴保證唯一穩定；註冊表存原始顯示名。 | 純 hash／純 slug |
| D4 | 讀取端 | `/rag #<類別> <問題>` → 只查該 collection；`/rag #all <問題>` → 掃所有類別 collection 合併；不帶 `#` → 維持現狀（tracker + knowledge）。 | 不帶 `#` 也掃全部 |
| D5 | 圖片入庫內容 | 透過**新的 `VisionClient` 介面**產生「結構化描述 + 可見文字」，連同使用者補充說明一起 embed；payload 保留原圖路徑。文字由可插拔 extractor 清單產出（目前 `vlm_description`，OCR 預留）。 | 只存使用者文字 |
| D9 | 視覺模型解耦 | `VisionClient` 是唯一的模型接觸點；端點用 provider-neutral 設定 `OPENCLAW_VLM_BASE_URL` / `OPENCLAW_VLM_MODEL` / `OPENCLAW_VLM_MAX_TOKENS`，預設全部回退到主 vLLM。`compose.yaml` 附 `profiles:[vision]` 的第二個 vLLM，預設關閉。OCR（方案 C）延後，記入 `docs/FUTURE_TODO.md`。 | 直接改主模型 env |
| D6 | ingest 執行點 | **單一路徑**：gateway 只負責把「可被 watcher 讀取的檔案」放進 `inbox/categories/<slug>/`（圖片額外先跑 VLM 產生 `.md`），實際 embed/chunk/dedup 全部沿用 `memory_watcher`。 | gateway 直接寫 Qdrant |
| D7 | 既有兩個 collection | 不動，本功能純新增。 | — |
| D8 | 行為變更 | 傳圖／傳檔且無可辨識 caption 時，bot 會**多一句**提示輸入類別（原本的分析／存檔照舊）。可用 `OPENCLAW_CATEGORY_ALWAYS_PROMPT=false` 關閉。 | 預設關閉 |

## 4. 資料流

### 4.1 兩段式（無 `#` caption）
```
使用者傳 PDF/圖片
  └─ gateway: 下載存檔（照現行路徑）
     ├─ 圖片：照現行跑 VLM 分析並回覆
     └─ 設 PENDING[chat_id] = {files:[...], kind, ts}
        回覆：「已收到並處理。要歸入哪個知識類別？直接回一個類別名，或 /cancel 取消。」
使用者傳「工作筆記」
  └─ gateway.handle_message: 偵測 PENDING 未過期且非 / 指令
     ├─ slug = slugify("工作筆記"); 建 inbox/categories/<slug>/
     ├─ 文件：複製檔案進該資料夾
     ├─ 圖片：跑一次 VLM → 寫 <name>.md（frontmatter 記 image_path / category）
     ├─ 寫 <name>.meta.json（顯示類別名）
     ├─ 更新 categories.json 註冊表
     ├─ 清 PENDING
     └─ 回覆：「已排入類別「工作筆記」（collection: oc_cat_xxx），約 10 秒後可用 /rag #工作筆記 查詢。」
memory_watcher（~10s 後）
  └─ scan_once → _collection_for 認出 categories/<slug>/ → oc_cat_xxx
     → ensure_collection → chunk → embed → upsert（payload 帶 category / category_slug / image_path / sha256）
```

### 4.2 Caption 捷徑
`caption = "#工作筆記"` 或 `"#工作筆記 這張是伺服器機櫃照"` → 直接走上面的 ingest 動作，`#` 後第一段為類別名，其餘為補充說明，不設 PENDING。

### 4.3 查詢
```
/rag #工作筆記 這份文件的重點？
  └─ RagRetrieveSkill：解析前置 #token → 查註冊表 → collection
     → 只搜該 collection（+ 該 collection 內的檔名比對）→ 丟 LLM 作答
/rag #all 關於機櫃的資料
  └─ 對註冊表所有 collection 各搜一次 → 合併排序 → 作答
/rag 一般問題
  └─ 不變（tracker + knowledge）
```

## 5. 變更清單

### 新檔
| 檔案 | 內容 |
|------|------|
| `app/openclaw_runtime/categories.py` | `category_slug()`, `category_collection_name()`, `load_registry()/upsert_registry_entry()/resolve_category()`, `parse_category_caption()`；純函式、可單測 |
| `app/openclaw_runtime/vision_client.py` | `VisionClient.describe_image(path, instruction)`；唯一的視覺模型接觸點；OpenAI 相容端點 |
| `tests/test_categories.py` | slug 正規化、中文 hash 後綴、碰撞、collection 命名決定性、註冊表讀寫 |
| `tests/test_category_rag_retrieve.py` | `#cat` / `#all` / 預設 / 停用 的查詢路由 |
| `docs/CATEGORY_RAG.md` | 使用者說明（指令、caption 慣例、`/cat`） |

### 修改
| 檔案 | 變更 |
|------|------|
| `app/openclaw_runtime/config.py` | 新增 Settings 欄位：`category_rag_enabled`(True)、`category_collection_prefix`(`oc_cat_`)、`category_pending_ttl_seconds`(600)、`category_max_name_chars`(40)、`category_always_prompt`(False)、`category_registry_path`(`/workspace/inbox/.openclaw/categories.json`)、`category_inbox_dirname`(`categories`) |
| `tests/support.py` | `build_settings` 補上述預設 |
| `app/openclaw_runtime/file_ingest.py` | `_collection_for`：`categories/<slug>/…` → `category_collection_name`；ingest 前對 category collection 呼叫 `ensure_collection`（以 set 快取）；讀 `.meta.json` sidecar，把 `category`/`category_slug`/`image_path` 併入 upsert metadata；`SUPPORTED_SUFFIXES` 不變（`.jpg` 由 watcher 略過，靠 `.md` 入庫） |
| `app/openclaw_runtime/qdrant_client.py` | 新增 `list_points_count(collection)`（給 `/cat list`）；`search` 已泛用不需改 |
| `app/openclaw_runtime/skills/memory.py` | `RagRetrieveSkill.run`：解析前置 `#token`；`#all` fan-out；`#<name>` 解析註冊表→單一 collection；未知類別回明確訊息；`_file_hits` 支援指定 collection。`_strip_command` 去掉 `#token` |
| `app/openclaw_telegram_gateway.py` | ①`PENDING_CATEGORY` dict + lock + TTL；②`handle_document_message`/`handle_photo_message`：偵測 `#` caption 或設 PENDING；③`handle_message`：文字進入前先檢查 PENDING；④`/cancel`；⑤`/cat list\|help`；⑥`ingest_into_category()` helper（建資料夾／複製檔／圖片跑 VLM 寫 `.md`／寫 `.meta.json`／更新註冊表）；⑦`HELP_TEXT`、`setup_bot_commands` 補 `/cat` |
| `app/openclaw_memory_watcher.py` | 啟動時 log 註冊表載入的類別數；`ensure_collections` 之外不需大改（category collection lazy 建立） |
| `.env.example` / `.env.arm-cpu-only.example` | 新增上述 `OPENCLAW_CATEGORY_*` 變數與註解 |
| `README.md` / `README.zh-TW.md` | 「Document RAG」段落補分類式 RAG 用法 |
| `docs/FUTURE_TODO.md` | 勾掉／移除對應項目（若有） |

### 佈署
- 無新 container、無新對外埠。
- 新環境變數經 `.env`（`env_file`）自動帶入 gateway 與 memory-watcher，compose 只需在 `.env.example` 補說明；如要顯式預設值再加到 `compose.yaml` 兩個 service 的 `environment:`。
- `inbox/.openclaw/`、`inbox/categories/` 在共用 volume，兩個 process 都讀得到；註冊表用「原子寫入（temp+rename）＋只新增條目」容忍競態。

## 6. 測試計畫

| 測試 | 重點 |
|------|------|
| `test_categories.py` | slug／hash／碰撞／collection 命名決定性／註冊表 round-trip |
| `test_file_ingest.py`（擴充） | `categories/foo/note.md` → `oc_cat_foo_xxxx`；`.meta.json` 顯示名進 payload；`.jpg` 被略過不報錯 |
| `test_telegram_gateway.py`（擴充） | PENDING：傳檔設狀態 → 後續文字觸發 ingest；`/cancel` 清除；TTL 過期後文字走正常路由；`/` 指令不被當類別；`#cat` caption 直接 ingest 不設 PENDING |
| `test_skill_router.py` / memory 測試 | `/rag #cat` 只查目標 collection；`/rag #all` fan-out；未知類別訊息；不帶 `#` 行為不變 |
| 手動 e2e（搬遷後主機） | Telegram 傳 PDF＋類別 → `/rag #類別` 得到答案；傳圖＋類別 → 描述可被檢索；`/cat list` 正確 |

## 7. 分階段落地

1. **Stage 1 — 核心（可獨立 merge）**：`categories.py` + config + `file_ingest._collection_for` + `test_categories` + `test_file_ingest`。此時可手動把檔案丟進 `inbox/categories/<slug>/` 驗證入庫。
2. **Stage 2 — 讀取端**：`RagRetrieveSkill` 的 `#cat` / `#all` + 測試。
3. **Stage 3 — Telegram 文件流**：caption `#cat` 捷徑 + `ingest_into_category` + `/cat`。
4. **Stage 4 — Telegram 圖片流 + 兩段式 PENDING**：VLM 描述寫 `.md`、PENDING 狀態機、`/cancel`、`ALWAYS_PROMPT`。
5. **Stage 5 — 文件 / README / .env.example / e2e**。

## 8. 實作結果（對照決策）

- D1：**兩段式 + caption `#類別` 捷徑**都做了。文件兩段式先進 `.staging`，逾時／`/cancel` 回退一般知識庫。
- D3：collection 命名 `oc_cat_<ascii|x>_<sha1[:8]>`，例如 `oc_cat_work-notes_1a2b3c4d` / 中文 `oc_cat_x_1a2b3c4d`。註冊表 `inbox/.openclaw/categories.json` 存顯示名。
- D4：不帶 `#` 的 `/rag` **維持現狀**（tracker + knowledge），類別為 opt-in；`/rag #all` 掃全部類別。
- D5：圖片走 `VisionClient.describe_image`（VLM 描述 + 逐字文字），寫成 `.md` 再由 watcher 入庫；原圖存 `categories/<slug>/media/`。
- D8：傳圖會多一句「回類別名可一併建索引」；傳文件（無 `#`／`/mem`）會問類別。可用 `OPENCLAW_CATEGORY_RAG_ENABLED=false` 整組關閉。
- D9：`OPENCLAW_VLM_BASE_URL` / `OPENCLAW_VLM_MODEL` / `OPENCLAW_VLM_MAX_TOKENS`（預設回退主 vLLM）；`compose.yaml` 有 `profiles:[vision]` 的 `openclaw-vllm-vision`（port 8001，預設不啟動）。

## 9. 主機驗證待辦（搬遷後環境）

1. `OPENCLAW_VLM_MODEL` 設成 VL 模型（或啟 `--profile vision` 並設 `OPENCLAW_VLM_BASE_URL`）。
2. Telegram 傳 PDF + caption `#工作筆記` → 等 ~10s → `/rag #工作筆記 這份重點` 應答得出。
3. Telegram 傳圖（無 caption）→ 回「機房」→ `/rag #機房 ...` 應檢索到描述。
4. `/cat list` 顯示兩個類別與 chunk 數。
5. 確認 `personal_knowledge_base` 沒有被 `#` 上傳污染（staging 生效）。
