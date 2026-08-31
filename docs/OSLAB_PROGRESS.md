# Qwen OS Lab Progress

## Current position

- Active milestone: local fixture/framework proof complete; overall goal blocked at Gate L
- Branch: `codex/qwen-os-lab`
- Validated primary model: `huihui-qwen3.8-27b-abliterated:latest`, Qwen3.8 27.3B Q4_K_M through Ollama 0.33.2
- Validated Qwen Code worker: 0.22.3 with wrapper model `qwen-os-lab-worker:latest`, two read-only MCP tools, and fail-closed tool-set checking
- Real OS target: absent from the bounded discovery scope; fixture work continues independently
- Next action: provide an authorized real OS source path and existing build entry point, then run `oslab target inspect --repo <AUTHORIZED_OS_SOURCE_PATH> --json`

## Ledger

| Date | Milestone | Result | Evidence |
|---|---|---|---|
| 2026-08-31 | 0 | Pass | Empty unborn repository preserved; no applicable `AGENTS.md`; branch and living documents created. |
| 2026-08-31 | 1 | Pass | Doctor recorded Windows 11 build 26200, i9-13900KF (24C/32T), 63.79 GiB RAM, RTX 4090 24 GiB, disks, WSL2, Docker, and bounded target/model discovery in `artifacts/discovery/hardware-report.json`. |
| 2026-08-31 | 2 | Pass | Pinned Python 3.12 environment, uv lock, schemas, SQLite migrations, CAS artifact store, safe process runner, CLI, and deterministic fake provider tested. |
| 2026-08-31 | 3 | Pass | Live structured Ollama response produced `{"status":"ok","sum":4}`; three-sample benchmark averaged about 45.37 output tokens/s; exact manifest metadata recorded. |
| 2026-08-31 | 4 | Pass | Typed capability broker, path/command policies, evaluator denial, transactional patch with expected hashes, rollback, receipt, and disposable worktrees tested. |
| 2026-08-31 | 5 | Pass | Source-built 512-byte fixture cold-booted in actual QEMU 7.2 TCG; PASS/FAIL/CRASH/HANG, QMP save/restore, serial artifacts, and cleanup ran successfully. |
| 2026-08-31 | 6 | External blocker | No real OS source exists in the current repository, immediate parent/children, or configured bounded development scope. The required input is one local path to the authorized OS source and its build entry point. |
| 2026-08-31 | 8/10 | Pass | Live Qwen proposed `mov al, '4'`; broker patched a disposable worktree; true QEMU baseline failed and targeted/regression runs passed; independent verifier accepted it and rejected evaluator tampering. Run `77253eed-ff3d-456f-983f-79f187e98440`. |
| 2026-08-31 | Qwen Code | Pass | Qwen Code 0.22.3 connected the local 27B model to `mcp__oslab__policy_remaining_budget`, executed exactly one successful read-only MCP call, exposed no host-shell/file tools, and returned `MCP_BUDGET_OK`. |
| 2026-08-31 | 7 | Pass | A worker subprocess exited deliberately with code 97 after six transitions, resumed from `GENERATE_TEST`, reached `COMPLETE`, retained SQLite integrity, recorded exactly one finding, and produced artifact `9dcc166333912c4ad2e5ca06597a23158c9aaa98778b41387bf3c6d74fd1f7d1`. |
| 2026-08-31 | 9 | Pass | Actual QEMU campaign `acceptance-fixture-fuzz-s101` covered pass/fail/crash/hang/seeded/induced-infra states, checkpointed six inputs, deduplicated four findings, replayed an irreducible one-byte crash input, and produced artifact `f49ba28548559c5086b824e07c61f2256942f8dd05bd744f4677d3410084dbb2`. |
| 2026-08-31 | Broker/CLI | Pass | Every required broker tool has a handler; lightweight broker contract tests pass; CLI now exposes `minimize`, `verify`, `report`, `cleanup`, `training dry-run`, and `campaign run`. |
| 2026-08-31 | 11 | Pass | Seeded evaluation completed variants A-E across seeds 1,2,3; all 15 rows reproduced the failure, accepted a patch, survived regression, and variants with adversarial verification recorded verifier artifacts. CAS artifact `f88e00dc0789d26059d863428c211b3104a9ff283f96b8337630c6518a0672aa`. |
| 2026-08-31 | 12 | Pass | Runtime assessment found only Ollama installed/benchmarked; optional runtimes documented from current upstream sources; router/scheduler tests pass. |
| 2026-08-31 | 13 | Pass | `oslab training dry-run --json` exported 16 verified JSONL/Parquet records and reloaded both formats. Summary artifact `d1ff60f41e8a450d52a5c29fab71c224b727b4cb0bfec59b720ca514d72d631c`. |
| 2026-08-31 | 14 | Pass | Added `ARCHITECTURE`, `THREAT_MODEL`, `RUNBOOK`, `MODEL_RUNTIME_REPORT`, `REAL_OS_INTEGRATION`, `TRAINING_READINESS`, `KNOWN_LIMITATIONS`, `NEXT_EXPERIMENTS`, demo scripts, and updated README. |
| 2026-09-01 | 15 | Pass except Gate L | Main checkout selftest proof `45c4ff05144aa7fae15a55ada7c74cfadbf18f8953aef7581117b0a72451af38`; clean checkout bootstrap and live selftest proof `b4937a77c10a1e40be1755ce63bd24e168b7cb52253e4e13c20200eb188d4e5a`; format checks are enforced; repeated verification leaves both checkouts clean; `oslab acceptance audit --save --json` passes and machine-checks gate evidence, proof-only post-verification changes, semantic artifact contents, artifact-index hashes, selftest proof artifacts, selftest live outputs, clean Git status, unresolved placeholders, and required support files for verified code commit `c7ec168`; audit artifact `1af7276c1b60777e8613d9d4ee68cc48a4f9058ca493bc850ae26c41aca43029`. |

| 2026-09-01 | Real target manifest hardening | Pass | Added typed `oslab-target.toml` validation, `target manifest-template`, `target validate-manifest`, and `config/oslab-target.example.toml`; unit tests prove valid manifests are accepted and public networking, path traversal, source-root escapes, missing smoke tests, branch-like base commits, and shell-eval command wrappers are rejected. |
| 2026-09-01 | Acceptance evidence hardening | Pass | `acceptance audit` now opens the recorded main and clean selftest SHA-256 artifacts, verifies their content hashes, parses the JSON PASS manifests, and checks that every expected selftest subcommand is present with exit code 0. Regression tests reject missing selftest proof blobs. |
| 2026-09-01 | Acceptance audit-artifact hardening | Pass | `acceptance audit` now opens the recorded acceptance-audit SHA-256 artifact, verifies its content hash, parses the JSON PASS audit, and requires the prior audit artifact to include every non-recursive acceptance check with no failures. Regression tests reject missing acceptance audit blobs. |
| 2026-09-01 | Live proof-output cross-check | Pass | `acceptance audit` now parses live Ollama and Qwen Code stdout from the recorded main and clean selftest artifacts and checks that model identity, structured live response, constrained Qwen Code tools, single MCP call, and smoke result agree with `PROOF.json`. Regression tests reject mismatched live smoke output. |
| 2026-09-01 | Artifact content validation | Pass | `acceptance audit` now validates the semantic shape of hardware discovery, model benchmark, runtime assessment, A-E seeded evaluation JSON/CSV/report, training JSONL/Parquet/card, and latest report artifacts. Regression tests reject malformed required artifact contents. |
| 2026-09-01 | Stale audit-artifact rejection | Pass | The recorded acceptance-audit artifact must now include the semantic artifact-content check and live selftest stdout cross-check as PASS. Regression tests reject stale audit blobs that predate those checks. |

## Remaining acceptance work

- No independently actionable fixture/framework acceptance work remains.
- Gate L remains externally blocked until a real OS source path/build entry point is supplied.
