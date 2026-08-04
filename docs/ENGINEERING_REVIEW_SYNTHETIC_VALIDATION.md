# Engineering Review Synthetic Endpoint Validation

## Purpose

This validation proves that the v1.3 routing and workflow implementation works
through real OpenAI-compatible HTTP endpoints before selecting and loading the
final DGX Spark model set.

Synthetic validation does not prove model quality, model capacity, or final
latency. Those require the separate real-model validation stage.

## Validated Environment

- Platform: NVIDIA DGX Spark / GB10
- Architecture: Arm64
- Python: 3.12
- Endpoint transport: HTTP on random `127.0.0.1` ports
- Fixture: `examples/engineering-review/sanitized-design-package.md`
- Validation date: 2026-07-31

No public cloud model API was used. The test did not modify or stop the
existing OpenClaw deployment or its vLLM endpoint.

## Test Shape

The integration test starts four temporary OpenAI-compatible endpoints:

```text
local_router
local_coder
local_reasoner
local_default
```

It then validates:

```text
sanitized engineering fixture
  -> structured router response
  -> code review subtask
  -> architecture review subtask
  -> synthesis subtask
  -> endpoint and agent history
```

The test stops the coding endpoint and repeats the review to validate:

```text
local_coder unavailable
  -> local_default fallback
  -> successful structured code result
  -> fallback_from recorded in task history
```

All temporary HTTP servers bind to random loopback ports and shut down at the
end of the test.

## Commands

Run only the synthetic integration test:

```bash
PYTHONPATH=app:tests python3 -m unittest -v test_engineering_review_integration
```

Run the complete regression suite:

```bash
PYTHONPATH=app python3 -m unittest discover -s tests
```

## Recorded Result

Synthetic integration:

```text
test_real_http_workflow_and_coder_fallback ... ok

Ran 1 test
OK
```

Complete DGX Spark regression:

```text
Ran 145 tests
OK (skipped=5)
```

After validation, the pre-existing local vLLM and Gateway endpoints remained
healthy and all existing OpenClaw containers remained running.

## Remaining Real-Model Validation

Before creating the `v1.3` tag:

1. Select the router, coding, and reasoning models.
2. Define context, concurrency, and unified-memory budgets.
3. Start every selected local endpoint on DGX Spark.
4. Verify `/v1/models` and `/v1/chat/completions` for every endpoint.
5. Run the same sanitized engineering fixture with real model output.
6. Validate structured-output reliability and model quality.
7. Stop one real specialist endpoint and verify fallback or degradation.
8. Measure end-to-end and per-subtask latency.
9. Repeat the complete v1.2 baseline regression suite.
