# CyntOX Runbook

Normal assistant execution is local. Commands keep their existing privacy, tool, and target-authorization boundaries. Explicit setup can download prerequisites and models; see [installation](INSTALL.md).

## Setup

After the [one-line installation](INSTALL.md), run from any folder:

```powershell
cyntox ask "review this repo and give me the safest next engineering step"
```

Every public command starts with `cyntox`. Use `cyntox help` for the overview. An unregistered developer checkout can still use `.\cyntox.cmd` from its repository root.

Detached jobs keep the terminal short by returning a job id. `cyntox show JOB_ID` prints a compact human summary; use `cyntox show JOB_ID --json` for compact parseable metadata or `cyntox show JOB_ID --json --full > job.json` when you intentionally need full metadata. Foreground jobs still save the full result in `.oslab/cyntox/jobs/<job-id>/output.md`, but only print a 4,000-character preview; set `CYNTOX_FOREGROUND_OUTPUT_LIMIT=0` to suppress preview text or raise the number for a larger preview. CyntOX JSON output is field-bounded and whole-response-bounded by default, so oversized metadata returns a valid compact summary instead of flooding the terminal. `cyntox jobs sweep-stale` records bounded worker stdout/stderr tails into job evidence before marking dead workers failed, so crash recovery stays useful without flooding the terminal. Stress history auto-prunes generated timestamped reports to `--keep-history` by default; add `--no-prune-history` for one-off full retention.

## Command shortcuts

| Command | Action |
| --- | --- |
| `cyntox run` | Open the interactive assistant |
| `cyntox ask TASK` | Queue a task |
| `cyntox council "TASK"` | Run a foreground council review |
| `cyntox next` | Show the suggested next work |
| `cyntox check` | Check setup and prerequisites |
| `cyntox test` | Run reliability checks |
| `cyntox fix` | Run reliability checks with supported automatic fixes |
| `cyntox history` | Show test history |
| `cyntox jobs` | List jobs |
| `cyntox status` | Show job status; add `JOB_ID` to select one |
| `cyntox show JOB_ID` | Read a job's saved result |
| `cyntox resume JOB_ID` | Resume a selected job |
| `cyntox retry JOB_ID` | Retry a selected job |
| `cyntox report` | Show the job report |
| `cyntox remember TEXT` | Save a memory |
| `cyntox recall TEXT` | Search memory |
| `cyntox forget MEMORY_ID` | Archive a memory and remove it from search |
| `cyntox memory` | Show the memory vault path |
| `cyntox skills` | List available skills |
| `cyntox use SKILL "TASK"` | Use a selected skill for a task |
| `cyntox devices` | List registered devices |
| `cyntox privacy` | Show the privacy policy |
| `cyntox audit` | List saved audits |
| `cyntox airllm` | Show optional Qwythos/AirLLM status |
| `cyntox lab --help` | Show OS-lab commands |
| `cyntox help` | Show the command overview |

Replace `JOB_ID`, `MEMORY_ID`, and `SKILL` with real identifiers. Job and device operations never choose a target for you. Ordinary task text and search phrases need no quotes; quote paths containing spaces and text containing shell punctuation. Bare `audit` only lists existing audits; `cyntox audit start --repo "C:\path\to\repo"` explicitly starts one.

Shortcuts accept the same options as their original commands. For example, `cyntox fix --quick` means `cyntox stress --fix --quick`; `cyntox show JOB_ID --json` means `cyntox jobs show JOB_ID --json`; and `cyntox airllm setup` means `cyntox model airllm setup`. Legacy commands and script launchers remain available.

## Advanced launcher and setup options

One-line OS-lab launcher from anywhere in PowerShell:

```powershell
cyntox lab --help
```

One-line interactive CyntOX Code launcher from anywhere in PowerShell:

```powershell
cyntox run
```

This launches the project-local CyntOX Code package with bundled Node when system Node is not on `PATH`, auto-selects the local Ollama-backed `cyntox` model through the OpenAI-compatible loopback provider, displays it as `CyntOX` with a custom CyntOX/Mythos banner, uses an ignored `.oslab` interactive workspace, keeps startup context lean, enables normal text-file read/search/edit tools, denies `display_image` for text files, raises the default completion/context limits to reduce mid-answer truncation, tells the model to keep terminal answers compact and save/report file paths for long detail, removes the lab MCP approval prompt, and leaves the audited lab `.cyntox/settings.json` untouched. The human-use launcher also denies CyntOX Code's built-in `web_fetch` and `web_search` tools by default, injects CyntOX prompt-injection boundaries, and installs a local `run_shell_command` pre-tool hook that denies public-network/secret-exfiltration shell attempts plus obvious terminal-flood commands such as raw `git diff`, broad unbounded `rg`, unbounded recursive listings, and wildcard/raw file dumps before execution. Use council allowlists for any task that genuinely needs public internet. To run a one-shot prompt and stay interactive, add CyntOX Code's own `-i` option:

Council jobs use direct local Ollama by default because it avoids CyntOX Code's large tool-prompt overhead. The default direct-Ollama council limits are `num_ctx=32768` and `num_predict=8192`; override them with `CYNTOX_COUNCIL_NUM_CTX` and `CYNTOX_COUNCIL_NUM_PREDICT` only when you know the local model/runtime can handle it. Terminal previews are capped to 4,000 characters by default while full outputs are saved under the run artifacts; use `--terminal-output-limit <chars>` or `CYNTOX_TERMINAL_OUTPUT_LIMIT=<chars>` to resize previews, `--terminal-output-limit 0` for artifact pointers only, or `--print-full-output` for intentional full terminal dumps. CyntOX command JSON is compact, parseable, and whole-response capped by default; add `--full` beside `--json` when intentionally exporting full metadata, preferably redirected to a file. Use `cyntox council --engine cyntox-code ...` only when explicitly testing the CyntOX Code prompt path.

```powershell
cyntox run -i "help me inspect this project"
```

For an unregistered developer checkout (PowerShell):

```powershell
.\scripts\bootstrap.ps1
.\oslab.ps1 init --json
.\oslab.ps1 doctor --json
.\cyntox-code.ps1 --version
```

Bash or WSL:

```bash
./scripts/bootstrap.sh
python -m oslab.cli init --json
python -m oslab.cli doctor --json
```

## Common Workflows

### Defensive repository audits

Run `audit plan` first to confirm the immutable commit, dirty-tree status, stages, file budget, and optional adapter availability. `audit start` is detached by default; add `--foreground` for automation or debugging. Standard and deep profiles refuse dirty targets, keep networking disabled, and place patch candidates in disposable worktrees without modifying the source checkout. Use the quick profile for bounded read-only review of a dirty tree.

```powershell
cyntox audit plan --repo C:\src\owned-project --profile standard
cyntox audit start --repo C:\src\owned-project --profile standard
cyntox audit
cyntox audit status <audit-id>
cyntox audit findings <audit-id> --status confirmed
cyntox audit report <audit-id> --format sarif
```

Audit reports are stored under `artifacts/security/<audit-id>/`; resumable stage checkpoints and worker logs are stored under `.oslab/cyntox/audits/<audit-id>/`. A confirmed finding requires deterministic sink evidence and an adversarial validation verdict. Findings that need a caller-to-sink trace remain `needs_review`, and rejected test/fixture matches are retained for auditability but omitted from SARIF and the human report.

CyntOX daily jobs:

```powershell
cyntox ask "inspect this repo and propose the next safe fix"
cyntox next
cyntox check
cyntox fix
cyntox history --limit 10
cyntox fix --quick --repeat 3 --skip-qemu
cyntox fix --rerun-failures 1
cyntox test --prune-history --keep-history 50
cyntox test --no-prune-history
cyntox fix --strict
cyntox fix --require-qemu
cyntox jobs
cyntox show <job-id>
cyntox report
cyntox benchmark --dry-run
cyntox proof mythos
cyntox proof canary
cyntox benchmark mythos
cyntox benchmark prompt-ab --dry-run
```

`cyntox proof mythos` is the safe “impressiveness” proof: it writes `proofs/mythos/done.txt` through the typed broker, verifies fixed-path/fixed-content enforcement, checks denial of arbitrary shell/network tools, touches memory/skills, plans a device dry-run, and links the OS-lab smoke evidence. It writes `artifacts/reports/mythos-proof-report.json`. `cyntox benchmark mythos` runs the 10-task guided council smoke test. Its canonical report is `artifacts/reports/mythos-council-smoke-report.json`; `mythos-capability-report.json` is retained only as a compatibility alias. Every report identifies itself as `council_smoke`, sets `promotion_eligible=false`, and is not capability or parity evidence.

Run the prompt comparison in two stages. The harness always binds the explicit v2 candidate, independent of the normal v1 runtime selector. The first command generates 120 bound responses, application-origin start/completion receipts, and a 60-pair anonymized A/B bundle covering every case and seed, checkpointing after every response. Review only `blind-review.json`. **Copy** `blind-preferences-template.json` to a new `blind-preferences.json`; never edit the generated template because the first report binds its original bytes. In the copy, explicitly attest that the reveal mapping was not consulted, mark each anonymous answer's `material_defect` as `true` or `false`, add a short rationale for both answers, and choose `A`, `B`, or `tie`. The reviewer identity is self-declared human attestation, not cryptographically verified. Any material defect on a revealed candidate answer blocks adoption. Preferences are calculated only over the subjective pairs, ties are excluded from that denominator, and an all-tie review fails closed.

```powershell
cyntox benchmark prompt-ab
Copy-Item .oslab\cyntox\prompt-ab\RUN_ID\reviewer\blind-preferences-template.json .oslab\cyntox\prompt-ab\RUN_ID\reviewer\blind-preferences.json
cyntox benchmark prompt-ab --resume .oslab\cyntox\prompt-ab\RUN_ID\responses.json --preferences .oslab\cyntox\prompt-ab\RUN_ID\reviewer\blind-preferences.json --json --full
```

Use `--resume` with an interrupted `responses.json` checkpoint. Resume validates the suite, locked archived-v1 hash, explicit v2-candidate hash, model name, digest, seeds, and generation options before issuing another call. Fresh generation records the resolved Ollama digest immediately before and after every model call; any observation drift invalidates completion provenance even if the mutable tag later returns to its starting digest. Only a completed response bundle bound to an application-origin completion receipt can contribute authoritative adoption provenance. An interrupted or manually imported bundle may still be completed and scored diagnostically, but reused records remain non-authoritative without that completed receipt. The receipts provide application-managed local provenance and tamper evidence; they are not cryptographic proof against someone controlling the same local account. Prompt A/B passing supports prompt adoption only and does not establish AirLLM routing parity or change the configured model profile.

Device safety:

```powershell
cyntox devices
cyntox devices doctor local-4090-pc
cyntox run-on raspberry-pi "check uptime" --dry-run
cyntox setup jellyfin --target local-4090-pc
```

`devices.toml` is the source of truth for device channels and approval state. `configured=false` means planning/dry-run only. `approved_writes=false` blocks first writes and installs. USB alone is not treated as control; configure SSH/RDP/SMB/API/local filesystem explicitly.

```powershell
cyntox lab model probe --live --json
cyntox lab campaign --target fixture --budget 10m --seed 1 --iterations 6 --json
cyntox lab eval --suite seeded --seeds 1,2,3 --base-commit HEAD --json
cyntox lab acceptance audit --json
cyntox lab model benchmark --json
cyntox lab model cyntox-code-smoke --json
cyntox lab target inspect --json
cyntox lab target manifest-template --json
cyntox lab build --target fixture --profile debug --json
cyntox lab boot --target fixture --seed 1 --json
cyntox lab test --target fixture --test pass --seed 1 --json
cyntox lab reproduce --mode crash --cold-boots 2 --json
cyntox lab verify --finding crash --cold-boots 2 --json
cyntox lab fuzz run --campaign-id runbook-fixture --seed 101 --iterations 6 --json
cyntox lab minimize --finding latest-crash --campaign-id runbook-fixture --json
cyntox lab campaign recovery-proof --json
cyntox lab campaign agentic-fix --base-commit HEAD --seed 1 --json
cyntox lab campaign run --target fixture --budget 10m --seed 1 --iterations 6 --json
cyntox lab campaign --target fixture --budget 10m --seed 1 --iterations 6 --base-commit HEAD --json
cyntox lab eval run --seeds 1,2,3 --base-commit HEAD --json
cyntox lab eval --suite seeded --seeds 1,2,3 --base-commit HEAD --json
cyntox lab training dry-run --json
cyntox lab report --experiment latest --json
cyntox lab cleanup --dry-run --json
cyntox lab integrity check --json
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
cyntox lab selftest --live --json
cyntox lab acceptance artifact-index --write --json
cyntox lab acceptance audit --save --json
```

`selftest` runs `uv lock --check`, frozen/offline `pnpm install`, pytest, format check, lint, strict typing for both `oslab` and `scripts`, target inspection, generated acceptance trace/artifact-index checks, training export, cleanup dry-run, live Ollama smoke, CyntOX Code MCP smoke, and artifact/database integrity. The QEMU and live tests require Docker Desktop/WSL2, Ollama, and the local CyntOX model to be available.
`acceptance audit` checks the final proof files, gate summary, required artifact contents, key evidence CAS artifacts, artifact snapshot, referenced selftest proof blobs, clean-checkout source commit, live smoke stdout, recorded audit artifact, clean Git state, and proof-only post-verification changes. The recorded audit artifact must include the semantic-content, key-evidence, clean-checkout, and live-output checks, match the final gate summary and Gate L blocker, and decisive evidence blobs must exist with valid nested serial/stderr artifact references.

For the opt-in full-BF16 Qwythos specialist, run `cyntox airllm setup --dry-run` before the explicit setup, then verify with `cyntox airllm status --json --verify` and require current evidence with `cyntox airllm status --require-qualified`. Use `--model-profile hybrid-airllm` for the exact AirLLM specialist on councils, queued jobs, resume/retry, and council smoke tests. Keep `--model-profile single` as the tracked default and immediate rollback path. The resident backend is available only through explicit `model airllm smoke/qualify --backend resident` diagnostics, not through council routing. Neither the guided council smoke nor prompt A/B evaluation changes the default; a future routing change requires independent parity evidence and an explicit decision. The complete preparation, offline-worker, fallback, and qualification procedure is in `docs/AIRLLM_QWYTHOS.md`.

## Real OS Target

When the authorized OS source is available locally:

```powershell
cyntox lab target inspect --repo C:\path\to\authorized-os --json
cyntox lab target manifest-template --json
cyntox lab target validate-manifest --repo C:\path\to\authorized-os --json
cyntox lab build --target real --repo C:\path\to\authorized-os --profile debug --json
cyntox lab boot --target real --repo C:\path\to\authorized-os --profile debug --json
cyntox lab test --target real --repo C:\path\to\authorized-os --test smoke --profile debug --json
```

If the target has a build marker and OS-source markers, add an `oslab-target.toml` manifest using `docs/REAL_OS_INTEGRATION.md` or `config/oslab-target.example.toml`, then rerun target inspection and validation. The validator rejects path traversal, non-isolated QEMU networking, QEMU network devices, serial PASS smoke tests without success patterns, shell-eval build commands, target directories that inherit an unrelated parent Git repository, and base commits that do not resolve in the target Git repository. Manifest-backed builds run in detached disposable Git worktrees and pass only the manifest's declared environment allowlist. Manifest-backed boot/test commands run the declared serial smoke through Docker-backed QEMU with `-nic none` and loopback QMP. Do not guess build commands.
