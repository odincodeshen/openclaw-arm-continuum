# Three-model validation on Radxa Orion O6

This profile keeps the existing ERNIE 4.5 llama.cpp server on port 8080 as
`local_default`, then adds three CPU-only OpenAI-compatible endpoints:

| Role | Model | Port |
| --- | --- | ---: |
| Router | Qwen3-0.6B Q4 | 18001 |
| Coder | Qwen2.5-Coder-1.5B Q4 | 18002 |
| Reasoner | Qwen3-4B Q4_K_M | 18003 |

The catalog is `app/models.o6-multimodel.example.json`. Copy it to the path
specified by `OPENCLAW_MODEL_CATALOG` on the O6 host and replace the model
aliases only if the local llama.cpp server uses different names.

The model files are intentionally not checked into Git. Store them in a
private O6 model directory and run one llama.cpp server per model. Start the
servers sequentially so each process completes model loading before the next
one begins. Use a context size of 4096 initially and reduce it if memory
pressure appears.

This is a validation profile, not a claim that all three models have the same
latency or quality as the DGX deployment. If the O6 becomes memory constrained,
keep the reasoner resident and start the router or coder on demand.

## O6 validation result

On 2026-08-04, the three endpoints were started alongside the existing ERNIE
21B fallback on an Orion O6 with 30 GiB RAM. All four endpoint health checks
passed and the live `/review` workflow completed successfully:

- router: success, about 5 seconds;
- code review: success, 4 findings, 48.887 seconds;
- architecture review: success, 8 findings, 146.496 seconds;
- synthesis: success, 143.826 seconds;
- end-to-end workflow: success, about 344 seconds;
- memory after loading all four models: about 15 GiB available.

The O6 uses the existing ERNIE endpoint as `local_default`; the three new
endpoints are local llama.cpp servers on ports 18001--18003. Start them
sequentially and keep their logs and PID files outside the repository.
