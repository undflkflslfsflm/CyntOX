# Final Report

Generated: 2026-09-02

Overall status: complete. The local framework, fixture proof, model integration, CyntOX Code constrained MCP integration, CyntOX daily assistant hardening, fuzzing, evaluation, training export, docs, scripts, clean-checkout verification, and real-target Gate L smoke path now have evidence. Gate L was unlocked with a separate MIT-licensed open-source OS target.

## Confirmed Working

- Hardware/model/target discovery writes `artifacts/discovery/hardware-report.json`, `docs/HARDWARE_REPORT.md`, and `config/local.auto.toml`.
- Direct Ollama worker uses `cyntox:latest`: CyntOX base architecture `cyntox-27b`, 27,320,697,856 parameters, GGUF `Q4_K_M`, Ollama 0.33.2, loopback endpoint.
- Measured model throughput: 45.371 output tokens/s average across three benchmark samples.
- CyntOX Code 0.22.3 is project-local and constrained to two read-only MCP tools; the smoke proof returned `MCP_BUDGET_OK`.
- True QEMU fixture builds from NASM source inside the pinned Docker image and boots through QEMU TCG with QMP on loopback and `-nic none`.
- PASS, FAIL, CRASH, HANG, snapshot/restore, seeded defect, and induced infrastructure-error paths are exercised and classified.
- Broker required tool surface has handlers for repo, build, VM, test, debug, fuzz, report, policy, memory, code, and git families.
- Transactional patching requires base commit and expected hashes, denies evaluator edits, emits receipts, and rolls back on mismatch.
- Persistent supervisor recovery proves a controlled process exit and resume without DB corruption or duplicate accepted findings.
- Live agentic fix loop repaired the seeded fixture in a disposable worktree and rejected an invalid evaluator edit.
- Evaluation matrix A-E ran for seeds 1,2,3 with raw JSON/CSV and report output.
- Training dry-run exported and reloaded 16 verified JSONL/Parquet trajectory records; repeated dry-runs are byte-stable and do not dirty clean checkouts.
- One-line launchers `oslab.ps1`/`oslab.cmd` and `cyntox-code.ps1`/`cyntox-code.cmd` work from paths containing spaces/parentheses. The lab launchers forward to the project-local `oslab` CLI; the CyntOX Code launchers start the project-local CyntOX Code 0.22.3 CLI through bundled Node when system Node is unavailable. Human CyntOX launches now auto-select the local `cyntox` Ollama alias through the OpenAI-compatible loopback provider, use an ignored `.oslab` interactive workspace, keep context lean, enable text-file inspection, remove the lab MCP prompt, deny `display_image` for text, and leave the audited lab `.cyntox/settings.json` untouched.
- CyntOX daily-use commands keep terminal output bounded by default: foreground/council previews are capped, CyntOX JSON is compact and whole-response capped, full dumps require explicit `--json --full`/redirection, and the CyntOX shell hook blocks broad terminal-flood commands before execution.
- `oslab selftest --live --json` passed in both the main checkout and a clean checkout at `6151eb0`, including `uv lock --check`, frozen/offline `pnpm install`, `mypy oslab scripts`, `oslab target blocker-report --json`, `oslab acceptance trace --json`, `oslab acceptance artifact-index --json`, `210 passed`, 3 expected QEMU-gated skips, and `ruff format --check .`.
- The CLI accepts both subcommand-style and spec-style campaign/evaluation workflows, including `oslab campaign --target fixture --budget 10m --seed 7 --iterations 6 --base-commit HEAD --json` and `oslab eval --suite seeded --seeds 1,2,3 --base-commit HEAD --json`.
- `oslab acceptance audit --save --json` provides a machine-checkable acceptance proof over the gate summary, required docs/artifacts, required support files, semantic required-artifact contents, key evidence CAS artifacts, artifact-index hashes, real selftest proof artifacts, clean-checkout source commit, live Ollama/CyntOX Code stdout, the current requirements trace, the recorded acceptance-audit artifact, Gate L status/evidence, proof-only post-verification changes, clean Git status, and unresolved placeholders. The recorded audit-artifact check rejects stale or inconsistent audit blobs, and the key-evidence check rejects missing/corrupt/semantically invalid decisive artifacts.
- Real target onboarding now has a typed `oslab-target.toml` schema, a tracked example manifest, a `target manifest-template` command, a `target validate-manifest` command, manifest-backed real-target build execution, and manifest-backed QEMU serial smoke boot/test execution. The validator rejects path traversal, source-root escapes, missing smoke tests, serial PASS smoke tests without success patterns, non-isolated QEMU networking, QEMU network devices, branch-like or unresolved base commits, inherited parent Git repositories, and shell-eval command wrappers before any real target build is attempted. Manifest-backed builds run from detached disposable Git worktrees at `source.base_commit`, pass only the declared environment allowlist, and hash declared build artifacts. Manifest-backed smoke runs boot the disposable-worktree artifacts through Docker-backed QEMU with `-nic none`, loopback QMP, declared serial readiness/success pattern checks, and serial/stderr artifact capture.

## Tested but Limited

- The fixture is intentionally small: a 16-bit boot-sector target. It proves the lab control plane and QEMU path, not production-kernel coverage.
- Fixture debug backtrace support is limited because there is no linked DWARF symbol table.
- Protocol-state coverage is collected for the fixture; compiler-level coverage is not claimed.
- Evaluation sample size is three deterministic seeds across five variants. It is engineering evidence, not broad statistical significance.

## Unsupported on This Hardware/Runtime

- Native Windows QEMU is not installed. Verified QEMU uses Docker/WSL2 and TCG.
- A real open-source OS target is available in the bounded discovery scope: `C:\Users\vikto\Documents\ChatGPT\cyntox-open-os-target`.
- `oracle` routing remains disabled because no backend has been qualified for that lab profile.

## Optional Runtimes

- AirLLM is now prepared in a separately locked Python 3.12 environment with the pinned full-BF16 Qwythos checkpoint. It was not part of the historical OS-lab proof, and its current implementation binding still requires post-freeze live requalification before it can be relied upon.
- KTransformers, llama.cpp, vLLM, SGLang, and LM Studio remain unselected. Current status and evidence boundaries are summarized in `docs/MODEL_RUNTIME_REPORT.md` and `docs/AIRLLM_QWYTHOS.md`.

## Gate L Completed With Open-Source Target

Gate L now uses the MIT-licensed open-source target:

```text
C:\Users\vikto\Documents\ChatGPT\cyntox-open-os-target
```

The target manifest pins source commit `db7b591789313d6288584c223e954e6f52880d11`. OS-lab validated the manifest, built `build/disk.raw` from a detached disposable worktree, cold-booted it through Docker-backed QEMU with networking disabled, and verified serial output:

```text
OSLAB_EVT {"event":"READY","seq":0}
OSLAB_EVT {"event":"PASS","seq":1}
```

Machine-readable proof is saved in `PROOF.json`, `artifacts/reports/gate-l-real-target-run.json`, `artifacts/reports/gate-l-blocker-report.json`, and `artifacts/reports/requirements-trace.json`.

## Final Verification

- Main checkout selftest at `6151eb06762c64e0f252eb276fa5404653f91be4`: PASS, proof artifact `9c531f81130566e71c8774e9454f9a01fd7bd5d834a531e00dab70c2ce8bfb0a`; checkout remained clean afterward.
- Main checkout safe subset: PASS, `208 passed, 5 deselected, 2 warnings`.
- Clean checkout bootstrap at `6151eb06762c64e0f252eb276fa5404653f91be4`: PASS
- Clean checkout selftest: PASS, proof artifact `7fba749b8a0f891acb98952066a9cbf2ba5dd6dbf3defcfbc0f588a7851e2ae9`; checkout remained clean afterward.
- Acceptance audit: PASS, artifact `2431801869e63e960ea9e5bb149bffe33576e7ea28bdadd2577f0b5ce6f9bf77`.
- Artifact index: `artifacts/ARTIFACT_INDEX.snapshot.json`
- Requirements trace: `artifacts/reports/requirements-trace.json`
- Machine proof: `PROOF.json`

## Future Research

See `docs/NEXT_EXPERIMENTS.md`. The next meaningful step is broader stress coverage against larger open-source or owned OS targets.
