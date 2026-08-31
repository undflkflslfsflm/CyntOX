# Real OS Integration

Current Gate L status: blocked by missing external input. Bounded discovery found no authorized real OS source in the current repository, immediate parent/children, or saved target configuration.

Smallest input needed:

```powershell
.\.venv\Scripts\python.exe -m oslab.cli target inspect --repo C:\path\to\authorized-os --json
```

The path must point to the OS source the user owns or is authorized to test, and it must expose an existing build entry point. The lab must not invent or weaken the normal build.

## Required Target Manifest

Create `oslab-target.toml` in the real OS root once the source is present. The manifest should describe:

- source root and immutable base commit
- debug/release/sanitizer/coverage build profiles that already exist
- allowed build commands and whitelisted environment
- boot artifact paths: kernel, disk image, ISO, initrd, firmware as applicable
- QEMU machine, CPU, RAM, devices, accelerator, and network policy
- readiness patterns and serial logging protocol
- smoke tests, regression tests, and replay commands
- debugger symbols and symbolization command
- sanitizer and coverage output locations
- expected shutdown behavior and cleanup

Unsupported profiles must be reported as unsupported. They must not be silently mapped to a weaker profile.

## Safety Requirements

- Guest networking remains `none` unless a separate isolated virtual network is explicitly configured.
- Mutations occur in disposable worktrees, never directly in the production checkout.
- The evaluator and target ground truth remain read-only to workers.
- Build commands are declarative and allowlisted.
- Artifacts stay local and content-addressed.

## Unlocking Gate L

Gate L can pass only after the real target is present and the lab has actually built it, cold-booted it through the target adapter, and run at least one smoke test without modifying normal production build behavior.
