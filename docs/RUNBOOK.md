# Runbook

All commands are local. They do not push, publish, install services, or modify global Qwen settings.

## Setup

PowerShell:

```powershell
.\scripts\bootstrap.ps1
.\.venv\Scripts\python.exe -m oslab.cli init --json
.\.venv\Scripts\python.exe -m oslab.cli doctor --json
```

Bash or WSL:

```bash
./scripts/bootstrap.sh
python -m oslab.cli init --json
python -m oslab.cli doctor --json
```

## Common Workflows

```powershell
.\.venv\Scripts\python.exe -m oslab.cli model probe --live --json
.\.venv\Scripts\python.exe -m oslab.cli model benchmark --json
.\.venv\Scripts\python.exe -m oslab.cli model qwen-code-smoke --json
.\.venv\Scripts\python.exe -m oslab.cli target inspect --json
.\.venv\Scripts\python.exe -m oslab.cli target manifest-template --json
.\.venv\Scripts\python.exe -m oslab.cli build --target fixture --profile debug --json
.\.venv\Scripts\python.exe -m oslab.cli boot --target fixture --seed 1 --json
.\.venv\Scripts\python.exe -m oslab.cli test --target fixture --test pass --seed 1 --json
.\.venv\Scripts\python.exe -m oslab.cli reproduce --mode crash --cold-boots 2 --json
.\.venv\Scripts\python.exe -m oslab.cli verify --finding crash --cold-boots 2 --json
.\.venv\Scripts\python.exe -m oslab.cli fuzz run --campaign-id runbook-fixture --seed 101 --iterations 6 --json
.\.venv\Scripts\python.exe -m oslab.cli minimize --finding latest-crash --campaign-id runbook-fixture --json
.\.venv\Scripts\python.exe -m oslab.cli campaign recovery-proof --json
.\.venv\Scripts\python.exe -m oslab.cli campaign agentic-fix --base-commit HEAD --seed 1 --json
.\.venv\Scripts\python.exe -m oslab.cli campaign run --target fixture --budget 10m --seed 1 --iterations 6 --json
.\.venv\Scripts\python.exe -m oslab.cli eval run --seeds 1,2,3 --base-commit HEAD --json
.\.venv\Scripts\python.exe -m oslab.cli training dry-run --json
.\.venv\Scripts\python.exe -m oslab.cli report --experiment latest --json
.\.venv\Scripts\python.exe -m oslab.cli cleanup --dry-run --json
.\.venv\Scripts\python.exe -m oslab.cli integrity check --json
```

## One-Command Demo

```powershell
.\scripts\demo.ps1
```

```bash
./scripts/demo.sh
```

## Full Local Proof

```powershell
.\.venv\Scripts\python.exe -m oslab.cli selftest --live --json
.\.venv\Scripts\python.exe -m oslab.cli acceptance audit --save --json
```

`selftest` runs pytest, format check, lint, strict typing, target inspection, training export, cleanup dry-run, live Ollama smoke, Qwen Code MCP smoke, and artifact/database integrity. The QEMU and live tests require Docker Desktop/WSL2, Ollama, and the local Qwen model to be available.
`acceptance audit` checks the final proof files, gate summary, artifact snapshot, clean Git state, and proof-only post-verification changes.

## Real OS Target

When the authorized OS source is available locally:

```powershell
.\.venv\Scripts\python.exe -m oslab.cli target inspect --repo C:\path\to\authorized-os --json
.\.venv\Scripts\python.exe -m oslab.cli target manifest-template --json
.\.venv\Scripts\python.exe -m oslab.cli target validate-manifest --repo C:\path\to\authorized-os --json
```

If the target has a build marker and OS-source markers, add an `oslab-target.toml` manifest using `docs/REAL_OS_INTEGRATION.md` or `config/oslab-target.example.toml`, then rerun target inspection and validation. The validator rejects path traversal and non-isolated QEMU networking. Do not guess build commands.
