# OpenClaw Arm Continuum 專案介紹

OpenClaw Arm Continuum 是一套以 Arm 架構為核心、強調地端優先、隱私優先、彈性部署的個人 AI 助理執行環境。

這個專案的目標不是只在單一硬體上跑一個聊天機器人，而是探索一種新的個人 AI 部署方式：同一套 OpenClaw runtime 可以從低功耗 edge device，一路延伸到 Arm CPU server，再到 DGX Spark / GB10 這類高效能本地 AI 工作站。

也就是說，OpenClaw 可以依照硬體能力切換不同部署型態：

- 在 DGX Spark / GB10 上，使用本機 NVIDIA GPU 與 vLLM 執行大型語言模型，建立高效能、全地端的個人 AI 系統。
- 在 Arm CPU-only server 上，使用 llama.cpp 搭配較小或 MoE 類模型，提供低功耗、長時間在線的本地 AI 助理能力。
- 在 RPi5 或小型 Arm edge device 上，作為 Telegram gateway、cron worker、RAG 管線與個人記憶入口，並把推理請求導向內網中的 vLLM server。

這樣的設計讓 OpenClaw 不被單一硬體綁住，而是形成一條從 edge 到 server 的 Arm 部署連續帶。

專案目前已完成並驗證的主要 profile 是 DGX Spark / GB10。在這個模式下，OpenClaw 可以透過 Telegram 溝通，使用本地 vLLM 回答問題，透過 Qdrant 和 Ollama 建立個人記憶與文件 RAG，並支援 Playwright 網頁查詢、Whisper 語音轉錄、圖片輸入、PDF 文件審核、cron 定時推播與 Gateway dashboard 管理。

OpenClaw Arm Continuum 的核心理念是：

```text
Same runtime, different capability tiers.
```

同一套 runtime 可以根據硬體能力調整模型大小、推理位置與自動化程度。低功耗 Arm device 可以負責常駐控制與資料管線，高效能 Arm workstation 可以負責重型本地推理，而內網 vLLM server 可以作為共享的私有推理資源。

因此，OpenClaw Arm Continuum 特別適合以下場景：

- 想要建立不依賴公有雲 API 的個人 AI 助理。
- 想把 Telegram、文件、記憶、RAG、網頁搜尋整合成地端系統。
- 想在 Arm edge device、Arm server、DGX Spark 之間彈性部署。
- 想讓 AI 助理長時間在線，但又控制功耗與資料外流風險。
- 想探索 llama.cpp、ERNIE 4.5、vLLM 等不同本地推理後端。

這個專案的長期方向，是讓個人 AI 不再只能依附於雲端 API，而是能夠部署在使用者自己掌控的 Arm 硬體環境中。從 edge 到 server，從小模型到大模型，從單機閉環到可信任內網，OpenClaw Arm Continuum 提供一個可延伸、可驗證、可持續迭代的地端 AI 助理基礎。
