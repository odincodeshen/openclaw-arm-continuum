# Vision (VLM) setup

OpenClaw runs every image through one seam, `VisionClient`, which is bound to
the catalog model with role `vision`:

- Telegram photo messages (`process_image_message`) -> interactive analysis.
- Category RAG image uploads (`ingest_image_into_category`) -> indexed description.

Both call the same resolved endpoint. If no dedicated vision model is
configured, `vision` falls back to `local_default` (the main text model), and
image quality is poor or unusable.

## 1. Pick how to point `vision` at a VLM

### Option A -- `models.json` (preferred, same catalog as routing)

Add a model with role `vision` to `app/models.json`
(see `app/models.example.json`):

```json
{
  "models": {
    "local_default": { "base_url": "http://openclaw-vllm:8000/v1", "model": "<text-model>", "roles": ["general"], "timeout": 60 },
    "vision": {
      "base_url": "http://openclaw-vllm-vision:8000/v1",
      "model": "Qwen/Qwen2.5-VL-7B-Instruct",
      "roles": ["vision"],
      "timeout": 120,
      "fallback": "local_default"
    }
  }
}
```

### Option B -- `OPENCLAW_VLM_*` shortcut (no `models.json`)

If `models.json` is absent, OpenClaw synthesises a `vision` entry from these:

```env
OPENCLAW_VLM_BASE_URL=http://gb10.local:8000/v1
OPENCLAW_VLM_MODEL=Qwen/Qwen2.5-VL-7B-Instruct
OPENCLAW_VLM_MAX_TOKENS=500
```

Leave them empty to fall back to the main text model (`OPENCLAW_VLLM_*`).

### Option C -- bundled second vLLM (`vision` compose profile)

Runs a dedicated VLM container alongside the text vLLM on the same GPU:

```bash
docker compose --profile vision up -d openclaw-vllm-vision
```

Then set `OPENCLAW_VLM_BASE_URL=http://openclaw-vllm-vision:8000/v1` (Option B)
or a `models.json` `vision` entry pointing at it (Option A). Tune the model and
GPU split with `OPENCLAW_VLM_VISION_MODEL`,
`OPENCLAW_VLM_VISION_GPU_MEMORY_UTILIZATION`, `OPENCLAW_VLM_VISION_MAX_MODEL_LEN`.

Candidate VLM families: Qwen2.5-VL, Qwen3-VL, Llama Vision.

## 2. Verify before enabling it for users

```bash
docker compose exec openclaw-telegram python scripts/vision_smoke.py
# or, for a real quality check, with your own image:
docker compose exec openclaw-telegram python scripts/vision_smoke.py /path/to/photo.jpg
```

The script builds `VisionClient` exactly as the gateway does and prints the
resolved endpoint, whether it is reachable, and the description it got back. It
warns loudly if `vision` still resolves to the text model, and exits non-zero
if the call fails.

## 3. Enable and restart

```env
OPENCLAW_VISION_ENABLED=true
OPENCLAW_VISION_MAX_TOKENS=500
```

```bash
docker compose restart openclaw-telegram openclaw-memory-watcher
```

Module-level init (settings, catalog, `VisionClient`) only runs at container
start, so a restart is required even though `./app` is mounted live.

## 4. Smoke test in Telegram

- Send a photo with a caption like `Read out the text in this image`.
- Send a photo with the caption `#<category>` -> it is described and indexed
  under that Category RAG collection; `/rag #<category> ...` should retrieve it.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `vision_smoke.py` prints the "resolves to the same endpoint" warning | No dedicated VLM configured | Do step 1 |
| `FAIL: ... returned an empty description` | Text-only model can't accept images, or returned only reasoning | Use a real VLM; check `chat_template_kwargs` support |
| Telegram: "OPENCLAW_VISION_ENABLED=false" | Vision disabled | Step 3 |
| Telegram: "the vision model failed to process the image input" | Endpoint unreachable or rejected the request | Re-run `vision_smoke.py`, check the vLLM logs |
