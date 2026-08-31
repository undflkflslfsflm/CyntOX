# Qwen OS Lab Progress

## Current position

- Active milestone: 11–13 — evaluation, runtime assessment, and training dry run
- Branch: `codex/qwen-os-lab`
- Validated primary model: `huihui-qwen3.8-27b-abliterated:latest`, Qwen3.8 27.3B Q4_K_M through Ollama 0.33.2
- Validated Qwen Code worker: 0.22.3 with wrapper model `qwen-os-lab-worker:latest`, two read-only MCP tools, and fail-closed tool-set checking
- Real OS target: absent from the bounded discovery scope; fixture work continues independently
- Next action: finish persistent campaign recovery, bounded fuzz/evaluation CLIs, runtime report, training dry run, and acceptance bundle

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

## Remaining acceptance work

- Run variants A–E with seeds 1–3 and save raw JSON/CSV plus the comparative report.
- Finish runtime assessment, training export dry run, complete documentation, and final clean-checkout proof bundle.
- Gate L remains externally blocked until a real OS source path/build entry point is supplied.
