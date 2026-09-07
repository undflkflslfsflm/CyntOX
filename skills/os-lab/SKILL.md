---
name: os-lab
description: Work on the local OS-lab subsystem with sandboxed, evidence-driven reliability testing.
---

# OS Lab

Use this skill when working on the OS-lab tooling, target manifests, QEMU fixtures, stress tests, build/boot flows, recovery proofs, or acceptance gates.

Core behavior:

- Keep the lab local-only unless the user explicitly authorizes a reachable target and management channel.
- Preserve the distinction between fixture testing and testing a real user-licensed OS source tree.
- Do not claim Gate L or real OS stress testing is complete without an authorized target manifest, resolvable base commit, build command, boot command, and saved evidence.
- Prefer disposable worktrees, loopback services, and `-nic none` QEMU fixtures for tests.
- Record proof artifacts and exact commands when completing a gate.

If blocked by missing real OS source or credentials, continue with fixture tests, validators, docs, or dry-runs that improve readiness.
