# Qwen OS Lab Execution Plan

## Objective and boundary

Build and verify a local, safety-bounded OS reliability lab driven by the installed Qwen model. The external supervisor—not the model—owns policy, budgets, persistence, evaluator integrity, and acceptance. The model receives only typed broker capabilities; QEMU guest networking is disabled.

## Measured workspace facts

- Root: `C:\Users\vikto\Documents\ChatGPT\bob (qwen remodeled to act as mythos)`
- Branch: `codex/qwen-os-lab`; repository role: dedicated orchestrator
- Host: Windows 11 Pro build 26200, i9-13900KF, 63.79 GiB RAM, RTX 4090 24 GiB
- Isolation: Docker Desktop/WSL2; QEMU 7.2 TCG in a pinned Debian container
- Model: Qwen3.8 27.3B Q4_K_M GGUF, Ollama 0.33.2, loopback only
- Real OS target: not present in the permitted bounded discovery scope

## Milestones

| Milestone | State | Evidence / next action |
|---|---|---|
| 0. Preserve work and plan | Complete | Branch, baseline commit, no applicable instructions. |
| 1. Discovery/bootstrap | Complete | Machine-readable, Markdown, and TOML reports generated from live probes. |
| 2. Core | Complete | Pinned typed core and safe fake-provider tests pass. |
| 3. Live Qwen | Complete | Exact identity, structured smoke, and resident throughput benchmark saved. |
| 4. Tool broker | Complete | Transactional patch, rollback, receipts, immutable evaluator and policy tests pass. |
| 5. QEMU fixture | Complete | Actual cold boots, QMP, snapshot restore and controlled failures pass. |
| 6. Real target | Blocked externally | Finish framework/adapter report; resume needs authorized source path plus build entry point. |
| 7. Supervisor | In progress | Implement and execute subprocess crash/restart/resume proof with idempotent finding record. |
| 8. Fix loop | Complete | Live Qwen + disposable worktree + actual QEMU fix/regression loop passed. |
| 9. Fuzzing | In progress | Add actual fixture runner, checkpoint/replay/minimize and explicit protocol-state coverage. |
| 10. Adaptive/verifier | Complete for fixture | Population prompts, adaptive variant and independent live verifier; invalid evaluator edit rejected. |
| 11. Evaluation | In progress | Execute A–E for seeds 1–3; label micro-suite limits honestly. |
| 12. Runtime routing | In progress | Document installed-runtime benchmark and evidence-based optional-runtime assessment; no large downloads. |
| 13. Training readiness | In progress | Execute verified JSONL/Parquet export and validate reload. |
| 14. Documentation/security | Pending | Architecture, threat model, runbook, real-target, runtime, training and demo scripts. |
| 15. Acceptance | Pending | Full test/audit commands, artifact index, `PROOF.json`, and `FINAL_REPORT.md`. |

## Resume

Run `git status --short`, read `docs/OSLAB_PROGRESS.md`, then continue the first incomplete milestone. Never treat the missing real OS target as permission to stop independent fixture/framework work.
