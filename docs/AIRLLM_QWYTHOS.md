# Qwythos specialist: AirLLM route and resident diagnostic path

CyntOX can route the `fact-checker` and `critic` council roles to the locked full-BF16 Qwythos checkpoint while retaining CyntOX for every other role, synthesis, retries, safety, tools, and terminal fallback. `hybrid-airllm` is the only Qwythos routing profile and is an explicit opt-in. `single` is the tracked default and permanent rollback profile; neither the guided council smoke test nor the prompt A/B evaluation auto-promotes routing. The resident backend remains available only through explicit `model airllm smoke/qualify --backend resident` diagnostics; it is not a council or benchmark routing profile.

## Locked runtime

The only permitted specialist model is `huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated` at revision `efcc73cac15ff8fc5d46b8d41b53c22d571cf97d`. Its expected snapshot size is `19,333,096,957` bytes, precision is BF16, and context is capped at 32,768 tokens. The tracked registry is `config/models.toml`; arbitrary Hugging Face IDs are not accepted.

The heavy Python environment, Hugging Face snapshot, layer shards, file manifest, and measured VRAM data live outside Git at `%LOCALAPPDATA%\CyntOX\airllm`. Set `CYNTOX_AIRLLM_HOME` to override that location. Setup requires Python 3.12, CUDA with BF16 support, and at least 50 GiB free disk.

```powershell
.\cyntox.cmd model airllm setup --dry-run
.\cyntox.cmd model airllm setup
.\cyntox.cmd model airllm status --json --verify
.\cyntox.cmd model airllm status --require-qualified
.\cyntox.cmd model airllm smoke --backend resident
.\cyntox.cmd model airllm smoke --backend airllm
.\cyntox.cmd model airllm qualify --backend resident
.\cyntox.cmd model airllm qualify --backend airllm
```

Setup is serialized across processes. It installs the separately pinned and wheel-hashed Windows/Python 3.12 runtime, downloads only the locked revision, verifies every local file and the exact aggregate size, checks the native Qwen3.5 architecture, pre-splits the model with complete tensor accounting, builds the resident index, and proves a second reload with Hugging Face offline mode enabled. Optional FLA and causal-convolution native kernels are intentionally absent in v1.

## Running councils and jobs

```powershell
.\cyntox.cmd council --model-profile hybrid-airllm "review this plan"
.\cyntox.cmd jobs resume JOB_ID --model-profile hybrid-airllm
.\cyntox.cmd jobs retry JOB_ID --model-profile hybrid-airllm
```

The worker has no broker tools and accepts only authenticated loopback health, generation, and shutdown requests. It runs with Hugging Face offline mode after setup. Python-level outbound networking and post-start subprocess creation are denied, inherited credentials are removed, and an OS job/process group reaps descendants. This is a strong application boundary, but not a claim of kernel-enforced egress containment for arbitrary native code; use an OS sandbox or outbound firewall policy when that threat model applies.

CyntOX, Qwythos, and other provider generation are serialized for the current OS account by a machine-local, GPU-specific lease. The lease directory must remain on a local filesystem; separate OS accounts and network/shared-filesystem overrides are outside this boundary. Active build, fuzz, and QEMU work publishes separate leases so disk-heavy AirLLM startup can defer. The council keeps one Qwythos admission/session across the specialist phase, but the authenticated CUDA worker is single-use and is recycled after each successful AirLLM specialist call; this avoids long second-generation instability while preserving serial routing and cleanup evidence. A worker crash, timeout, malformed response, reasoning leak, repeated OOM, or initial specialist GPU-lease timeout produces one concise warning, terminates the worker, reacquires the GPU lease, reruns the affected role through CyntOX, and marks the council manifest `degraded`.

Generation is hard-limited to 2,048 tokens at the worker boundary. The resident diagnostic is capped at an 8,192-token total context; AirLLM retains the 32,768-token cap. The council uses compact prompts and bounded output so both calls fit the 30-minute aggregate envelope; invalid truncation falls back visibly.

## Historical local evidence (2026-09-05)

Both recorded smoke runs are record schema 3, `attempt_status=passed`, `passed=true`, `backend_kind=real`, and used the exact locked model and revision above. They were produced before the current qualification binding schema 3 implementation and source hashes settled, so they are historical measurements rather than current promotion evidence. The record schema and qualification binding schema are separate versioned structures even though both currently use the number 3. Rerun the smoke and qualification commands after all implementation changes settle.

| Backend | Immutable smoke specification | Measured result |
| --- | --- | --- |
| Resident | Prompt `Reply with exactly: Qwythos resident ready`; expected response `Qwythos resident ready`; seed `0`; `max_new_tokens=2048`; context 8,192 | 22 prompt tokens, 141 completion tokens, 48.656 s elapsed, 17,112 MiB startup peak, 17,174 MiB peak VRAM |
| AirLLM | Prompt `Reply with exactly: Qwythos airllm ready`; expected response `Qwythos airllm ready`; seed `12`; `max_new_tokens=64`; context 32,768 | 24 prompt tokens, 61 completion tokens, 580.672 s elapsed, 2 MiB startup peak, 1,998 MiB peak VRAM |

The smoke token budgets are backend-specific qualification specifications. The general worker boundary remains 2,048 new tokens; the AirLLM smoke intentionally uses only 64.

The AirLLM result is a speed caveat: 580.672 seconds for 61 completion tokens is too slow to call a fast path. It is one smoke measurement and must not be presented as a general throughput benchmark. The lower VRAM peak demonstrates the intended low-residency behavior; the resident backend remains a diagnostic comparison path rather than a public routing option.

The historical `%LOCALAPPDATA%\CyntOX\airllm\qualification-evidence.json` recorded three distinct live, real repetitions for each backend. All six run IDs passed all three required checks: lifecycle cleanup, crash/timeout/malformed/OOM fallback handling, and offline/no-egress. The resident failure/fallback checks measured 228.750–233.031 seconds; the AirLLM checks measured 185.125–186.469 seconds. The opt-in live integration invocation for `tests/integration/test_live_airllm.py` also passed both tests (`2 passed`), covering pinned offline AirLLM completion/cleanup and resident plain plus structured completion on one worker.

At the time, these passes qualified the prepared backends for further evaluation. They no longer match the current schema-3 qualification binding because bound implementation files changed, and they never promoted a model profile. A later `single` council smoke run was stopped after four completed tasks when review found that the old evaluator could accept a non-empty `must_fix` list and miss the angle-bracket placeholder `<4090-pc-ip>`. Its partial artifacts are invalid even as smoke evidence. `config/models.toml` continues to track `single` as the default.

## Qualification

Run `model airllm qualify` before relying on the backend. It performs three distinct live repetitions of worker cleanup, crash/timeout/malformed/OOM fallback handling, and offline/no-egress checks. The CLI holds one outer GPU lease for the complete run. Before each Qwythos worker starts, the harness clears and verifies Ollama residency; after the real CyntOX fallback, it requests an explicit unload and verifies the Ollama model inventory, runner processes, and recovery to the pre-fallback free-VRAM level within 30 seconds. A failed cleanup stops the run before another Qwythos worker can start and remains failed evidence. Fault probes are available only in an explicitly launched, authenticated qualification worker. They do not generate Qwythos tokens, and fake-worker CI results are persisted only as non-live diagnostics. Each attempt is written before execution, so interruption or a newer failure invalidates older passing evidence.

Qualification binding schema 3 SHA-256 binds the worker, provider, council orchestration, model registry implementation and configuration, response protocol/parser, qualification logic, process runner, GPU/resource leases, and the runtime/model/snapshot/shard locks. Missing files, unknown implementation-hash entries, legacy binding schemas, or any bound-file change invalidate the smoke record and lifecycle evidence. `model airllm status --json` reports the complete binding and fails closed instead of reusing stale evidence.

Run the existing ten-task council smoke suite in each profile:

```powershell
.\cyntox.cmd benchmark mythos --model-profile single
.\cyntox.cmd benchmark mythos --model-profile hybrid-airllm
```

These reports are explicitly `benchmark_kind=council_smoke`, `promotion_eligible=false`. They exercise topology, provider attestations, deadlines, VRAM handling, boundaries, and scoring, but their tasks guide the answer and the council scorer is not independent. They are therefore operational smoke evidence only, never capability, model-parity, or routing-promotion evidence.

Prompt adoption has a separate held-out evaluation:

```powershell
.\cyntox.cmd benchmark prompt-ab
```

It uses 20 evaluator-only cases across six fixed categories, three fixed seeds, the same Ollama model digest/configuration for v1 and v2, deterministic hard/soft validators, incremental checkpoints, and a separately bound anonymized human A/B review of all 60 pairs. Passing requires all hard gates, no candidate material defects, no category regression, at least a five-point score gain or an exact 1.0/1.0 ceiling tie, no more than ten percent median-length growth, and at least sixty percent candidate preference across the declared subjective pairs excluding ties. This validates prompt adoption only; it does not compare `single` with `hybrid-airllm` and cannot establish routing parity.

No current benchmark command writes `.oslab/cyntox/model-profile.json` or changes the default. Default resolution rejects legacy automatic records backed by council-smoke reports and falls closed to `single`. Any future default change needs an independent, held-out routing-parity design plus explicit acceptance. Passing `--model-profile single` always remains available. Live AirLLM tests are enabled with `CYNTOX_RUN_AIRLLM_LIVE=1`; ordinary CI uses the fake worker and never downloads weights.

The full hardening sequence and defensible stop condition are in `docs/QWYTHOS_IMPROVEMENT_PLAN.md`; unresolved items are tracked by priority in `docs/QWYTHOS_DEFECT_LEDGER.md`.
