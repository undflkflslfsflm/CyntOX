# CyntOX

This repository is a private, local AI workbench called CyntOX. It runs a Qwen language model on the owner’s own computer through Ollama, gives it a custom “Mythos” personality, and surrounds it with safety checks, memory, specialized roles, and testing tools.

CyntOX is the primary daily command for the local-first AI workbench. The stack uses the local CyntOX model, Mythos operating persona, council review, resumable jobs, repo-local skills, a device safety registry, and an Obsidian-compatible memory vault. The OS-lab subsystem remains a local-only, evidence-driven reliability laboratory for operating-system targets.

The detected primary worker is the local Ollama model `cyntox:latest`. Guest networking is disabled, APIs bind to loopback, evaluated patches use disposable Git worktrees, and artifacts are content addressed.

## Install and run (Windows)

Paste this one line into PowerShell:

```powershell
irm https://raw.githubusercontent.com/undflkflslfsflm/CyntOX/main/install.ps1 | iex
```

Then start CyntOX from any folder:

```text
cyntox run
```

Setup downloads this repository's `main` branch, installs missing prerequisites, prepares the locked environment and local model, and registers the command in your user PATH. Windows App Installer (`winget`) is needed if prerequisites are missing; Windows may request approval for a prerequisite installer. The base-model download is about 17 GB and fresh model setup requires 50 GiB free disk. Setup reuses an existing `cyntox` model. Docker and the optional Qwythos/AirLLM specialist are separate setups.

See [installation details](docs/INSTALL.md) for locations, reruns, and using an existing checkout.

## Everyday commands

Once installed, these work from any folder:

| Command | Purpose |
| --- | --- |
| `cyntox run` | Open the interactive assistant |
| `cyntox ask review this repo` | Queue a task and return its job ID |
| `cyntox check` | Check the local setup |
| `cyntox test` | Run reliability checks |
| `cyntox fix` | Run those checks with supported automatic fixes |
| `cyntox jobs` | List saved jobs |
| `cyntox status` | Show job status |
| `cyntox show JOB_ID` | Read a saved job |
| `cyntox resume JOB_ID` | Resume a selected job |
| `cyntox retry JOB_ID` | Retry a selected job |
| `cyntox remember I prefer concise answers` | Save a memory |
| `cyntox recall jellyfin` | Search memory |
| `cyntox help` | Show all command shortcuts |

Replace `JOB_ID` with the ID returned by your task. Commands that select a job or device require its ID; they do not guess a target. Ordinary task text needs no quotes. Quote paths containing spaces and text containing shell punctuation.

More simple commands are `cyntox history`, `cyntox report`, `cyntox skills`, `cyntox devices`, `cyntox privacy`, `cyntox memory`, and `cyntox airllm`. They show test history, a job report, available skills, registered devices, the privacy policy, the memory path, and optional specialist status respectively. `cyntox audit` lists saved audits; start a new one with `cyntox audit start --repo "C:\path\to\repo"`. See the [full command reference](docs/RUNBOOK.md).

The original forms, including `cyntox doctor`, `cyntox stress`, and `cyntox jobs show JOB_ID`, remain compatible. Unregistered checkouts can still use `.\cyntox.cmd` and the existing script launchers.

## Advanced and developer workflows

Run a foreground council review:

```powershell
cyntox council "review this repo and give me the safest next engineering step"
```

The council runs focused roles, scores the final answer, and retries synthesis once if the score is below the quality threshold. Default mode is planning/review. Use `--mode implement` for scoped local changes or authorized setup.

```powershell
cyntox council --preset fast --max-wall-time 5m "make a Raspberry Pi Jellyfin setup plan"
cyntox council --mode implement "make the smallest safe local repo change for this task"
cyntox use media-server "plan Jellyfin on the 4090 PC with Pi helper"
cyntox run-on raspberry-pi "check uptime" --dry-run
cyntox setup jellyfin --target local-4090-pc
cyntox test --quick --repeat 3 --skip-qemu
cyntox fix --require-qemu
cyntox history --limit 10
cyntox memory sync --json
cyntox forget MEMORY_ID
cyntox benchmark mythos
cyntox benchmark prompt-ab --dry-run
cyntox lab --help
```

`cyntox run` opens the project-local CyntOX Code CLI with the Ollama-backed `cyntox` alias. It uses an ignored `.oslab` interactive workspace and local provider, keeps startup context lean, and gives the assistant text-file read/search/edit tools. The existing local privacy and tool boundaries apply.

Council jobs use direct local Ollama by default with `num_ctx=32768` and `num_predict=8192`. Terminal output shows a 4,000-character preview; full role outputs are saved in run artifacts. Use `--terminal-output-limit <chars>` to resize previews, `--terminal-output-limit 0` for the artifact pointer, or `--print-full-output` for full text. JSON is compact by default; add `--full` beside `--json` when intentionally exporting complete metadata.

Jobs are stored under `.oslab/cyntox/jobs/<job-id>/` with `job.json`, `prompt.md`, `output.md`, `commands.jsonl`, `events.jsonl`, `errors.log`, `verification.md`, `score.json`, and optional `memory.md`. Normal `cyntox ask "task"` returns a job id immediately and runs detached; use `cyntox show JOB_ID` to inspect a compact evidence summary, `cyntox show JOB_ID --json` for compact parseable metadata, or `cyntox show JOB_ID --json --full > job.json` for the full saved metadata. Foreground jobs print a 4,000-character preview of `output.md` by default and keep the full output in the job artifact; set `CYNTOX_FOREGROUND_OUTPUT_LIMIT=0` to print only the artifact pointer or a larger number to increase the preview. `jobs sweep-stale` also captures bounded worker stdout/stderr tails into job evidence before marking a dead worker failed, so crash recovery stays useful without dumping logs into the terminal. Stress history auto-prunes generated timestamped reports to the keep limit by default; use `--no-prune-history` for one-off full retention.

Presets: `fast` = architect/critic/synthesizer/scorer, `balanced` = full practical review with fact-checker, `max` = full review with fact-checker, skillmaker, and the strict 9.0 quality gate. Skill creation is off unless `--allow-skill-create` and `--mode implement` are both set; created skills are repo-local under `skills/`. Skill usage and score impact are tracked in `skills/.registry.json`; unused skills are archived to unique folders under `skills/.archive/` with lifecycle notes mirrored into `vault/Skills` from both CLI and council archive flows. Skill archive dry-runs preview without moving skills or mutating the registry. Starter skills are `coding`, `pc-admin`, `media-server`, `research-notes`, `os-lab`, and `privacy-security`.

CyntOX vault/RAG memory is stored as Markdown under `vault/` and indexed into `.oslab/cyntox/memory.sqlite3`. Obsidian can open `vault/` directly; CyntOX uses retrieved vault notes as memory hints, not proof. `memory sync` treats current non-archived vault Markdown as the source of truth, so archived or deleted notes are purged from search. `memory forget` requires a non-empty identifier, moves a note to `vault/Archive/`, marks its frontmatter as archived, and removes it from RAG. Memory note creation picks a unique filename instead of overwriting same-second duplicates. Memory confidence must be normalized from `0` to `1`, and search results are capped to keep RAG bounded. `memory search --json` returns bounded excerpts by default; add `--full` only when intentionally exporting full note bodies. Vault writes and job-memory extraction reject common secret shapes, including token assignments, authorization headers, private keys, OpenAI-like keys, AWS access keys, and GitHub tokens.

Device control is registered in `devices.toml`. Dry-run planning is allowed for configured and unconfigured devices. Configured devices must declare command allow/deny policies or `devices doctor` keeps them in limited mode. The deterministic executor enforces each device's denied-command list and allowed-command list before local/SSH execution, rejects unsafe SSH host/user values before invoking `ssh`, and blocks setup services that are not allowed for the target. Dangerous task patterns hard-stop implementation/device execution, while plan-mode boundary discussions are passed to the council instead of being falsely blocked. Real writes/installs require an approved target and command evidence; CyntOX must not claim success without captured output and verification.

Privacy defaults are strict. The council injects a default-deny internet policy (`--internet-mode off`), can allow specific domains with `--internet-mode allowlist --allow-domain <domain>`, scans prompt text/RAG context for injection-like and secret-like patterns, and records a privacy summary in each run manifest. The interactive CyntOX launcher denies built-in `web_fetch`/`web_search` and installs a local `run_shell_command` pre-tool hook that denies public-network/secret-exfiltration shell attempts plus obvious terminal-flood commands such as raw `git diff`, broad unbounded `rg`, unbounded recursive listings, and wildcard/raw file dumps before execution. See `docs/PRIVACY_AND_INJECTION.md`.

`cyntox audit` lists saved defensive repository audits. `cyntox audit start --repo REPO_PATH` starts an audit: it fingerprints the selected Git commit, inventories the codebase, performs bounded specialist scans, independently validates matches, deduplicates root causes, and emits Markdown, JSON, and SARIF 2.1.0. Standard and deep audits require a clean target checkout and generate eligible patches only in disposable worktrees under `.oslab`; they never apply changes to the source checkout. Quick audits are read-only and may inspect a dirty tree. External harness adapters are optional and remain disabled when their executable, authentication, or network authorization is unavailable. The pinned reference inventory is `config/security-harnesses.lock.json`.

`cyntox proof mythos` runs a safe Mythos-level capability proof: a dry-run council job, a brokered canary write to `proofs/mythos/done.txt`, denial checks for arbitrary paths/shell/network tools, memory and skill lifecycle checks, a device dry-run plan, and OS-lab smoke-evidence review. The matching machine report is `artifacts/reports/mythos-proof-report.json`. `cyntox benchmark mythos` is a 10-task guided, council-scored smoke test with strict quality and boundary checks. Its canonical report is `artifacts/reports/mythos-council-smoke-report.json`; the old `mythos-capability-report.json` is written only as a compatibility alias. Every report is labeled `benchmark_kind=council_smoke`, `promotion_eligible=false`, and is not capability, parity, or routing-promotion evidence.

`cyntox benchmark prompt-ab` is the held-out prompt-adoption evaluation. It compares the hash-locked `prompts/archive/mythos-system-v1.md` with the explicit v2 candidate at `prompts/mythos-system.md` using 20 evaluator-only cases, three fixed seeds, deterministic semantic and read-only-command gates, per-generation Ollama digest observations under identical locked configuration, and an anonymized review of all 60 response pairs with absolute material-defect assessments. Any candidate material defect blocks adoption; the preference rate is calculated only for the declared subjective cases. Human reviewer identity is explicitly self-attested rather than cryptographically verified. It writes machine-readable checkpoints, application-origin generation receipts, and semantically re-verifiable JSON and Markdown reports and never asks either candidate to judge itself. Normal runtime remains on v1; set `CYNTOX_MYTHOS_V2_CANDIDATE=1` only for an explicit candidate session. Passing the A/B supports adoption of the prompt only; it does not establish Qwythos/AirLLM routing parity.

Unregistered developer checkout (PowerShell):

```powershell
.\scripts\bootstrap.ps1
.\oslab.ps1 doctor --json
.\oslab.cmd doctor --json
.\cyntox-code.ps1 --version
.\.venv\Scripts\uv.exe run oslab doctor --json
.\scripts\demo.ps1
```

Bash/WSL:

```bash
./scripts/bootstrap.sh
.venv/bin/uv run oslab doctor --json
./scripts/demo.sh
```

The bootstrap creates a project-local environment and does not install a service or modify global CyntOX settings. See `docs/RUNBOOK.md` for all workflows and `docs/THREAT_MODEL.md` for the security boundary.

The optional locked Qwythos council specialist, isolated AirLLM profile, resident-only diagnostics, and qualification workflow are documented in `docs/AIRLLM_QWYTHOS.md`. `single` remains the tracked default and permanent rollback, and `hybrid-airllm` is the only Qwythos council profile. The current council smoke and prompt A/B commands never auto-promote routing unless an independent routing-parity evaluation is added and accepted. The evidence-driven hardening and stop criteria are in `docs/QWYTHOS_IMPROVEMENT_PLAN.md`.

## Current verified status

- Discovery, local model probe, CyntOX Code constrained MCP smoke, QEMU fixture, recovery proof, fuzzing, A-E evaluation, training dry-run, and integrity/report commands have saved evidence under `artifacts/`.
- Spec-style workflows are available directly as `oslab campaign --target fixture --budget 10m --seed 1 --iterations 6 --json` and `oslab eval --suite seeded --seeds 1,2,3 --json`, while the original `campaign run` and `eval run` forms remain available.
- Optional runtimes are documented in `docs/MODEL_RUNTIME_REPORT.md`. Ollama remains the primary runtime; the prepared resident/AirLLM Qwythos paths have historical local smoke and qualification measurements, but implementation-binding changes make that evidence stale until requalification.
- Gate L now passes with the MIT-licensed open-source target at `C:\Users\vikto\Documents\ChatGPT\cyntox-open-os-target`. The lab validates its `oslab-target.toml`, builds from immutable commit `db7b591789313d6288584c223e954e6f52880d11` in a detached disposable worktree, then runs the serial smoke test through Docker-backed QEMU with `-nic none` and loopback QMP. The saved smoke proof is `artifacts/reports/gate-l-real-target-run.json`, and the serial output contains `READY` followed by `PASS`.

## Proof command

```powershell
cyntox lab selftest --live --json
cyntox lab acceptance artifact-index --write --json
cyntox lab acceptance audit --save --json
```
