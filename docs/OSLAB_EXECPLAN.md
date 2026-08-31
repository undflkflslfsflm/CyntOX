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
| 6. Real target | Blocked externally | Manifest schema, template, and validator are complete; resume still needs authorized source path plus build entry point. |
| 7. Supervisor | Complete | Controlled exit 97, database recovery, full state path, one idempotent finding, and CAS proof passed. |
| 8. Fix loop | Complete | Live Qwen + disposable worktree + actual QEMU fix/regression loop passed. |
| 9. Fuzzing | Complete | Six actual QEMU protocol modes, checkpoint/resume data, deduplication, stable replay, and minimal input passed. |
| 10. Adaptive/verifier | Complete for fixture | Population prompts, adaptive variant and independent live verifier; invalid evaluator edit rejected. |
| 11. Evaluation | Complete | A-E variants ran for seeds 1-3 with 15 accepted fixture repairs, raw JSON/CSV, and report saved. |
| 12. Runtime routing | Complete | `docs/MODEL_RUNTIME_REPORT.md`, `artifacts/discovery/runtime-assessment.json`, `ModelRouter`, and `ResourceScheduler` added. |
| 13. Training readiness | Complete | `oslab training dry-run --json` exported and reloaded 16 JSONL/Parquet records. |
| 14. Documentation/security | Complete | Architecture, threat model, runbook, real-target, runtime, training, limitations, next experiments, README, and demo scripts added. |
| 15. Acceptance | Complete except Gate L | Main checkout and clean checkout live selftests passed at verified code commit `ad4a3be`; artifact index, `PROOF.json`, `FINAL_REPORT.md`, and runnable acceptance audit are produced. The audit now verifies required artifact contents, key evidence CAS artifacts, referenced selftest proof artifacts, live model/Qwen Code stdout, selftest dependency reproducibility checks, and the recorded acceptance-audit artifact, and rejects stale or missing evidence blobs. Overall goal remains blocked until an authorized real OS source path and build entry point are supplied. |

## Resume

Run `git status --short`, read `docs/OSLAB_PROGRESS.md`, then continue the first incomplete milestone. If no authorized real OS target has been supplied, the fixture/framework work is already complete and the only valid next step is to unlock Gate L with `oslab target inspect --repo <AUTHORIZED_OS_SOURCE_PATH> --json`.
