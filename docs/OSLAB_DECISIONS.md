# Qwen OS Lab Decision Log

## D-001 — Use the current repository as the orchestration root

- Status: accepted
- Date: 2026-08-31
- Context: the current repository is empty and therefore is not an existing OS source tree.
- Decision: create the lab at the repository root and reserve `.oslab/` for ignored runtime state.
- Consequence: a nearby real OS repository may be added as a separately allowlisted target; it will not be copied or mutated directly.

## D-002 — Start on a dedicated branch before the initial commit

- Status: accepted
- Date: 2026-08-31
- Context: an unborn repository cannot create a secondary worktree until a first commit exists.
- Decision: create `codex/qwen-os-lab`, make a minimal baseline commit after bounded instruction discovery, then exercise disposable worktrees through the broker later.
- Consequence: the initial bootstrap occurs in the primary checkout; all evaluated patch work will use disposable worktrees.

## D-003 — Evidence over declared capability

- Status: accepted
- Date: 2026-08-31
- Decision: unavailable tools and optional runtime adapters are reported as unsupported or untested. Only actual executions satisfy acceptance gates.

## D-004 — Use Ollama as the validated primary local provider

- Status: accepted
- Date: 2026-08-31
- Context: the local manifest and live Ollama API identify `huihui-qwen3.8-27b-abliterated:latest` as a Qwen3.8 27.3B GGUF model quantized `Q4_K_M`, with tool/thinking/completion capabilities and a declared 262,144-token architecture context. The installed Modelfile defaults to 8,192 tokens.
- Decision: implement Ollama native and OpenAI-compatible adapters first, initially benchmark at conservative context sizes, and increase only from measured stability.
- Consequence: the informal 37B description is superseded by measured model metadata. Ollama 0.33.2 at `http://127.0.0.1:11434` is the primary local runtime.

## D-005 — Use Docker/WSL isolation for the QEMU toolchain

- Status: provisional
- Date: 2026-08-31
- Context: native Windows QEMU and compiler tools are absent, while Docker Desktop 4.88.1 and WSL2 are operational.
- Decision: build a pinned Linux container containing NASM and QEMU and run the true fixture under TCG. Do not install system-wide packages.
- Consequence: WHPX is unavailable inside the Linux container; TCG performance will be measured and classified honestly.
