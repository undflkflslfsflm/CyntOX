# Qwen OS Lab

Qwen OS Lab is a local-only, evidence-driven reliability laboratory for operating-system targets. A persistent external supervisor owns policy, budgets, state transitions, and acceptance; the model can only propose schema-validated actions through scoped tools.

The detected primary worker is the local Ollama model `huihui-qwen3.8-27b-abliterated:latest`. Guest networking is disabled, APIs bind to loopback, evaluated patches use disposable Git worktrees, and artifacts are content addressed.

## Bootstrap

PowerShell:

```powershell
.\scripts\bootstrap.ps1
.\.venv\Scripts\uv.exe run oslab doctor --json
```

Bash/WSL:

```bash
./scripts/bootstrap.sh
.venv/bin/uv run oslab doctor --json
```

The bootstrap creates a project-local environment and does not install a service or modify global Qwen settings. See `docs/RUNBOOK.md` for all workflows and `docs/THREAT_MODEL.md` for the security boundary.

