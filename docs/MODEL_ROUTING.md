# Model routing

Two independent layers:

1. **Which handler** takes a message — deterministic, unchanged.
2. **Which model** that handler talks to — the catalog.

## 1. Handler routing (deterministic)

Order, highest priority first:

1. Explicit slash command (`/rag`, `/mem`, `/search`, `/cron`, `/review`, …).
2. Skill keyword match (e.g. a plain "天氣" question → weather).
3. **Optional** LLM intent classifier — only for messages that matched
   nothing above, and only when a `local_router` model is configured.
4. Chat model.

Steps 1–2 and 4 have always existed and are covered by
`tests/test_routing_integration.py`. Step 3 is opt-in and never overrides an
explicit command or a keyword match.

### LLM intent classifier (step 3)

`OPENCLAW_INTENT_ROUTER_ENABLED=true` (default) + a catalog `local_router`
model. It classifies a plain-language message as `knowledge_base`,
`web_search`, or `chat`; a `knowledge_base` / `web_search` result above
`OPENCLAW_INTENT_ROUTER_MIN_CONFIDENCE` (default 0.6) is sent to the RAG or
web-search skill instead of straight to the chat model. Any error, low
confidence, or `chat` result → falls through to the chat model as before.

Without a `local_router` model in `models.json`, this layer does nothing.

## 2. Model catalog (`models.json`)

`OPENCLAW_MODEL_CATALOG` (default `/app/models.json`). Absent → OpenClaw runs
on `OPENCLAW_VLLM_*` as `local_default`, plus a `vision` entry synthesised
from `OPENCLAW_VLM_*`.

```json
{
  "models": {
    "local_default": { "base_url": "...", "model": "...", "roles": ["general"], "timeout": 60 },
    "vision":        { "base_url": "...", "model": "...", "roles": ["vision"], "timeout": 120, "fallback": "local_default" },
    "local_router":  { "base_url": "...", "model": "...", "roles": ["classification"], "timeout": 30, "fallback": "local_default" }
  }
}
```

Roles: `general`, `vision`, `classification`, `code_review`,
`architecture_review`, `synthesis`. Each model may name a `fallback` model
used when its endpoint fails.

### Pinning a skill to a model

Add `model_policy` (a catalog model id) to a skill in `skills.json`:

```json
"rag_retrieve": { "enabled": true, "keywords": ["/rag", "rag:"], "model_policy": "local_reasoner" }
```

Resolved through the catalog, falling back to `local_default` if the id is
unknown. `/agents` shows the endpoint each skill resolved to.

### Who uses which model

| Consumer | Model |
|---|---|
| Chat, unpinned skills | `local_default` |
| Photo description (Category RAG) | `vision` |
| Intent classifier | `local_router` |
| `/review` router → specialists → synthesis | `local_router` → `local_coder` / `local_reasoner` → `local_reasoner` |
| A skill with `model_policy` | that model |

## Deploying multiple endpoints

`app/models.dgx-multimodel.example.json` and `compose.dgx-validation.yaml`
show a DGX layout (router 8B, coder 7B, reasoner 27B). Start endpoints
sequentially — concurrent vLLM memory-profiling can interfere. See
`docs/DGX_V13_VALIDATION.md` and `docs/O6_MULTIMODEL.md`.
