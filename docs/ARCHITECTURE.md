# Architecture

Qwen OS Lab is a local-only reliability-testing supervisor for owned OS targets. The language model proposes hypotheses and patches; the Python supervisor, policy layer, broker, QEMU backend, evaluator, and artifact store decide what is allowed and what counts as evidence.

## Main components

- `oslab/cli.py`: user-facing workflows for discovery, model probes, build/boot/test, reproduction, fuzzing, evaluation, training export, reporting, cleanup, and selftest.
- `oslab/config.py`: loopback-only model/API configuration, local runtime paths, resource budgets, and guest-network policy.
- `oslab/doctor.py`: bounded hardware, runtime, model, Qwen Code, Docker/WSL/QEMU, disk, and target discovery.
- `oslab/model/`: provider-neutral model interface, Ollama worker, Qwen Code worker, fake provider, and runtime router.
- `oslab/tools/broker.py`: capability-scoped typed tool broker. It owns path validation, worktree mutation, build/VM/test/fuzz/debug/report/memory/code tools, receipts, and denials.
- `oslab/qemu/`: Docker-backed QEMU fixture runner with QMP, serial capture, timeouts, snapshots, and `-nic none`.
- `oslab/fuzz/`: seedable fixture mutator, checkpointed campaigns, deduplication, replay, minimization, and protocol-state coverage.
- `oslab/supervisor.py` and `oslab/state_machine.py`: persistent state machine with transactional transitions and restart inspection.
- `oslab/eval/`: live agentic fix loop, deterministic verifier, and A-E seeded evaluation harness.
- `oslab/memory/`: local SQLite FTS retrieval over prior evidence and experiment notes.
- `oslab/training/`: verified trajectory export to JSONL/Parquet with split and dataset card generation.
- `fixtures/boot/`: true bootable QEMU fixture source, hidden ground truth, and deterministic PASS/FAIL/CRASH/HANG/seeded-defect behaviors.

## Control Flow

1. `oslab doctor` records host, tooling, model, and target facts.
2. The supervisor creates or resumes a run and prepares a disposable worktree for mutations.
3. The model receives bounded, model-visible context and proposes structured actions.
4. The broker validates every action against JSON schemas, path roots, evaluator immutability, budgets, and allowlists.
5. Build/test/QEMU/fuzz/debug tools execute locally and write content-addressed evidence.
6. The evaluator accepts findings only after reproduction, stable fingerprinting, targeted fix, regression survival, and independent verification.
7. Reports, evaluation rows, training trajectories, and final proof files point back to SHA-256 artifacts.

## Safety Boundary

The model never receives a generic host shell. Qwen Code is configured project-locally and fail-closed: `QwenCodeWorker` rejects any declared tool set other than the two read-only MCP tools used for the smoke proof. Guest networking is disabled by default. APIs bind to loopback. Patches are applied only in disposable lab worktrees with expected base commits and before/after hashes.

## Runtime Strategy

The measured resident Ollama Qwen model is the normal worker. The router exposes `fast`, `deep`, `long`, and disabled `oracle` profiles; all enabled profiles currently use the same resident model to avoid duplicate loads. Optional AirLLM, KTransformers, vLLM, SGLang, llama.cpp, and LM Studio paths are documented but not enabled without local installation and benchmarks.

## Persistence

SQLite stores experiments, runs, transitions, events, tool calls, entities, model invocations, findings, and FTS memory. Artifacts are stored under `artifacts/blobs/sha256/` and indexed in `artifacts/artifact-index.jsonl`.

## Target Model

The included fixture is the complete verified target. Real OS integration is through a target plugin/manifest that must describe source root, immutable base commit, build profiles, boot method, QEMU settings, readiness patterns, test transport, symbols, instrumentation, and cleanup. Gate L remains blocked until an authorized real OS source path and existing build entry point are provided.
