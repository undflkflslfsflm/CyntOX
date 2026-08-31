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
- `oslab selftest --live --json` passed in both the main checkout and a clean checkout at `95aa53c`, including `ruff format --check .`.
- `oslab acceptance audit --save --json` provides a machine-checkable acceptance proof over the gate summary, required docs/artifacts, required support files, artifact-index hashes, real selftest proof artifacts, the recorded acceptance-audit artifact, Gate L blocker, proof-only post-verification changes, clean Git status, and unresolved placeholders. Saved audit artifact: `508998b34e5a0c06a6c7935d056f4d261c7d8b07c5522d0f84c4ebb4a8a1ca21`.
- Real target onboarding now has a typed `oslab-target.toml` schema, a tracked example manifest, a `target manifest-template` command, and a `target validate-manifest` command. The validator rejects path traversal, source-root escapes, missing smoke tests, non-isolated QEMU networking, branch-like base commits, and shell-eval command wrappers before any real target build is attempted.

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
```

The path must point to the authorized OS source and its existing build entry point. The lab must then build, cold-boot, and smoke-test that target through a declarative target manifest.

## Final Verification

- Main checkout selftest at `95aa53c`: PASS, proof artifact `32d2f2e59c17a9b7418f8ed602494323458aa6957748321d0f403629959b5ee3`; checkout remained clean afterward.
- Clean checkout bootstrap at `95aa53c`: PASS
- Clean checkout selftest: PASS, proof artifact `c2203ebb2a96f53264918fd3b849e6478ebb847e62eb8059b198eb4d8fe40bb4`; checkout remained clean afterward.
- Acceptance audit: PASS, artifact `508998b34e5a0c06a6c7935d056f4d261c7d8b07c5522d0f84c4ebb4a8a1ca21`.
- Artifact index: `artifacts/ARTIFACT_INDEX.snapshot.json`
- Machine proof: `PROOF.json`

## Future Research

See `docs/NEXT_EXPERIMENTS.md`. The next meaningful step is to provide the authorized real OS source/build path and add its target manifest.
