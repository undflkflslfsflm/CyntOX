# Real OS Integration

Current Gate L status: blocked by missing external input. Bounded discovery found no authorized real OS source in the current repository, immediate parent/children, or saved target configuration.

## Gate L Blocker Report

What was attempted:

- Bounded target discovery inspected the current repository, immediate parent/children, and saved target configuration without recursively crawling unrelated personal files or the whole disk.
- The main-checkout live selftest and clean-checkout live selftest both ran `oslab target inspect --json` as part of the final proof sequence.
- `oslab acceptance audit --json` re-checked the recorded Gate L status, the exact blocker text, the proof hashes, the clean-checkout source commit, and current Git cleanliness.
- The framework side of Gate L was implemented and tested: manifest validation, disposable commit-pinned build worktrees, real-target `build`, real-target `boot`, real-target `test`, QEMU `-nic none`, loopback QMP, and serial readiness/success pattern matching.

Concrete evidence:

- Verified source commit: `62a262cdfa754dc77479a9b5d7e4adb9a4cfaf73`.
- Main live selftest proof: `eb92b290866fde96ce6528778596ee23c59ef4d971f34209bed54dfc0efa261b` with `68 passed`.
- Clean-checkout live selftest proof: `fa57af57e2685038aeb6982301ca54e7e714c03f17dd454d9bb25756f1266f33` with `68 passed`.
- Saved acceptance audit proof: `89ef3f89705c1744cd63bf70226e32330e4a5f4344064c6db5ad50a535148218`.
- Current `target inspect` result: fixture ready, `real_os.status = "absent"`, `gate_l = "blocked_missing_external_input"`, and no bounded candidates.
- Machine-readable blocker report: `artifacts/reports/gate-l-blocker-report.json`.

Why further local progress is impossible:

- Gate L requires an actual authorized OS source tree and its existing build entry point. The lab cannot honestly build, cold-boot, smoke-test, or claim evidence for a target that is not locally present in the allowed discovery scope.
- The specification forbids inventing a successful build command, silently changing production build behavior, recursively crawling unrelated personal files, uploading source, or using external systems to fill in the missing target.
- Therefore the only defensible local state is `BLOCKED_MISSING_EXTERNAL_INPUT` until the authorized target path and manifest/build details are supplied.

Smallest input needed:

```powershell
.\.venv\Scripts\python.exe -m oslab.cli target inspect --repo C:\path\to\authorized-os --json
```

The blocker report itself can be regenerated from the current proof and target inspection:

```powershell
.\.venv\Scripts\python.exe -m oslab.cli target blocker-report --json
```

The path must point to the OS source the user owns or is authorized to test, and it must expose an existing build entry point. The lab must not invent or weaken the normal build.

The lab now includes a machine-validated manifest front door:

```powershell
.\.venv\Scripts\python.exe -m oslab.cli target manifest-template --json
.\.venv\Scripts\python.exe -m oslab.cli target validate-manifest --repo C:\path\to\authorized-os --json
.\.venv\Scripts\python.exe -m oslab.cli build --target real --repo C:\path\to\authorized-os --profile debug --json
.\.venv\Scripts\python.exe -m oslab.cli boot --target real --repo C:\path\to\authorized-os --profile debug --json
```

The tracked starter file is `config/oslab-target.example.toml`.

## Required Target Manifest

Create `oslab-target.toml` in the real OS root once the source is present. The validator requires:

- `source.root`, a full 40- or 64-character immutable `source.base_commit`, and `source.authorization = "owned_or_authorized"`
- `source.root` must resolve to the target Git repository root, and `source.base_commit` must resolve to an actual commit in that repository
- at least one named build profile with argv-vector commands, cwd, environment allowlist, and artifacts
- QEMU boot metadata with `network = "none"`
- at least one boot artifact and one readiness pattern
- at least one smoke test; serial PASS smoke tests must declare exact `success_patterns`
- optional debugger symbols, sanitizer/coverage labels, and cleanup paths

The validator rejects path traversal, paths escaping the target source root, non-immutable base commit names such as branches, base commits that Git cannot resolve in the target repository, target directories that accidentally inherit an unrelated parent Git repository, shell-eval command forms such as `bash -c` or `powershell -Command`, malformed profile/test identifiers, serial PASS smoke tests without success patterns, missing smoke tests, QEMU network devices, and public/NAT/bridged QEMU networking.

Unsupported profiles must be reported as unsupported. They must not be silently mapped to a weaker profile.

## Manifest-Backed Build Execution

When `oslab build --target real --repo ...` is used, the lab validates `oslab-target.toml`, creates a detached disposable Git worktree at `source.base_commit`, runs only the selected profile's declared argv-vector commands, passes only the selected profile's `env_allowlist`, and stores declared build artifacts in the content-addressed artifact store. The original target checkout is not used as the build directory.

When `oslab boot --target real --repo ...` or `oslab test --target real --test smoke --repo ...` is used, the lab builds the selected profile in a fresh manifest worktree, constructs a QEMU command from the manifest's boot artifacts, forces `-nic none`, exposes QMP only through loopback, waits for declared readiness and success serial patterns, saves serial/stderr artifacts, and shuts the VM down through QMP. The current Docker-backed smoke path supports `disk`, `iso`, `kernel`, `initrd`, and `firmware` boot artifacts and honestly rejects unsupported boot shapes.

## Safety Requirements

- Guest networking remains `none` unless a separate isolated virtual network is explicitly configured.
- Mutations occur in disposable worktrees, never directly in the production checkout.
- The evaluator and target ground truth remain read-only to workers.
- Build commands are declarative and allowlisted.
- Artifacts stay local and content-addressed.

## Unlocking Gate L

Gate L can pass only after the real target is present and the lab has actually built it from a disposable manifest worktree, cold-booted it through the manifest QEMU adapter, and run at least one smoke test without modifying normal production build behavior.

Exact resume sequence after the authorized source path is available:

```powershell
.\.venv\Scripts\python.exe -m oslab.cli target inspect --repo C:\path\to\authorized-os --json
.\.venv\Scripts\python.exe -m oslab.cli target manifest-template --json
.\.venv\Scripts\python.exe -m oslab.cli target blocker-report --json
.\.venv\Scripts\python.exe -m oslab.cli target validate-manifest --repo C:\path\to\authorized-os --json
.\.venv\Scripts\python.exe -m oslab.cli build --target real --repo C:\path\to\authorized-os --profile debug --json
.\.venv\Scripts\python.exe -m oslab.cli boot --target real --repo C:\path\to\authorized-os --profile debug --json
.\.venv\Scripts\python.exe -m oslab.cli test --target real --repo C:\path\to\authorized-os --test smoke --profile debug --json
```
