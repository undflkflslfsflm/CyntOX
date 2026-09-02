# CyntOX OS Lab Execution Plan

## Objective and boundary

Build and verify a local, safety-bounded OS reliability lab driven by the installed CyntOX model. The external supervisor—not the model—owns policy, budgets, persistence, evaluator integrity, and acceptance. The model receives only typed broker capabilities; QEMU guest networking is disabled.

## Measured workspace facts

- Root: `<repo>`
- Branch: `codex/cyntox-os-lab`; repository role: dedicated orchestrator
- Host: Windows 11 Pro build 26200, i9-13900KF, 63.79 GiB RAM, RTX 4090 24 GiB
- Isolation: Docker Desktop/WSL2; QEMU 7.2 TCG in a pinned Debian container
- Model: CyntOX 27B 27.3B Q4_K_M GGUF, Ollama 0.33.2, loopback only
- Real OS target: MIT-licensed `cyntox-open-os-target` present in the bounded discovery scope and smoke-tested through QEMU
- Daily assistant overlay: CyntOX CLI/CyntOX launcher now has compact terminal output defaults, shared compact JSON with a whole-response cap, and terminal-flood hook coverage; latest local stress gates pass except external Docker/Git warnings.

## Milestones

| Milestone | State | Evidence / next action |
|---|---|---|
| 0. Preserve work and plan | Complete | Branch, baseline commit, no applicable instructions. |
| 1. Discovery/bootstrap | Complete | Machine-readable, Markdown, and TOML reports generated from live probes. |
| 2. Core | Complete | Pinned typed core and safe fake-provider tests pass. |
| 3. Live CyntOX | Complete | Exact identity, structured smoke, and resident throughput benchmark saved. |
| 4. Tool broker | Complete | Transactional patch, rollback, receipts, immutable evaluator and policy tests pass. |
| 5. QEMU fixture | Complete | Actual cold boots, QMP, snapshot restore and controlled failures pass. |
| 6. Real target | Complete | MIT-licensed `cyntox-open-os-target` is a separate Git repository with a valid manifest pinned to commit `db7b591789313d6288584c223e954e6f52880d11`. The lab built it from a detached disposable worktree, booted it through Docker-backed QEMU with `-nic none`, and verified serial `READY`/`PASS` smoke output. |
| 7. Supervisor | Complete | Controlled exit 97, database recovery, full state path, one idempotent finding, and CAS proof passed. |
| 8. Fix loop | Complete | Live CyntOX + disposable worktree + actual QEMU fix/regression loop passed. |
| 9. Fuzzing | Complete | Six actual QEMU protocol modes, checkpoint/resume data, deduplication, stable replay, and minimal input passed. |
| 10. Adaptive/verifier | Complete for fixture | Population prompts, adaptive variant and independent live verifier; invalid evaluator edit rejected. |
| 11. Evaluation | Complete | A-E variants ran for seeds 1-3 with 15 accepted fixture repairs, raw JSON/CSV, and report saved. |
| 12. Runtime routing | Complete | `docs/MODEL_RUNTIME_REPORT.md`, `artifacts/discovery/runtime-assessment.json`, `ModelRouter`, and `ResourceScheduler` added. |
| 13. Training readiness | Complete | `oslab training dry-run --json` exported and reloaded 16 JSONL/Parquet records. |
| 14. Documentation/security | Complete | Architecture, threat model, runbook, real-target, runtime, training, limitations, next experiments, README, and demo scripts added. |
| 15. Acceptance | Complete | Main checkout and clean checkout live selftests passed at verified source commit `6151eb0`; Gate L is now backed by the open-source real-target smoke report, refreshed `PROOF.json`, refreshed Gate L report, and refreshed requirements trace. Acceptance logic now supports the completed Gate L state instead of requiring the old blocker. |

## Resume

Run `git status --short`, read `docs/OSLAB_PROGRESS.md`, then continue with broader stress coverage or proof refresh work as needed. The current Gate L target can be rechecked with `oslab target inspect --repo C:\Users\vikto\Documents\ChatGPT\cyntox-open-os-target --json`.
