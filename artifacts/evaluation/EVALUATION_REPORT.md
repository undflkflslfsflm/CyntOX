# Seeded Evaluation Report

Sample size: 15 runs (3 seeds × 5 variants).
Every accepted row used an isolated worktree and actual targeted plus regression QEMU cold boots. Baseline fingerprints came from three separate cold boots.
This deterministic fixture comparison is an infrastructure/ablation check; its small correlated sample does not establish general model-quality significance.

| Variant | Acceptance rate | Infra failures | Mean wall seconds | Mean output tokens |
|---|---:|---:|---:|---:|
| A — vanilla | 100.0% | 0 | 44.727 | 1738.0 |
| B — scaffold | 100.0% | 0 | 5.206 | 27.0 |
| C — retrieval-memory | 100.0% | 0 | 5.334 | 27.0 |
| D — adaptive-compute | 100.0% | 0 | 5.261 | 27.0 |
| E — adversarial-verifier | 100.0% | 0 | 6.571 | 27.0 |

All variants used the same installed model, defect, base commit, seeds, QEMU backend, and bounded output profile. Variant C used a hashed SQLite FTS retrieval result; variant D requested an explicit multi-hypothesis revision; variant E added an independent live verifier.
