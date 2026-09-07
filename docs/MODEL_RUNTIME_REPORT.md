# Model Runtime Report

Baseline generated: 2026-08-31

Current-status addendum: 2026-09-05. The Ollama measurements below remain historical
baseline evidence. Since that baseline was recorded, the exact full-BF16 Qwythos
checkpoint has been prepared in the isolated AirLLM runtime described in
`docs/AIRLLM_QWYTHOS.md`. Snapshot, layer-shard, package-lock, architecture, and offline
reload checks currently pass. Its prior smoke and qualification records are stale after
implementation-binding changes, so they are historical measurements only until the
post-freeze smoke and three-repeat qualification are regenerated.

## Local measured runtime

The validated worker runtime is the already-installed Ollama server on `http://127.0.0.1:11434`.

- Worker model: `cyntox:latest`
- CyntOX Code wrapper model: `cyntox-os-lab-worker:latest`
- Runtime: Ollama 0.33.2
- Architecture reported by Ollama: `cyntox-27b`
- Parameters reported by Ollama: 27,320,697,856
- Format/quantization: GGUF, `Q4_K_M`
- Model blob size: 17,378,626,464 bytes
- Architecture context limit reported by Ollama metadata: 262,144 tokens
- Validated lab context: 16,384 tokens for CyntOX Code MCP smoke; 8,192 tokens for the direct worker config
- Measured direct-output throughput: 45.371 output tokens/s average across 3 samples
- Structured JSON reliability: passed the live structured smoke test
- Tool-mediated reliability: passed the constrained CyntOX Code MCP smoke with only `mcp__oslab__policy_remaining_budget` and `mcp__oslab__fixture_explain` visible

Saved evidence:

- `artifacts/discovery/hardware-report.json`
- `artifacts/discovery/model-benchmark.json`
- `artifacts/evaluation/seeded-results.json`

## Runtime candidates

| Runtime | Local status | Current assessment |
| --- | --- | --- |
| Ollama | Installed and benchmarked | Default runtime. It already serves the resident GGUF model locally, supports JSON-schema formatting through the API, and exposes token timing fields used by the benchmark. |
| llama.cpp | Not installed as `llama-cli` or `llama-server` | Plausible future backend for GGUF on NVIDIA because upstream documents CUDA kernels and CPU/GPU hybrid inference, but it is not part of v1 proof until installed and benchmarked locally. |
| vLLM | Not installed | Upstream lists CyntOX-compatible causal models, but current GGUF support is documented as experimental and moved to an out-of-tree plugin. Not selected for the current GGUF worker. |
| SGLang | Not installed | Upstream has CyntOX 27B recipes and OpenAI-compatible serving, but no local SGLang server or HF-format model is installed here. |
| LM Studio | Not installed | Could be another local OpenAI-compatible server if installed, but there is no `lms` CLI or LM Studio package present in this environment. |
| AirLLM | Isolated runtime and locked Qwythos snapshot prepared; current qualification pending | The opt-in `hybrid-airllm` council profile streams the pinned full-BF16 Qwen3.5 checkpoint for `fact-checker` and `critic`. It is a disk-heavy, latency-sensitive specialist path, not a speed path. `single` remains the default. |
| KTransformers | Python package not installed | Upstream focuses on CPU/GPU heterogeneous MoE inference/fine-tuning. It is not useful for the current dense GGUF worker unless a compatible supported model and runtime are installed. |

## Lab profiles

- `fast`: resident Ollama CyntOX, short/medium context, default for normal worker calls, QEMU triage, and patch loops.
- `deep`: same model with larger context/output budget for hard cases. It stays on Ollama to avoid loading a duplicate model.
- `long`: same model with cautious 32K-64K routing, but retrieval is preferred before using huge prompts.
- `oracle`: disabled. No compatible larger/offloaded local runtime and model files were found.

These lab-router profiles are distinct from the council routing profiles `single` and
`hybrid-airllm` in `config/models.toml`.

The code implementation is `oslab/model/router.py`. It exposes `ModelRouter` for profile selection and `ResourceScheduler` for avoiding conflicts between QEMU/build/fuzz I/O and optional disk-heavy offload work.

## Upstream sources checked

- Ollama context length documentation: https://docs.ollama.com/context-length
- Ollama Modelfile parameters: https://docs.ollama.com/modelfile
- Ollama API structured output and token timing fields: https://github.com/ollama/ollama/blob/main/docs/api.md
- vLLM supported models: https://docs.vllm.ai/en/latest/models/supported_models/
- vLLM GGUF notes: https://docs.vllm.ai/en/latest/features/quantization/gguf/
- llama.cpp README: https://github.com/ggml-org/llama.cpp
- SGLang CyntOX architecture documentation: https://docs.sglang.io
- LM Studio docs: https://lmstudio.ai/docs/app
- AirLLM releases: https://github.com/lyogavin/airllm/releases
- KTransformers docs: https://kvcache-ai.github.io/ktransformers/
