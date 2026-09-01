# Qwen OS Lab

Qwen OS Lab is a local-only, evidence-driven reliability laboratory for operating-system targets. A persistent external supervisor owns policy, budgets, state transitions, and acceptance; the model can only propose schema-validated actions through scoped tools.

The detected primary worker is the local Ollama model `huihui-qwen3.8-27b-abliterated:latest`. Guest networking is disabled, APIs bind to loopback, evaluated patches use disposable Git worktrees, and artifacts are content addressed.

## Bootstrap

PowerShell:

```powershell
.\scripts\bootstrap.ps1
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
- Optional runtimes are documented in `docs/MODEL_RUNTIME_REPORT.md`; only Ollama is installed and benchmarked locally.
- Gate L is blocked until a local authorized real OS source path and build entry point are supplied. Use `oslab target manifest-template --json`, create `oslab-target.toml` from `config/oslab-target.example.toml`, then run `oslab target inspect --repo <AUTHORIZED_OS_SOURCE_PATH> --json` and `oslab target validate-manifest --repo <AUTHORIZED_OS_SOURCE_PATH> --json` to unlock that gate. The validator confirms the target is its own Git repository root and that `source.base_commit` resolves to a real commit before any build is attempted. `oslab build --target real --repo <AUTHORIZED_OS_SOURCE_PATH> --profile debug --json` then builds from a detached disposable worktree at that base commit and hashes the declared artifacts; `oslab boot --target real --repo <AUTHORIZED_OS_SOURCE_PATH> --profile debug --json` runs the declared serial smoke boot through Docker-backed QEMU with `-nic none` and loopback QMP.

## Proof command

```powershell
.\.venv\Scripts\python.exe -m oslab.cli selftest --live --json
.\.venv\Scripts\python.exe -m oslab.cli acceptance audit --save --json
```
