# Category RAG — manual test script

Run these on the host after deploying the `dev` branch. Each step lists the
action and the expected result. Stop and note any deviation.

## 0. Preconditions

```bash
# in the running repo dir
git -C . branch --show-current            # -> dev
docker compose ps                          # gateway / memory-watcher / qdrant / vllm all Up
```

Vision model — pick one:

- **A (simplest):** set `OPENCLAW_VLM_MODEL` in `.env` to a VL model
  (e.g. `Qwen/Qwen2.5-VL-7B-Instruct`) and restart `openclaw-vllm`.
- **B (dedicated):**
  ```bash
  docker compose --profile vision up -d openclaw-vllm-vision
  # add to .env:
  #   OPENCLAW_VLM_BASE_URL=http://openclaw-vllm-vision:8000/v1
  #   OPENCLAW_VLM_MODEL=Qwen/Qwen2.5-VL-7B-Instruct
  docker compose up -d openclaw-telegram
  ```

Confirm the vision endpoint answers:

```bash
docker compose exec openclaw-telegram \
  python -c "from openclaw_runtime.config import load_settings; s=load_settings(); print(s.vlm_base_url, s.vlm_model)"
curl -s http://127.0.0.1:8000/v1/models | head -c 200      # or :8001 for profile B
```

Baseline snapshot (to prove isolation later):

```bash
curl -s http://127.0.0.1:6333/collections | python -m json.tool
```

Note the current `points_count` of `personal_knowledge_base`.

---

## 1. Caption shortcut — document

1. In Telegram, send a small PDF (or `.md`/`.txt`) with caption:
   `#工作筆記`
2. **Expect:** a reply saying it is being filed into category「工作筆記」,
   then within ~10 s a second reply: `Queued for indexing: <file>` and a
   hint `/rag #工作筆記 ...`.
3. Check the collection was created:
   ```bash
   curl -s http://127.0.0.1:6333/collections | grep -o 'oc_cat_[a-z0-9_-]*'
   ```
   **Expect:** an `oc_cat_x_<hash>` entry.
4. Query it:
   `/rag #工作筆記 這份文件在講什麼？`
   **Expect:** an answer grounded in the PDF's content.
5. `/rag #工作筆記 <a question the PDF cannot answer>`
   **Expect:** the model says the context is insufficient (not a hallucinated answer).

## 2. Caption shortcut — document with a note and spaces in the name

1. Send another document with caption:
   `#[Arm Manuals] this is the v9 debug architecture doc`
2. **Expect:** filed into category「Arm Manuals」.
3. `/rag #[Arm Manuals] summarise the key points`
   **Expect:** grounded answer.

## 3. Two-step flow — document

1. Send a document with **no caption**.
2. **Expect:** reply asking which knowledge category it should go in
   (mentions `/cancel`).
3. Reply with a plain message: `讀書心得`
4. **Expect:** "Filing ... 「讀書心得」" then the queued/indexed reply.
5. `/rag #讀書心得 <question about that doc>` → grounded answer.

## 4. Two-step flow — /cancel

1. Send a document with no caption.
2. Reply `/cancel`.
3. **Expect:** "cancelled ... goes to the general knowledge base".
4. After ~10–20 s: `/rag <question about that doc>` (no `#`)
   **Expect:** answer found — it landed in `personal_knowledge_base`.

## 5. Two-step flow — timeout fallback

1. Send a document with no caption. Do **not** reply.
2. Wait `OPENCLAW_CATEGORY_PENDING_TTL_SECONDS` (default 600 s; lower it in
   `.env` to ~60 for the test and restart the gateway).
3. Send any unrelated message (e.g. `hi`) to trigger the sweep.
4. **Expect:** a message that no category was given in time and the document
   was filed into the general knowledge base.

## 6. Caption shortcut — image

1. Send a photo (something with visible text/labels — a diagram or a rack
   photo) with caption: `#機房`
2. **Expect:** the usual image-analysis reply, **plus** a category reply
   once the VLM description is generated.
3. Check the stored artefacts:
   ```bash
   ls workspace/inbox/categories/*/            # a <name>.md description file
   ls workspace/inbox/categories/*/media/      # the original image
   cat workspace/inbox/categories/*/*.md.meta.json
   ```
4. `/rag #機房 描述這張圖裡有什麼`
   **Expect:** an answer derived from the VLM description (mentions objects /
   visible text from the photo).

## 7. Two-step flow — image

1. Send a photo with **no** caption.
2. **Expect:** analysis reply + a prompt: "reply with a category name (or
   /cancel to skip)".
3. Reply `巡檢照片`.
4. **Expect:** category ingest reply. Then `/rag #巡檢照片 ...` works.
5. Repeat but reply `/cancel` → **expect** no category ingest, image stays
   only as a normal analysed photo.

## 8. /cat

1. `/cat list`
   **Expect:** every category created above, each with a chunk count and its
   `oc_cat_...` collection name.
2. `/cat help` → usage text.

## 9. /rag #all

1. `/rag #all 有沒有關於機櫃或伺服器的資料`
   **Expect:** an answer that pulls from the image categories (機房 /
   巡檢照片) but a plain `/rag` of the same question does **not** surface
   that image content.

## 10. Isolation check (the whole point)

1. Pick a distinctive fact that exists **only** in the「工作筆記」PDF.
2. `/rag #讀書心得 <that fact>`
   **Expect:** not found / insufficient context — it must not leak across
   categories.
3. `/rag <that fact>` (no `#`)
   **Expect:** not found either — category content is not in the default
   collections.
4. Compare `personal_knowledge_base` `points_count` to the step-0 baseline:
   ```bash
   curl -s http://127.0.0.1:6333/collections/personal_knowledge_base | python -m json.tool
   ```
   **Expect:** only grew by the step 4 (/cancel) and step 5 (timeout)
   documents — never by the `#`-captioned uploads.

## 11. Regression — existing flows untouched

- `/rag <normal question>` about previously-indexed docs still works.
- Upload a document with caption `/mem` → still goes to tracker memory,
  **no** category prompt.
- `/mem some text`, `/search ...`, `/cron list`, `/doc url ...` all behave
  as before.
- A photo with a caption that is a plain instruction (no `#`) still gets
  analysed; you can `/cancel` the category prompt.

## Cleanup (optional)

```bash
# delete test category collections
for c in $(curl -s http://127.0.0.1:6333/collections | grep -o 'oc_cat_[a-z0-9_-]*'); do
  curl -s -X DELETE "http://127.0.0.1:6333/collections/$c"
done
rm -rf workspace/inbox/categories workspace/inbox/.staging workspace/inbox/.openclaw/categories.json
# then restart the watcher so its state file forgets the removed files
docker compose restart openclaw-memory-watcher
```
