# Qwen OS Lab

Qwen OS Lab is a local-only, evidence-driven reliability laboratory for operating-system targets. A persistent external supervisor owns policy, budgets, state transitions, and acceptance; the model can only propose schema-validated actions through scoped tools.

The detected primary worker is the local Ollama model `huihui-qwen3.8-27b-abliterated:latest`. Guest networking is disabled, APIs bind to loopback, evaluated patches use disposable Git worktrees, and artifacts are content addressed.

## Bootstrap

One-line local launcher from PowerShell:

```powershell
& "C:\Users\vikto\Documents\ChatGPT\bob (qwen remodeled to act as mythos)\oslab.ps1" --help
```

One-line interactive Qwen Code launcher:

```powershell
& "C:\Users\vikto\Documents\ChatGPT\bob (qwen remodeled to act as mythos)\qwen-code.ps1"
```

That opens the project-local Qwen Code 0.22.3 CLI with the local Ollama-backed `qwen-os-lab-worker:latest` model and this repository's `.qwen/settings.json`.

PowerShell:

```powershell
.\scripts\bootstrap.ps1
.\oslab.ps1 doctor --json
.\oslab.cmd doctor --json
.\qwen-code.ps1 --version
.\.venv\Scripts\uv.exe run oslab doctor --json
.\scripts\demo.ps1
```

Bash/WSL:

```bash
./scripts/bootstrap.sh
.venv/bin/uv run oslab doctor --json
./scripts/demo.sh
```

The bootstrap creates a project-local environment and does not install a service or modify global Qwen settings. See `docs/RUNBOOK.md` for all workflows and `docs/THREAT_MODEL.md` for the security boundary.

## Current verified status

- Discovery, local model probe, Qwen Code constrained MCP smoke, QEMU fixture, recovery proof, fuzzing, A-E evaluation, training dry-run, and integrity/report commands have saved evidence under `artifacts/`.
- Spec-style workflows are available directly as `oslab campaign --target fixture --budget 10m --seed 1 --iterations 6 --json` and `oslab eval --suite seeded --seeds 1,2,3 --json`, while the original `campaign run` and `eval run` forms remain available.
- Optional runtimes are documented in `docs/MODEL_RUNTIME_REPORT.md`; only Ollama is installed and benchmarked locally.
- Gate L is blocked until a local authorized real OS source path and build entry point are supplied. Use `oslab target manifest-template --json`, create `oslab-target.toml` from `config/oslab-target.example.toml`, then run `oslab target inspect --repo <AUTHORIZED_OS_SOURCE_PATH> --json` and `oslab target validate-manifest --repo <AUTHORIZED_OS_SOURCE_PATH> --json` to unlock that gate. The validator confirms the target is its own Git repository root and that `source.base_commit` resolves to a real commit before any build is attempted. `oslab target blocker-report --json` regenerates the machine-readable blocker report while Gate L is waiting. `oslab build --target real --repo <AUTHORIZED_OS_SOURCE_PATH> --profile debug --json` then builds from a detached disposable worktree at that base commit and hashes the declared artifacts; `oslab boot --target real --repo <AUTHORIZED_OS_SOURCE_PATH> --profile debug --json` runs the declared serial smoke boot through Docker-backed QEMU with `-nic none` and loopback QMP.

## Proof command

```powershell
.\oslab.ps1 selftest --live --json
.\oslab.ps1 acceptance audit --save --json
```
