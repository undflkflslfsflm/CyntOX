# Qwythos specialist improvement plan

This plan turns the full-BF16 Qwythos checkpoint into a fast, bounded council specialist without weakening CyntOX's control plane. Work is accepted only when there is executable evidence; a model's self-review is useful input, but never proof that the system is finished.

## Outcomes and gates

1. **Specialist paths:** use AirLLM as the exact 32,768-token, lower-residency specialist required by the approved profile. Keep the 8,192-token resident implementation as a faster diagnostic path, not an automatically promotable substitute.
2. **Deterministic routing:** use Qwythos only for `fact-checker` and `critic`. CyntOX retains architecture, building, testing, safety, skill creation, synthesis, scoring, retries, tools, and every terminal fallback.
3. **Isolation:** run Qwythos in an authenticated loopback-only child process, deny Python networking and subprocess creation after initialization, provide no broker or shell tools, scrub inherited secrets, and terminate descendants with the parent.
4. **Resource safety:** serialize every GPU generation across processes, publish live build/fuzz/QEMU activity leases, measure actual VRAM, unload the resident CyntOX model when headroom is inadequate, and allow exactly one AirLLM OOM retry.
5. **Reproducibility:** require Python 3.12, exact package versions and Windows wheel hashes, the exact model revision and byte count, complete tensor coverage, shard and snapshot hashes, an offline reload proof, and a serialized setup operation.
6. **Honest fallback:** record requested and actual providers, stable failure codes, every backend attempt, timing, worker identity, hashes, token counts, and VRAM evidence. Any fallback marks the run `degraded`; degraded evidence can never justify a routing change.
7. **Honest evidence labels:** retain the exact ten-task guided Mythos run as a `council_smoke` test only. It records topology, specialist attestations, hashes, timing, VRAM, boundary checks, and finite scores, but it never claims capability/parity and never writes an automatic profile selection.
8. **Lifecycle proof:** repeat success and failure cleanup tests three times; verify no worker, descendant, listener, GPU lease, resource lease, or material GPU allocation remains.
9. **Prompt quality:** use the 20-case, three-seed held-out prompt A/B harness for v1 versus v2 under the same model digest and generation configuration. Require all deterministic hard gates, no category regression, a five-point gain or exact ceiling tie, bounded length growth, and independent blind human preference. This supports prompt adoption only; it is not model or routing parity evidence.
10. **Independent final review:** after the implementation passes deterministic checks, ask the app exactly `Can I make it better?`, triage every concrete finding, then perform a separate static, unit, integration, live-model, security, resource, and process-leak debug pass.

## Verified checkpoint and historical evidence (2026-09-05)

The prepared model is exactly `huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated` at revision `efcc73cac15ff8fc5d46b8d41b53c22d571cf97d`. The verified snapshot is BF16 and `19,333,096,957` bytes. These values match `config/models.toml`, the snapshot manifest, and the historical record-schema-3 smoke files. The measured passes below predate the current qualification binding schema 3 source hashes and are therefore stale operational evidence.

| Gate | Actual evidence | State |
| --- | --- | --- |
| Resident smoke | Record schema 3, real `transformers-resident`, seed `0`, `max_new_tokens=2048`, 141 completion tokens, 48.656 s elapsed, 17,174 MiB peak VRAM | Historical pass; current binding stale |
| AirLLM smoke | Record schema 3, real `airllm`, seed `12`, `max_new_tokens=64`, 61 completion tokens, 580.672 s elapsed, 1,998 MiB peak VRAM | Historical pass; current binding stale and latency caveat |
| Resident qualification | Three distinct live/real run IDs; every run passed lifecycle cleanup, failure fallback, and no-egress | Historical 3/3; current binding stale |
| AirLLM qualification | Three distinct live/real run IDs; every run passed lifecycle cleanup, failure fallback, and no-egress | Historical 3/3; current binding stale |
| Live integration | Both opt-in tests in `tests/integration/test_live_airllm.py` passed | Historical 2/2; rerun after code freeze |
| Ten-task council smoke | `single` stopped after four tasks when evaluator defects were found; partial artifacts are invalid | Open; rerun for diagnostics only, never parity/promotion evidence |
| Held-out prompt A/B | Harness implemented; live 120-generation run and independent blind human judgments are not yet recorded | Open; prompt-adoption evidence only |
| Tracked default | `[defaults] model_profile = "single"` in `config/models.toml` | Unchanged |

The qualification evidence records these run IDs:

- Resident: `qualification-resident-1-2c4180ab2a6f`, `qualification-resident-2-147ed5322d9f`, and `qualification-resident-3-01257fe29268`.
- AirLLM: `qualification-airllm-1-12125702dba6`, `qualification-airllm-2-d2cde2309f19`, and `qualification-airllm-3-7cf5b4b24e0b`.

Resident lifecycle/no-egress attempts historically measured 45.281–50.562 seconds and failure/fallback attempts measured 228.750–233.031 seconds. AirLLM lifecycle/no-egress attempts historically measured 36.469–37.625 seconds and failure/fallback attempts measured 185.125–186.469 seconds. These are stale qualification-harness measurements; they must not be substituted for current schema-3 qualification or the unfinished ten-task quality benchmark.

## Remaining work by priority

- **P0:** none currently verified. Any later integrity, isolation, fallback-safety, or cleanup regression immediately reopens P0 and blocks reliance on the specialist.
- **P1:** after the implementation freezes, regenerate smoke and three-repeat live qualification evidence for the current schema-3 binding. Rerun the ten-task suite as council smoke diagnostics, and complete the held-out prompt A/B run plus independent blind human review before adopting v2. Neither result changes routing defaults. A separate held-out `single` versus `hybrid-airllm` design would be required before making a parity claim.
- **P2:** characterize and improve AirLLM latency. Its historical passing smoke took 580.672 seconds for 61 completion tokens; that single smoke is a warning, not a throughput benchmark. Optional native-kernel work also remains unverified.

## Stop condition

The improvement cycle stops only when two independent review passes yield no accepted P0/P1 change, current schema-3 live evidence passes, council smoke diagnostics pass, the held-out prompt A/B gates and independent review pass, all available deterministic/live/security gates pass, and the defect ledger has no unresolved P0/P1 issue. That condition is not met while current requalification and prompt evaluation are unfinished. Unsupported hardware-dependent checks stay explicitly open; they are never converted into a pass by assertion. Routing parity remains unproven until a separate independent evaluation is designed and run.

## Rollback

`--model-profile single` is permanent and bypasses Qwythos. The tracked default remains `single`, and `hybrid-airllm` is the only Qwythos routing profile. The resident backend is limited to explicit smoke and qualification diagnostics. The council smoke and prompt A/B commands never create an automatic local promotion; changing the default requires an independent routing-parity evaluation and explicit acceptance.
