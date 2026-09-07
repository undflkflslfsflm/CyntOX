# Next Experiments

1. Add a real OS target manifest once the authorized source path and build entry point are available.
2. Extend the target plugin interface with target-specific serial commands, crash dump collection, debugger symbols, and smoke/regression suites.
3. Add compiler or emulator coverage for real targets where toolchains support it.
4. Add richer minimizers for multi-byte inputs and stateful test sequences.
5. Benchmark larger Ollama contexts such as 32K and 64K under real campaign load before promoting them beyond cautious `deep`/`long` use.
6. If installed later, benchmark llama.cpp against the same GGUF model and compare throughput, context stability, structured output reliability, and GPU memory use.
7. If AirLLM or KTransformers are installed later, treat them as rare oracle/offload experiments and measure wall-clock utility, disk pressure, and interaction with QEMU/fuzz workloads.
8. Build a larger verified trajectory corpus and add preference/verifier exports before any QLoRA or SFT run.
9. Add clean remote-free CI for non-live tests and a separate local acceptance script for Docker/Ollama/QEMU runs.
