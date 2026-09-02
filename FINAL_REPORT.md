# Final Report

Generated: 2026-09-01

Overall status: blocked only on Gate L. The local framework, fixture proof, model integration, Qwen Code constrained MCP integration, fuzzing, evaluation, training export, docs, scripts, and clean-checkout verification are complete. The real OS target gate cannot pass until the authorized OS source path and build entry point are supplied.

## Confirmed Working

- Hardware/model/target discovery writes `artifacts/discovery/hardware-report.json`, `docs/HARDWARE_REPORT.md`, and `config/local.auto.toml`.
- Direct Ollama worker uses `huihui-qwen3.8-27b-abliterated:latest`: Qwen architecture `qwen35`, 27,320,697,856 parameters, GGUF `Q4_K_M`, Ollama 0.33.2, loopback endpoint.
- Measured model throughput: 45.371 output tokens/s average across three benchmark samples.
- Qwen Code 0.22.3 is project-local and constrained to two read-only MCP tools; the smoke proof returned `MCP_BUDGET_OK`.
- True QEMU fixture builds from NASM source inside the pinned Docker image and boots through QEMU TCG with QMP on loopback and `-nic none`.
- PASS, FAIL, CRASH, HANG, snapshot/restore, seeded defect, and induced infrastructure-error paths are exercised and classified.
- Broker required tool surface has handlers for repo, build, VM, test, debug, fuzz, report, policy, memory, code, and git families.
- Transactional patching requires base commit and expected hashes, denies evaluator edits, emits receipts, and rolls back on mismatch.
- Persistent supervisor recovery proves a controlled process exit and resume without DB corruption or duplicate accepted findings.
- Live agentic fix loop repaired the seeded fixture in a disposable worktree and rejected an invalid evaluator edit.
- Evaluation matrix A-E ran for seeds 1,2,3 with raw JSON/CSV and report output.
- Training dry-run exported and reloaded 16 verified JSONL/Parquet trajectory records; repeated dry-runs are byte-stable and do not dirty clean checkouts.
- One-line launchers `oslab.ps1`/`oslab.cmd` and `qwen-code.ps1`/`qwen-code.cmd` work from paths containing spaces/parentheses. The lab launchers forward to the project-local `oslab` CLI; the Qwen Code launchers start the project-local Qwen Code 0.22.3 CLI through bundled Node when system Node is unavailable. Human Qwen launches now auto-select the local `cyntox` Ollama alias through the OpenAI-compatible loopback provider, use an ignored `.oslab` interactive workspace, keep context lean, enable text-file inspection, remove the lab MCP prompt, deny `display_image` for text, and leave the audited lab `.qwen/settings.json` untouched.
- `oslab selftest --live --json` passed in both the main checkout and a clean checkout at `bfcf1d2`, including `uv lock --check`, frozen/offline `pnpm install`, `oslab target blocker-report --json`, `oslab acceptance trace --json`, `73 passed`, and `ruff format --check .`.
- The CLI accepts both subcommand-style and spec-style campaign/evaluation workflows, including `oslab campaign --target fixture --budget 10m --seed 7 --iterations 6 --base-commit HEAD --json` and `oslab eval --suite seeded --seeds 1,2,3 --base-commit HEAD --json`.
- `oslab acceptance audit --save --json` provides a machine-checkable acceptance proof over the gate summary, required docs/artifacts, required support files, semantic required-artifact contents, key evidence CAS artifacts, artifact-index hashes, real selftest proof artifacts, clean-checkout source commit, live Ollama/Qwen Code stdout, the current requirements trace, the recorded acceptance-audit artifact, Gate L blocker, proof-only post-verification changes, clean Git status, and unresolved placeholders. The recorded audit-artifact check rejects stale or inconsistent audit blobs, and the key-evidence check rejects missing/corrupt/semantically invalid decisive artifacts. Saved audit artifact: `b9c29c30afddddae75db78439c296d82e1c9b07f50445203bd7c826ae3244f2b`.
- Real target onboarding now has a typed `oslab-target.toml` schema, a tracked example manifest, a `target manifest-template` command, a `target validate-manifest` command, manifest-backed real-target build execution, and manifest-backed QEMU serial smoke boot/test execution. The validator rejects path traversal, source-root escapes, missing smoke tests, serial PASS smoke tests without success patterns, non-isolated QEMU networking, QEMU network devices, branch-like or unresolved base commits, inherited parent Git repositories, and shell-eval command wrappers before any real target build is attempted. Manifest-backed builds run from detached disposable Git worktrees at `source.base_commit`, pass only the declared environment allowlist, and hash declared build artifacts. Manifest-backed smoke runs boot the disposable-worktree artifacts through Docker-backed QEMU with `-nic none`, loopback QMP, declared serial readiness/success pattern checks, and serial/stderr artifact capture.

## Tested but Limited

- The fixture is intentionally small: a 16-bit boot-sector target. It proves the lab control plane and QEMU path, not production-kernel coverage.
- Fixture debug backtrace support is limited because there is no linked DWARF symbol table.
- Protocol-state coverage is collected for the fixture; compiler-level coverage is not claimed.
- Evaluation sample size is three deterministic seeds across five variants. It is engineering evidence, not broad statistical significance.

## Unsupported on This Hardware/Runtime

- Native Windows QEMU is not installed. Verified QEMU uses Docker/WSL2 and TCG.
- No real OS target is available in the bounded discovery scope.
- `oracle` routing is disabled because no compatible larger/offloaded model files and runtime were detected.

## Optional and Not Installed

- AirLLM, KTransformers, llama.cpp, vLLM, SGLang, and LM Studio are not installed as local commands or Python packages.
- Current upstream docs were checked and summarized in `docs/MODEL_RUNTIME_REPORT.md`; none of these optional runtimes were used as proof because no local installation was available to benchmark.

## Blocked by Missing External Input

Gate L requires the real OS target. The smallest unlocking input is:

```powershell
.\.venv\Scripts\python.exe -m oslab.cli target inspect --repo C:\path\to\authorized-os --json
.\.venv\Scripts\python.exe -m oslab.cli build --target real --repo C:\path\to\authorized-os --profile debug --json
.\.venv\Scripts\python.exe -m oslab.cli boot --target real --repo C:\path\to\authorized-os --profile debug --json
```

What was attempted: bounded target discovery, main and clean live selftests, current `target inspect`, and final acceptance audits. Evidence shows the fixture is ready, the manifest-backed real-target framework is implemented, and `real_os.status = "absent"` with no bounded candidates. The machine-readable blocker report is `artifacts/reports/gate-l-blocker-report.json`. The path must point to the authorized OS source and its existing build entry point. The lab must then build from the declarative manifest, cold-boot, and smoke-test that target.

## Final Verification

- Main checkout selftest at `bfcf1d26045e4eca48de566ccd7b0aca38b859b9`: PASS, proof artifact `947f56f30971854e00e0e581f903c83060023d133106f6786750ee9678d4e630`; checkout remained clean afterward.
- Main checkout safe subset: PASS, `68 passed, 5 deselected`.
- Clean checkout bootstrap at `bfcf1d26045e4eca48de566ccd7b0aca38b859b9`: PASS
- Clean checkout selftest: PASS, proof artifact `c3dc0486ff5619f4fa01c0d73ac9a48a1fc0510453e76a1d1c1d263d2815c8f7`; checkout remained clean afterward.
- Acceptance audit: PASS, artifact `b9c29c30afddddae75db78439c296d82e1c9b07f50445203bd7c826ae3244f2b`.
- Artifact index: `artifacts/ARTIFACT_INDEX.snapshot.json`
- Requirements trace: `artifacts/reports/requirements-trace.json`
- Machine proof: `PROOF.json`

## Future Research

See `docs/NEXT_EXPERIMENTS.md`. The next meaningful step is to provide the authorized real OS source/build path and add its target manifest.
