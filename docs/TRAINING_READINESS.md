# Training Readiness

The lab is training-ready at the data-pipeline level only. It does not claim the Qwen model has improved, because no sufficient held-out trajectory corpus exists yet.

## Implemented

- Versioned `TrajectoryEvent` schema
- Verified-event filtering
- Negative examples for invalid/evaluator-tampering outcomes
- Evidence hashes attached to exported rows
- Split assignment by defect family to reduce leakage
- JSONL export
- Parquet export
- Dataset card generation
- Reload validation through `oslab training dry-run`

Current dry-run evidence:

- Records exported: 16
- JSONL reload records: 16
- Parquet reload records: 16
- Summary artifact: `d1ff60f41e8a450d52a5c29fab71c224b727b4cb0bfec59b720ca514d72d631c`

## Commands

```powershell
.\.venv\Scripts\python.exe -m oslab.cli training dry-run --json
```

Outputs:

- `artifacts/training/dry-run/trajectories.jsonl`
- `artifacts/training/dry-run/trajectories.parquet`
- `artifacts/training/dry-run/DATASET_CARD.md`

## Not Claimed

- No fine-tuning has been run.
- No model improvement is claimed.
- No hidden reasoning is stored or trained on.
- No cloud data export is performed.

Next training work should collect more verified real-target and fixture trajectories, add preference-pair exports, add verifier/judge splits, and run held-out evaluation before any model-improvement claim.
