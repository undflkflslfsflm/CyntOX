# Known Limitations

- Gate L is complete against the separate pinned MIT-licensed `cyntox-open-os-target`, but that target is intentionally small and does not establish broad production-kernel coverage.
- The verified fixtures prove the lab control plane, QEMU path, broker, fuzzing, evaluation, and training pipeline; larger authorized OS targets still need their own manifests and evidence.
- Native Windows QEMU is not installed. The verified QEMU path uses Docker/WSL2 with TCG inside a pinned Debian container.
- Fixture debug backtraces are limited because the boot-sector target has no linked DWARF symbol table.
- The isolated AirLLM runtime and exact full-BF16 Qwythos checkpoint are prepared, but post-freeze live qualification and representative council latency evidence are still required. KTransformers, llama.cpp, vLLM, SGLang, and LM Studio remain unselected.
- The `oracle` profile is disabled until a compatible local runtime and model files are present and benchmarked.
- The current training pipeline exports verified examples but does not fine-tune or claim model improvement.
- The OS-lab evaluation sample remains intentionally small: A-E variants across three deterministic seeds. The prompt-v2 evaluation separately uses 20 cases and three seeds, but remains prompt-adoption evidence rather than a statistically broad capability or routing-parity benchmark.
