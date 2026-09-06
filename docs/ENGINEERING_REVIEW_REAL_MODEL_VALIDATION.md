# Engineering Review DGX Real-Model Validation

Validation date: 2026-07-31

Target: NVIDIA DGX Spark (`aarch64`, GB10, 119 GiB system memory)

Scope: local-only structured routing, specialist review, synthesis, fallback, and regression testing

## Model allocation

| Policy | Model | Context | Memory fraction | Endpoint |
| --- | --- | ---: | ---: | ---: |
| `local_router` | `Qwen/Qwen3-0.6B` | 4,096 | 0.06 | 18001 |
| `local_coder` | `Qwen/Qwen2.5-Coder-7B-Instruct` | 8,192 | 0.18 | 18002 |
| `local_reasoner` / `local_default` | `Qwen/Qwen3.6-27B-FP8` | 16,384 | 0.45 | 18003 |

All endpoints used the NVIDIA vLLM `26.03.post1-py3` image. Router and coder used eager execution. The 27B reasoner used the compiled path because eager execution failed during CUDA convolution engine selection on this platform.

Start these endpoints sequentially. Starting multiple vLLM instances at the same time can make their memory-profiling phases interfere with one another.

## Validation results

### Normal workflow

The sanitized engineering fixture completed through structured routing, code review, architecture review, and synthesis.

| Stage | Endpoint | Result | Duration |
| --- | --- | --- | ---: |
| Code review | `local_coder` | success, 5 findings | 37.683 s |
| Architecture review | `local_reasoner` | success, 8 findings | 138.143 s |
| Synthesis | `local_reasoner` | success | 165.625 s |
| Full workflow | all local endpoints | success | 342.126 s |

The first attempt exposed JSON truncation in the architecture specialist. The specialist output was bounded to eight findings, five questions, and five limitations; its token budget was raised from 1,200 to 1,600; and JSON parse failures were made eligible for configured fallback. The repeated workflow then completed successfully.

The synthesis prompt is also bounded to fewer than 1,000 words and five bullets per section. This avoids an incomplete report when specialist outputs contain many overlapping findings.

### Coder failure and fallback

The `local_coder` container was stopped before running the same complete workflow. Code review automatically moved to `local_default`, while architecture and synthesis continued on `local_reasoner`.

| Stage | Endpoint | Result | Duration |
| --- | --- | --- | ---: |
| Code review | `local_default` | success, 8 findings | 133.303 s |
| Architecture review | `local_reasoner` | success, 8 findings | 138.190 s |
| Synthesis | `local_reasoner` | success | 183.264 s |
| Full degraded-route workflow | all available local endpoints | success | 455.435 s |

Task history recorded `fallback_from: local_coder` and `endpoint_id: local_default`. The coder service was restored after the test.

### Regression suite

The synchronized DGX worktree passed all 146 discovered tests. Five browser-scraper tests were skipped because Playwright is not installed in the validation environment; there were no failures or errors.

## Operational notes

- The previous v1.2 baseline containers were stopped, not deleted. Their data and caches remain available.
- The engineering-review validation stack was isolated under a dedicated test directory.
- Model traffic stayed on the DGX host; the validation fixture contains no secrets or production source.
- Expect slower completion when a specialist falls back to the 27B default model. This is service continuity, not equivalent latency.
- Preserve at least roughly 25 GiB available system memory for stable coexistence of these three endpoints.

## Outcome

The v1.3 engineering-review workflow passed real-model functional validation for structured dispatch, bounded specialist execution, synthesis, task-history observability, and configured endpoint fallback. Human approval, production security review, and workload-specific performance testing remain separate release gates.
