# Qwythos defect ledger

Evidence cutoff: 2026-09-05 02:59 CEST. This ledger distinguishes historical measurements, council smoke diagnostics, prompt-adoption evidence, and routing-parity evidence. None of those labels is interchangeable: a smoke or qualification pass does not prove capability/parity, and prompt A/B does not compare routing profiles.

## Historical verified measurements

- The locked checkpoint is `huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated` at revision `efcc73cac15ff8fc5d46b8d41b53c22d571cf97d`, BF16, expected and measured snapshot size `19,333,096,957` bytes.
- Both real backend smoke records use record schema 3 and historically passed with verified hashes. The resident smoke used seed `0` and `max_new_tokens=2048`; the AirLLM smoke used seed `12` and `max_new_tokens=64`.
- The resident smoke recorded 22 prompt tokens, 141 completion tokens, 48.656 seconds elapsed, 17,112 MiB startup peak and 17,174 MiB peak VRAM. The AirLLM smoke recorded 24 prompt tokens, 61 completion tokens, 580.672 seconds elapsed, 2 MiB startup peak and 1,998 MiB peak VRAM.
- The historical live qualification evidence has three distinct passing repetitions for each backend. Every repetition passed `lifecycle_cleanup`, `failure_fallback`, and `no_egress` with `backend_kind=real` and `live=true`.
- The historical opt-in live integration invocation for `tests/integration/test_live_airllm.py` passed both tests (`2 passed`): pinned offline AirLLM completion/cleanup, and resident plain plus structured completion on one worker.
- `config/models.toml` still tracks `model_profile = "single"`. No local smoke or qualification result changes that tracked default.
- The earlier Windows resource-activity publication fix serialized publication/read access per resource kind, retried raw sharing violations, kept heartbeat threads alive through transient conflicts, and used release tombstones when deletion was blocked. That implementation passed 11 focused tests, including dense-reader and cleanup-exhaustion regressions. Treat this as historical evidence: subsequent admission-barrier work changes the bound lease implementation and requires a current deterministic rerun.

Evidence files are `%LOCALAPPDATA%\CyntOX\airllm\qualification-resident.json`, `%LOCALAPPDATA%\CyntOX\airllm\qualification.json`, `%LOCALAPPDATA%\CyntOX\airllm\qualification-evidence.json`, `%LOCALAPPDATA%\CyntOX\airllm\snapshot-manifest.json`, and `config/models.toml`. The saved smoke and qualification results are stale because the current qualification binding is schema 3 with different bound implementation hashes; retain them only as historical performance and lifecycle measurements.

## Unresolved P0

None is currently verified. Any model-integrity, isolation, unsafe-fallback, or cleanup failure discovered later is P0 and blocks reliance on the specialist immediately.

## Unresolved P1

### P1-001 — Current schema-3 runtime evidence must be regenerated

The worker, provider, council, promotion evaluator, process runner, and lease implementation changed after the historical live runs. Because those sources are included in qualification binding schema 3, the old smoke and qualification files cannot qualify the current implementation. Resolution requires new resident and AirLLM smoke records plus three current live qualification repetitions for the applicable backend after the code freezes.

### P1-002 — Independent evaluation evidence is unfinished

The attempted current `single` ten-task run was deliberately stopped after four completed tasks because the evaluator could accept a non-empty `must_fix` list and failed to detect `<4090-pc-ip>` as an unresolved placeholder. Those partial manifests are invalid even as council-smoke evidence, and the evaluator changes make their fingerprint stale. The scorer now requires complete bound scorecards with `must_fix`, `contradictions`, `invented_actions`, and `known_defects`; any reported defect blocks the gate and a contradictory 9+ verdict is capped below 9. The suite still needs a fresh diagnostic run.

Prompt adoption separately requires the held-out 20-case, three-seed prompt A/B run with identical model/configuration bindings, deterministic hard gates, incremental evidence, and independent anonymized human preferences. That report validates the v2 prompt only. The guided ten-task run is now labeled `council_smoke`, is never capability/parity evidence, and always sets `promotion_eligible=false`. No current benchmark command auto-promotes the `hybrid-airllm` profile. The tracked default remains `single`; a routing-parity claim would require a separate independent evaluation.

## Unresolved P2

### P2-001 — AirLLM is a low-residency path, not a demonstrated fast path

The historical AirLLM smoke took 580.672 seconds for 61 completion tokens. This is a single bounded smoke measurement, not a statistically valid throughput benchmark, but it is enough to require a latency warning. AirLLM should remain an explicit opt-in route until current qualification and independent comparative latency data exist; the resident backend remains a practical diagnostic fast path.

### P2-002 — Optional native-kernel performance work remains open

The verified snapshot records `native_optional_kernels=false`; the v1 scope specifically leaves FLA and causal-convolution kernels absent. This did not prevent smoke, qualification, or the two live integration tests from passing. Installing or benchmarking optional kernels is optimization work and must not be described as already completed.
