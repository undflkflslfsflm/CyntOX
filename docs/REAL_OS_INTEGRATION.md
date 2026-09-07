# Real OS Integration

Current Gate L status: PASS.

Gate L was unlocked with a separate MIT-licensed open-source OS target at:

```text
C:\Users\vikto\Documents\ChatGPT\cyntox-open-os-target
```

The target is its own Git repository, has a checked-in `oslab-target.toml`, and pins builds to immutable source commit:

```text
db7b591789313d6288584c223e954e6f52880d11
```

## Gate L Evidence

What was performed:

- Added a tiny MIT-licensed open-source boot-sector OS target.
- Validated the target manifest with `oslab target validate-manifest`.
- Built the target from a detached disposable worktree.
- Cold-booted the target through Docker-backed QEMU.
- Kept QEMU networking disabled with `-nic none`.
- Verified serial `READY` and `PASS` output before claiming success.

Verified commands:

```powershell
.\.venv\Scripts\python.exe -m oslab.cli target inspect --repo C:\Users\vikto\Documents\ChatGPT\cyntox-open-os-target --json
.\.venv\Scripts\python.exe -m oslab.cli target validate-manifest --repo C:\Users\vikto\Documents\ChatGPT\cyntox-open-os-target --json
.\.venv\Scripts\python.exe -m oslab.cli build --target real --repo C:\Users\vikto\Documents\ChatGPT\cyntox-open-os-target --profile debug --json
.\.venv\Scripts\python.exe -m oslab.cli test --target real --repo C:\Users\vikto\Documents\ChatGPT\cyntox-open-os-target --profile debug --test smoke --json
```

Smoke result:

```text
OSLAB_EVT {"event":"READY","seq":0}
OSLAB_EVT {"event":"PASS","seq":1}
```

Machine-readable evidence:

- `PROOF.json` field: `real_target`
- `artifacts/reports/gate-l-real-target-run.json`
- `artifacts/reports/gate-l-blocker-report.json` now reports Gate L `PASS`
- `artifacts/reports/requirements-trace.json` now reports Gate L `PASS`

## Required Target Manifest Rules

The validator requires:

- `source.root`, a full 40- or 64-character immutable `source.base_commit`, and `source.authorization = "owned_or_authorized"`
- `source.root` must resolve to the target Git repository root
- `source.base_commit` must resolve to an actual commit in that repository
- at least one named build profile with argv-vector commands, cwd, environment allowlist, and artifacts
- QEMU boot metadata with `network = "none"`
- at least one boot artifact and one readiness pattern
- at least one smoke test; serial PASS smoke tests must declare exact `success_patterns`
- optional debugger symbols, sanitizer/coverage labels, and cleanup paths

The validator rejects path traversal, paths escaping the target source root, non-immutable base commit names such as branches, base commits that Git cannot resolve, inherited parent Git repositories, shell-eval command forms such as `bash -c` or `powershell -Command`, malformed profile/test identifiers, serial PASS smoke tests without success patterns, missing smoke tests, QEMU network devices, and public/NAT/bridged QEMU networking.

## Manifest-Backed Build Execution

When `oslab build --target real --repo ...` is used, the lab validates `oslab-target.toml`, creates a detached disposable Git worktree at `source.base_commit`, runs only the selected profile's declared argv-vector commands, passes only the selected profile's `env_allowlist`, and stores declared build artifacts in the content-addressed artifact store.

When `oslab boot --target real --repo ...` or `oslab test --target real --test smoke --repo ...` is used, the lab builds the selected profile in a fresh manifest worktree, constructs a QEMU command from the manifest's boot artifacts, forces `-nic none`, exposes QMP only through loopback, waits for declared readiness and success serial patterns, saves serial/stderr artifacts, and shuts the VM down through QMP.

## Safety Requirements

- Guest networking remains `none` unless a separate isolated virtual network is explicitly configured.
- Mutations occur in disposable worktrees, never directly in the target source checkout.
- The evaluator and target ground truth remain read-only to workers.
- Build commands are declarative and allowlisted.
- Artifacts stay local and content-addressed.
