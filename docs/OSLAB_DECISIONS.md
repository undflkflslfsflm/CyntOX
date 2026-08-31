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

## D-006 — Give Qwen Code a larger zero-copy Ollama wrapper

- Status: accepted
- Date: 2026-08-31
- Context: Qwen Code 0.22.3 sends an approximately 8.6k-token initial prompt, while the installed model's default Modelfile limited context to 8,192. The native architecture declares 262,144, and a 16,384-token wrapper ran stably.
- Decision: create `qwen-os-lab-worker:latest` from the existing Ollama blob with `num_ctx 16384`; do not duplicate or download model weights.
- Consequence: the user's original model remains unchanged and Qwen Code can complete its two-turn MCP smoke.

## D-007 — Fail closed around Qwen Code tools

- Status: accepted
- Date: 2026-08-31
- Context: project MCP servers remain pending in non-interactive runs unless Qwen Code uses its non-prompting approval mode. Its `yolo` label would be unsafe with ordinary built-ins.
- Decision: disable every observed built-in tool, surface only two read-only MCP tools, mark both idempotent/non-destructive, and have `QwenCodeWorker` reject any startup tool set other than those exact names.
- Consequence: approval is automatic inside a smaller capability boundary. A Qwen Code upgrade that introduces an unexpected tool causes the adapter to abort before trusting the result.

## D-008 — Classify the missing real OS as Gate L only

- Status: accepted
- Date: 2026-08-31
- Context: bounded discovery found a dedicated orchestrator but no authorized OS source tree or build manifest.
- Decision: complete every fixture/framework gate and report the overall goal blocked rather than complete. The smallest unlocking input is a local OS source path and its build entry point.
- Consequence: no unrelated filesystem crawl or guessed target integration is attempted.

## D-009 — Keep optional offload runtimes disabled until locally benchmarked

- Status: accepted
- Date: 2026-08-31
- Context: only Ollama is installed and benchmarked locally. AirLLM, KTransformers, llama.cpp, vLLM, SGLang, and LM Studio are absent as commands/packages. Current upstream docs show possible compatibility paths, but several require different model formats, plugins, large disk caches, or offload-heavy operation.
- Decision: route `fast`, `deep`, and `long` profiles to the resident Ollama Qwen worker; leave `oracle` disabled.
- Consequence: v1 proof uses the fast resident model. Optional runtimes can be added only after local installation, compatibility checks, and measured utility.

## D-010 — Mark final status as blocked, not complete

- Status: accepted
- Date: 2026-08-31
- Context: every local fixture/framework gate now has evidence, including main-checkout and clean-checkout live selftests. Gate L still lacks the external real OS source path and build entry point needed for authorized integration.
- Decision: record local proof as complete and mark the overall goal `blocked_on_gate_l` instead of pretending real-target validation was performed.
- Consequence: future work resumes from one minimal user-provided input: the authorized OS repository path plus its existing build command.
