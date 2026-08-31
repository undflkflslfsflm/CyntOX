# Qwen OS Lab Execution Plan

## Objective

Build and verify a local, safety-bounded OS reliability lab driven by the installed Qwen model. The external supervisor—not the model—owns policy, budgets, persistence, and acceptance decisions.

## Workspace facts

- Project root: `C:\Users\vikto\Documents\ChatGPT\bob (qwen remodeled to act as mythos)`
- Repository state at start: empty Git repository, no commits, no user files
- Working branch: `codex/qwen-os-lab`
- Repository role: dedicated orchestration repository
- Real OS target: not yet discovered

## Milestones

| Milestone | State | Evidence / next action |
|---|---|---|
| 0. Preserve work and plan | Complete | Empty repository and absence of applicable `AGENTS.md` confirmed; branch and living documents created. Initial worktree testing follows after the baseline commit exists. |
| 1. Discovery/bootstrap | Pending | Implement and execute cross-platform doctor; save JSON/TOML/Markdown reports. |
| 2. Core | Pending | Config, schemas, SQLite migrations, artifact store, process runner, CLI, fake provider. |
| 3. Live Qwen | Pending | Detect exact endpoint/model; structured smoke, throughput, context and concurrency evidence. |
| 4. Tool broker | Pending | Typed envelopes, policy, transactional patch receipts and rollback. |
| 5. QEMU fixture | Pending | Build freestanding fixture; real boot/QMP/serial/snapshot/failure modes. |
| 6. Real target | Pending | Inspect and integrate only when an authorized buildable OS source is found. |
| 7. Supervisor | Pending | Transactional state machine, cancellation, recovery, fingerprints. |
| 8. Fix loop | Pending | Live-Qwen hypothesis/experiment/patch/regression workflow. |
| 9. Fuzzing | Pending | Seeded bounded fixture campaign, checkpoint, replay, minimize, coverage. |
| 10. Adaptive/verifier | Pending | Role prompts, adaptive populations, independent verifier, exploit detection. |
| 11. Evaluation | Pending | Variants A–E, three seeds, raw JSON/CSV and honest report. |
| 12. Runtime routing | Pending | Benchmark installed runtime; assess optional alternatives without downloads. |
| 13. Training readiness | Pending | Verified trajectory exports and a small non-destructive dry run. |
| 14. Documentation/security | Pending | Runbook, architecture, threat model, bootstrap/demo, cleanup. |
| 15. Acceptance | Pending | Execute gates, repair failures, create proof bundle and integrity index. |

## Validation contract

Every completed milestone must have an executed command, exit code, and content-addressed evidence. Optional or unavailable capabilities are classified explicitly. A mandatory gate is never satisfied by a mock or unexecuted adapter.

## Resume

From the project root, inspect `docs/OSLAB_PROGRESS.md`, then run the next command recorded there.
