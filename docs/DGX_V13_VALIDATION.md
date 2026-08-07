# DGX Multi-Model Validation

## Scope

This validation used the isolated deployment directory
`/home/lpreview/openclaw-arm-continuum-v1.3` on the DGX GB10 host. Existing
OpenClaw and unrelated containers were preserved.

The deployment was based on the three-endpoint validation Compose pattern and
used an independent Compose project with these container names:

```text
openclaw-dgx-router
openclaw-dgx-coder
openclaw-dgx-reasoner
```

## Model configuration

```text
Router   Qwen/Qwen3-8B                 max context 4096
Coder    Qwen/Qwen2.5-Coder-7B-Instruct max context 8192
Reasoner Qwen/Qwen3.6-27B-FP8          max context 16384
```

Initial GPU memory budgets were adjusted after startup testing:

```text
Router   0.20
Coder    0.18
Reasoner 0.40
```

The models were exposed through local OpenAI-compatible endpoints on ports
18001, 18002, and 18003 respectively.

## Validation results

The Router smoke test returned `READY`. The complete engineering-review
workflow then completed successfully:

```text
WORKFLOW_STATUS=success
WORKFLOW_SECONDS=319.244
```

Observed subtasks:

```text
code_review         success, 5 findings,  39.070 seconds
architecture_review success, 8 findings, 133.558 seconds
synthesis           success,              142.712 seconds
```

The workflow used the expected endpoint mapping:

```text
code_review         -> local_coder
architecture_review -> local_reasoner
synthesis           -> local_reasoner
```

The final report contained critical findings, recommendations, open
questions, missing tests, release-readiness concerns, and limitations.

## Startup and resource observations

The three models must not be initialized concurrently on this GB10 system.
Concurrent initialization caused vLLM memory-profiling races and CUDA OOM
failures. The stable sequence was:

```text
Reasoner healthy -> Router healthy -> Coder healthy
```

After all three models were resident and the workflow completed, available
system memory fell to approximately 12 GiB and swap usage reached about 7.7
GiB. This configuration is therefore suitable for functional validation, but
not yet recommended as a long-running production service without further
memory reduction or scheduling controls.

## Cleanup

After validation, the isolated project was stopped with:

```bash
docker compose --env-file .env.dgx-validation \
  -f compose.dgx-validation.yaml down
```

The three validation containers and their Compose network were removed. The
unrelated DGX containers were not changed. Post-cleanup memory returned to
approximately 113 GiB available.
