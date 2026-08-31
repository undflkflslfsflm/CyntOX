# Known Limitations

- Gate L is blocked because no real OS source/build entry point is available in the bounded discovery scope.
- The verified fixture is intentionally tiny. It proves the lab control plane, QEMU path, broker, fuzzing, evaluation, and training pipeline, not full production-kernel coverage.
- Native Windows QEMU is not installed. The verified QEMU path uses Docker/WSL2 with TCG inside a pinned Debian container.
- Fixture debug backtraces are limited because the boot-sector target has no linked DWARF symbol table.
- Optional runtimes AirLLM, KTransformers, llama.cpp, vLLM, SGLang, and LM Studio are not installed locally and are not used in v1 proof.
- The `oracle` profile is disabled until a compatible local runtime and model files are present and benchmarked.
- The current training pipeline exports verified examples but does not fine-tune or claim model improvement.
- Evaluation sample size is intentionally small: A-E variants across three deterministic seeds. The report is comparative engineering evidence, not a statistically broad benchmark.
