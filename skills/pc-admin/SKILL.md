---
name: pc-admin
description: Diagnose and plan local Windows/PC administration tasks without assuming remote control.
---

# PC Admin

Use this skill for local PC setup, diagnostics, automation, services, drivers, GPUs, storage, networking, and performance tasks.

Core behavior:

- Separate what can be done locally from what requires administrator rights, another machine, BIOS/firmware access, or a physical cable/topology change.
- Prefer read-only diagnostics before changing settings.
- For service setup, identify the exact service, port, data directory, firewall rule, and rollback path.
- Avoid destructive disk, registry, firewall, or service changes unless explicitly authorized.
- When remote devices are involved, require an actual management channel such as SSH, RDP, SMB, web API, or a configured agent; USB alone is not control.

Give the user one safe next command when manual action is needed.
