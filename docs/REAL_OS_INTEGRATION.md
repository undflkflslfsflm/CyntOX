# Real OS Integration

Current Gate L status: blocked by missing external input. Bounded discovery found no authorized real OS source in the current repository, immediate parent/children, or saved target configuration.

Smallest input needed:

```powershell
.\.venv\Scripts\python.exe -m oslab.cli target inspect --repo C:\path\to\authorized-os --json
```

The path must point to the OS source the user owns or is authorized to test, and it must expose an existing build entry point. The lab must not invent or weaken the normal build.

The lab now includes a machine-validated manifest front door:

```powershell
.\.venv\Scripts\python.exe -m oslab.cli target manifest-template --json
.\.venv\Scripts\python.exe -m oslab.cli target validate-manifest --repo C:\path\to\authorized-os --json
```

The tracked starter file is `config/oslab-target.example.toml`.

## Required Target Manifest

Create `oslab-target.toml` in the real OS root once the source is present. The validator requires:

- `source.root`, `source.base_commit`, and `source.authorization = "owned_or_authorized"`
- at least one named build profile with argv-vector commands, cwd, environment allowlist, and artifacts
- QEMU boot metadata with `network = "none"`
- at least one boot artifact and one readiness pattern
- at least one smoke test
- optional debugger symbols, sanitizer/coverage labels, and cleanup paths

The validator rejects path traversal, paths escaping the target source root, malformed profile/test identifiers, missing smoke tests, and public/NAT/bridged QEMU networking.

Unsupported profiles must be reported as unsupported. They must not be silently mapped to a weaker profile.

## Safety Requirements

- Guest networking remains `none` unless a separate isolated virtual network is explicitly configured.
- Mutations occur in disposable worktrees, never directly in the production checkout.
- The evaluator and target ground truth remain read-only to workers.
- Build commands are declarative and allowlisted.
- Artifacts stay local and content-addressed.

## Unlocking Gate L

Gate L can pass only after the real target is present and the lab has actually built it, cold-booted it through the target adapter, and run at least one smoke test without modifying normal production build behavior.
