# Runbook

All commands are local. They do not push, publish, install services, or modify global Qwen settings.

## Setup

One-line launcher from anywhere in PowerShell:

```powershell
& "C:\Users\vikto\Documents\ChatGPT\bob (qwen remodeled to act as mythos)\oslab.ps1" --help
```

One-line interactive Qwen Code launcher from anywhere in PowerShell:

```powershell
& "C:\Users\vikto\Documents\ChatGPT\bob (qwen remodeled to act as mythos)\qwen-code.ps1"
```

This launches the project-local Qwen Code package with bundled Node when system Node is not on `PATH`, uses an ignored `.oslab` interactive workspace, keeps startup context lean, enables normal text-file read/search/edit tools, denies `display_image` for text files, and leaves the audited lab `.qwen/settings.json` untouched. To run a one-shot prompt and stay interactive, add Qwen Code's own `-i` option:

```powershell
& "C:\Users\vikto\Documents\ChatGPT\bob (qwen remodeled to act as mythos)\qwen-code.ps1" -i "help me inspect this project"
```

PowerShell:

```powershell
.\scripts\bootstrap.ps1
.\oslab.ps1 init --json
.\oslab.ps1 doctor --json
.\qwen-code.ps1 --version
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
.\oslab.ps1 model probe --live --json
.\oslab.ps1 campaign --target fixture --budget 10m --seed 1 --iterations 6 --json
.\oslab.ps1 eval --suite seeded --seeds 1,2,3 --base-commit HEAD --json
.\oslab.ps1 acceptance audit --json
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
.\.venv\Scripts\python.exe -m oslab.cli campaign --target fixture --budget 10m --seed 1 --iterations 6 --base-commit HEAD --json
.\.venv\Scripts\python.exe -m oslab.cli eval run --seeds 1,2,3 --base-commit HEAD --json
.\.venv\Scripts\python.exe -m oslab.cli eval --suite seeded --seeds 1,2,3 --base-commit HEAD --json
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

`selftest` runs `uv lock --check`, frozen/offline `pnpm install`, pytest, format check, lint, strict typing, target inspection, training export, cleanup dry-run, live Ollama smoke, Qwen Code MCP smoke, and artifact/database integrity. The QEMU and live tests require Docker Desktop/WSL2, Ollama, and the local Qwen model to be available.
`acceptance audit` checks the final proof files, gate summary, required artifact contents, key evidence CAS artifacts, artifact snapshot, referenced selftest proof blobs, clean-checkout source commit, live smoke stdout, recorded audit artifact, clean Git state, and proof-only post-verification changes. The recorded audit artifact must include the semantic-content, key-evidence, clean-checkout, and live-output checks, match the final gate summary and Gate L blocker, and decisive evidence blobs must exist with valid nested serial/stderr artifact references.

## Real OS Target

When the authorized OS source is available locally:

```powershell
.\.venv\Scripts\python.exe -m oslab.cli target inspect --repo C:\path\to\authorized-os --json
.\.venv\Scripts\python.exe -m oslab.cli target manifest-template --json
.\.venv\Scripts\python.exe -m oslab.cli target validate-manifest --repo C:\path\to\authorized-os --json
.\.venv\Scripts\python.exe -m oslab.cli build --target real --repo C:\path\to\authorized-os --profile debug --json
.\.venv\Scripts\python.exe -m oslab.cli boot --target real --repo C:\path\to\authorized-os --profile debug --json
.\.venv\Scripts\python.exe -m oslab.cli test --target real --repo C:\path\to\authorized-os --test smoke --profile debug --json
```

If the target has a build marker and OS-source markers, add an `oslab-target.toml` manifest using `docs/REAL_OS_INTEGRATION.md` or `config/oslab-target.example.toml`, then rerun target inspection and validation. The validator rejects path traversal, non-isolated QEMU networking, QEMU network devices, serial PASS smoke tests without success patterns, shell-eval build commands, target directories that inherit an unrelated parent Git repository, and base commits that do not resolve in the target Git repository. Manifest-backed builds run in detached disposable Git worktrees and pass only the manifest's declared environment allowlist. Manifest-backed boot/test commands run the declared serial smoke through Docker-backed QEMU with `-nic none` and loopback QMP. Do not guess build commands.
