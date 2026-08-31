# Final Report

Generated: 2026-08-31

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
- `oslab selftest --live --json` passed in both the main checkout and a clean checkout at `e197334`, including `ruff format --check .`.

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

- Main checkout selftest at `e197334`: PASS, proof artifact `b0ec83e89a2d82fd7536e32a0c9b2324e1913b4b7bb33024838df83c4015b4ce`; checkout remained clean afterward.
- Clean checkout bootstrap at `e197334`: PASS
- Clean checkout selftest: PASS, proof artifact `9817cf3be28ae95c4bb869c3f626aae43d1da79331a7ebb9e2fe473a438d07a4`; checkout remained clean afterward.
- Artifact index: `artifacts/ARTIFACT_INDEX.snapshot.json`
- Machine proof: `PROOF.json`

## Future Research

See `docs/NEXT_EXPERIMENTS.md`. The next meaningful step is to provide the authorized real OS source/build path and add its target manifest.
