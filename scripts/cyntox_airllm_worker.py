# mypy: disable-error-code="import-not-found,misc"

from __future__ import annotations

import argparse
import contextlib
import ctypes
import gc
import hashlib
import io
import json
import math
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

MODEL_ID = "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated"
REVISION = "efcc73cac15ff8fc5d46b8d41b53c22d571cf97d"
EXPECTED_BYTES = 19_333_096_957
CONTEXT_LIMIT = 32_768
RESIDENT_CONTEXT_LIMIT = 8_192
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PRIVATE_REASONING_PROBE_TOKENS = 16
SUPPORTED_BACKENDS = ("airllm", "resident")
FAKE_MODES = frozenset(
    {
        "bad-handshake",
        "bind-attempt",
        "crash",
        "health-delay",
        "invalid-schema",
        "malformed",
        "network-attempt",
        "oom",
        "oom-once",
        "oversized-response",
        "process-attempt",
        "spawned-descendant",
        "startup-crash",
        "startup-hang",
        "stdout-flood",
        "timeout",
        "udp-attempt",
        "wrong-identity",
    }
)


def _normalized_gpu_uuid(value: object) -> str:
    """Normalize NVIDIA/PyTorch UUID spellings for a physical-device check."""
    rendered = str(value or "").strip().casefold()
    return rendered.removeprefix("gpu-")


def _verified_gpu_uuid(torch_module: Any, expected: str | None) -> str:
    """Prove that CUDA device zero is the physical GPU selected by the parent."""
    if not expected:
        raise RuntimeError("Qwythos worker has no selected physical GPU identity")
    if not torch_module.cuda.is_available():
        raise RuntimeError("CUDA is unavailable")
    actual = getattr(torch_module.cuda.get_device_properties(0), "uuid", None)
    if not actual or _normalized_gpu_uuid(actual) != _normalized_gpu_uuid(expected):
        raise RuntimeError("Qwythos worker CUDA device does not match its selected GPU identity")
    return expected


def _cuda_peak_mib(torch_module: Any) -> float:
    return float(torch_module.cuda.max_memory_reserved(0) / (1024 * 1024))


def _startup_inclusive_peak_mib(torch_module: Any, startup_peak_mib: float) -> float:
    """Keep initialization peaks even if a backend resets CUDA peak statistics."""
    return max(float(startup_peak_mib), _cuda_peak_mib(torch_module))


def _home() -> Path:
    return Path(os.environ["CYNTOX_AIRLLM_HOME"])


def _snapshot_path() -> Path:
    return _home() / "model"


def _shards_path() -> Path:
    return _home() / "layer-shards"


def _resident_path() -> Path:
    return _home() / "resident-model"


def _snapshot_manifest_path() -> Path:
    return _home() / "snapshot-manifest.json"


def _shard_manifest_path() -> Path:
    return _home() / "shard-manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_LIKE_TAG = re.compile(r"<\s*/?\s*([A-Za-z]+)", re.IGNORECASE)
_THINK_PROMPT_SUFFIX = "<think>\n"


def _render_thinking_prompt(tokenizer: Any, prompt: str) -> str:
    """Render the pinned chat template and verify that internal reasoning is enabled."""
    rendered = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}],
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=True,
    )
    if not isinstance(rendered, str) or not rendered.endswith(_THINK_PROMPT_SUFFIX):
        raise RuntimeError("thinking chat template did not open a reasoning span")
    return rendered


def _restore_template_think_prefix(generated_text: str) -> str:
    """Restore only the opening tag already present in the thinking chat prompt."""
    # The pinned Qwen 3.5 template ends a thinking-enabled generation prompt with
    # ``<think>\n``. Generated tokens therefore start inside that span. Never add
    # the closing tag here: the model must emit it and the sanitizer must see it.
    return "<think>\n" + generated_text


def _looks_like_reasoning_tag(text: str, index: int) -> bool:
    candidate = _THINK_LIKE_TAG.match(text, index)
    if candidate is not None:
        name = candidate.group(1).casefold()
        if name.startswith("think") or "think".startswith(name):
            return True
    remainder = text[index:].casefold()
    return len(remainder) > 1 and any(
        tag.startswith(remainder) for tag in (_THINK_OPEN, _THINK_CLOSE)
    )


def _strip_complete_reasoning_spans(text: str) -> str:
    visible: list[str] = []
    cursor = 0
    visible_start = 0
    inside_reasoning = False
    while (tag_start := text.find("<", cursor)) >= 0:
        remainder = text[tag_start:]
        if remainder.startswith(_THINK_OPEN):
            if inside_reasoning:
                raise ValueError("nested reasoning span")
            visible.append(text[visible_start:tag_start])
            inside_reasoning = True
            cursor = tag_start + len(_THINK_OPEN)
            continue
        if remainder.startswith(_THINK_CLOSE):
            if not inside_reasoning:
                raise ValueError("misordered reasoning span")
            inside_reasoning = False
            cursor = tag_start + len(_THINK_CLOSE)
            visible_start = cursor
            continue
        if _looks_like_reasoning_tag(text, tag_start):
            raise ValueError("malformed reasoning tag")
        cursor = tag_start + 1
    if inside_reasoning:
        raise ValueError("incomplete reasoning span")
    visible.append(text[visible_start:])
    return "".join(visible)


def _sanitize_generated_text(text: str) -> str:
    """Remove reasoning inside the worker so it never crosses the process boundary."""
    cleaned = _strip_complete_reasoning_spans(text).strip()
    if not cleaned:
        raise ValueError("empty final response")
    words = re.findall(r"\S+", cleaned)
    for width in range(1, min(128, len(words) // 4) + 1):
        tail = words[-width:]
        if all(words[-width * repeat : -width * (repeat - 1)] == tail for repeat in (2, 3, 4)):
            raise ValueError("repetition loop")
    return cleaned


SAFE_WORKER_VALIDATION_MESSAGES = frozenset(
    {
        "nested reasoning span",
        "misordered reasoning span",
        "malformed reasoning tag",
        "incomplete reasoning span",
        "empty final response",
        "repetition loop",
    }
)


def _complete_structured_generation(
    generate_once: Callable[[str, int, bool, int | None], tuple[str, int, int]],
    prompt: str,
    max_new_tokens: int,
    seed: int | None,
) -> tuple[str, int, int]:
    """Run validated private reasoning before a non-thinking JSON formatting pass."""
    reasoning_budget = min(512, max_new_tokens // 2)
    final_budget = max_new_tokens - reasoning_budget
    reasoning_continuation, reasoning_prompt_tokens, reasoning_tokens = generate_once(
        prompt,
        reasoning_budget,
        True,
        seed,
    )
    private_draft = _sanitize_generated_text(_restore_template_think_prefix(reasoning_continuation))
    final_prompt = (
        f"{prompt}\n\n"
        "Private draft (do not quote):\n"
        f"{private_draft[-4000:]}\n\n"
        "Now return only the requested JSON object, with no analysis or code fence."
    )
    final_text, final_prompt_tokens, final_tokens = generate_once(
        final_prompt,
        final_budget,
        False,
        None if seed is None else seed + 1,
    )
    return (
        final_text,
        reasoning_prompt_tokens + final_prompt_tokens,
        reasoning_tokens + final_tokens,
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid or missing runtime manifest: {path.name}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"runtime manifest is not an object: {path.name}")
    return payload


def _validate_file_entries(base: Path, entries: object, *, deep: bool) -> set[str]:
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("runtime manifest has no file entries")
    listed: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise RuntimeError("runtime manifest contains an invalid file entry")
        relative = raw.get("path")
        expected_size = raw.get("size")
        expected_hash = raw.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected_size, int):
            raise RuntimeError("runtime manifest file entry is incomplete")
        if relative in listed:
            raise RuntimeError(f"runtime manifest repeats file entry: {relative}")
        listed.add(relative)
        path = (base / relative).resolve()
        try:
            path.relative_to(base.resolve())
        except ValueError as error:
            raise RuntimeError("runtime manifest path escaped its root") from error
        if not path.is_file() or path.stat().st_size != expected_size:
            raise RuntimeError(f"runtime file size mismatch: {relative}")
        if deep and (not isinstance(expected_hash, str) or _sha256(path) != expected_hash):
            raise RuntimeError(f"runtime file hash mismatch: {relative}")
    return listed


def _runtime_file_inventory(base: Path) -> set[str]:
    """Inventory model artifacts while excluding Hugging Face's disposable cache."""
    return {
        path.relative_to(base).as_posix()
        for path in base.rglob("*")
        if path.is_file() and ".cache" not in path.relative_to(base).parts
    }


def _load_real_model() -> Any:
    import airllm.airllm_base as airllm_base
    import torch
    from airllm.airllm_qwen3_5 import AirLLMQwen3_5
    from transformers import AutoConfig, AutoTokenizer, GenerationConfig

    class LockedQwythos(AirLLMQwen3_5):
        def set_layer_names_dict(self):  # type: ignore[no-untyped-def]
            super().set_layer_names_dict()
            # This authenticated worker exposes text only. Keeping the 870 MiB vision tower
            # resident wastes scarce VRAM and provides no reachable capability.
            self.layer_names_dict["resident"] = []

        def get_generation_config(self):  # type: ignore[no-untyped-def]
            return GenerationConfig.from_pretrained(
                self.model_local_path,
                local_files_only=True,
            )

        def get_tokenizer(self, hf_token=None):  # type: ignore[no-untyped-def]
            return AutoTokenizer.from_pretrained(
                self.model_local_path,
                token=hf_token,
                trust_remote_code=False,
                local_files_only=True,
            )

    original_from_pretrained = AutoConfig.from_pretrained

    def locked_config(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("trust_remote_code") is True:
            raise RuntimeError("remote model code is disabled for locked Qwythos")
        kwargs["trust_remote_code"] = False
        kwargs["local_files_only"] = True
        return original_from_pretrained(*args, **kwargs)

    # AirLLM 3.3.0 retries AutoConfig with trust_remote_code=True after any native-load
    # exception. Replace that call boundary so the unsafe branch cannot execute at all.
    airllm_base.AutoConfig.from_pretrained = locked_config
    airllm_base.find_or_create_local_splitted_path = lambda *_args, **_kwargs: (
        _snapshot_path(),
        _shards_path() / "splitted_model",
    )
    return LockedQwythos(
        _snapshot_path(),
        dtype=torch.bfloat16,
        max_seq_len=CONTEXT_LIMIT,
        layer_shards_saving_path=_shards_path(),
        compression=None,
        prefetching=True,
        delete_original=False,
    )


def _seed_torch(seed: int | None) -> None:
    if seed is None:
        return
    import torch

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _generation_kwargs(max_new_tokens: int) -> dict[str, Any]:
    return {
        "max_new_tokens": min(max_new_tokens, 2048),
        "do_sample": True,
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "repetition_penalty": 1.05,
    }


def _allowed_completion_tokens(max_new_tokens: int, *, structured: bool) -> int:
    requested = min(max_new_tokens, 2048)
    if structured:
        return requested
    return requested + min(PRIVATE_REASONING_PROBE_TOKENS, max(1, requested // 4))


def resident_parameter_name(source_name: str) -> str:
    if source_name.startswith("model.language_model."):
        return "model." + source_name.removeprefix("model.language_model.")
    return source_name


def is_resident_text_shard(path: Path) -> bool:
    return path.name == "lm_head.safetensors" or path.name.startswith("model.language_model.")


def _split_checkpoint() -> dict[str, Any]:
    """Split the single 19 GB safetensors file without materializing it as one state dict."""
    from safetensors import safe_open
    from safetensors.torch import save_file

    source = _snapshot_path() / "model.safetensors"
    destination = _shards_path() / "splitted_model"
    destination.mkdir(parents=True, exist_ok=True)
    with safe_open(str(source), framework="pt", device="cpu") as handle:
        keys = set(handle.keys())
        layer_numbers = sorted(
            {
                int(match.group(1))
                for key in keys
                if (match := re.match(r"model\.language_model\.layers\.(\d+)\.", key))
            }
        )
        module_names = ["model.language_model.embed_tokens"]
        module_names.extend(f"model.language_model.layers.{index}" for index in layer_numbers)
        module_names.extend(["model.language_model.norm", "lm_head", "model.visual"])
        covered: set[str] = set()
        weight_map: dict[str, str] = {}
        shard_entries: list[dict[str, Any]] = []
        for module_name in module_names:
            output = destination / f"{module_name}.safetensors"
            done = destination / f"{module_name}.safetensors.done"
            prefix = module_name + "."
            selected = sorted(key for key in keys if key == module_name or key.startswith(prefix))
            if not selected:
                continue
            with contextlib.suppress(FileNotFoundError):
                done.unlink()
            tensors = {key: handle.get_tensor(key) for key in selected}
            temporary = output.with_suffix(output.suffix + ".tmp")
            save_file(tensors, temporary)
            os.replace(temporary, output)
            done.touch()
            print(
                f"split {module_name} ({len(selected)} tensors)",
                file=sys.stderr,
                flush=True,
            )
            del tensors
            gc.collect()
            relative = output.relative_to(destination).as_posix()
            covered.update(selected)
            weight_map.update(dict.fromkeys(selected, f"../layer-shards/splitted_model/{relative}"))
            shard_entries.append(
                {
                    "path": relative,
                    "size": output.stat().st_size,
                    "sha256": _sha256(output),
                    "tensor_count": len(selected),
                    "tensors": selected,
                }
            )

    excluded = sorted(keys - covered)
    if any(not key.startswith("mtp.") for key in excluded):
        raise RuntimeError(f"checkpoint tensors were not assigned to shards: {excluded[:5]}")
    if len(covered) != len(weight_map):
        raise RuntimeError("checkpoint tensor appeared in more than one layer shard")
    expected_shards = {entry["path"] for entry in shard_entries}
    for candidate in destination.glob("*.safetensors"):
        if candidate.name not in expected_shards:
            candidate.unlink()
    for candidate in destination.glob("*.safetensors.done"):
        shard_name = candidate.name.removesuffix(".done")
        if shard_name not in expected_shards:
            candidate.unlink()
    manifest = {
        "model_id": MODEL_ID,
        "revision": REVISION,
        "source_path": "model.safetensors",
        "source_sha256": _sha256(source),
        "source_tensor_count": len(keys),
        "sharded_tensor_count": len(covered),
        "excluded_tensor_count": len(excluded),
        "excluded_tensors": excluded,
        "excluded_policy": "Only optional MTP draft-head tensors may be excluded.",
        "files": shard_entries,
    }
    _atomic_json(_shard_manifest_path(), manifest)
    _atomic_json(
        _resident_path() / "model.safetensors.index.json",
        {
            "metadata": {"total_size": sum(entry["size"] for entry in shard_entries)},
            "weight_map": weight_map,
        },
    )
    return manifest


def _validate_runtime(*, deep: bool = True) -> dict[str, Any]:
    snapshot = _load_json(_snapshot_manifest_path())
    shard_manifest_sha256 = _sha256(_shard_manifest_path())
    if (
        snapshot.get("model_id") != MODEL_ID
        or snapshot.get("revision") != REVISION
        or snapshot.get("actual_snapshot_bytes") != EXPECTED_BYTES
        or snapshot.get("architecture") != "Qwen3_5ForConditionalGeneration"
        or snapshot.get("trust_remote_code") is not False
        or snapshot.get("offline_reload_proven") is not True
        or snapshot.get("shard_manifest_sha256") != shard_manifest_sha256
    ):
        raise RuntimeError("snapshot manifest does not match locked Qwythos")
    listed_snapshot = _validate_file_entries(_snapshot_path(), snapshot.get("files"), deep=deep)
    if listed_snapshot != _runtime_file_inventory(_snapshot_path()):
        raise RuntimeError("snapshot directory contains unlisted or missing files")
    if sum((_snapshot_path() / path).stat().st_size for path in listed_snapshot) != EXPECTED_BYTES:
        raise RuntimeError("snapshot file inventory does not match the locked byte count")
    shards = _load_json(_shard_manifest_path())
    if (
        shards.get("model_id") != MODEL_ID
        or shards.get("revision") != REVISION
        or shards.get("source_sha256")
        != next(
            (
                entry.get("sha256")
                for entry in snapshot["files"]
                if isinstance(entry, dict) and entry.get("path") == "model.safetensors"
            ),
            None,
        )
        or shards.get("excluded_tensor_count") != len(shards.get("excluded_tensors", []))
        or any(not str(key).startswith("mtp.") for key in shards.get("excluded_tensors", []))
    ):
        raise RuntimeError("layer-shard manifest does not match locked Qwythos")
    shard_root = _shards_path() / "splitted_model"
    listed_shards = _validate_file_entries(shard_root, shards.get("files"), deep=deep)
    actual_shards = {
        path.relative_to(shard_root).as_posix()
        for path in shard_root.rglob("*.safetensors")
        if path.is_file()
    }
    if actual_shards != listed_shards:
        raise RuntimeError("layer-shard directory contains unlisted or missing files")
    actual_done = {
        path.relative_to(shard_root).as_posix().removesuffix(".done")
        for path in shard_root.glob("*.safetensors.done")
        if path.is_file()
    }
    if actual_done != listed_shards:
        raise RuntimeError("layer-shard completion markers do not match the shard inventory")
    manifest_tensors: set[str] = set()
    expected_weight_map: dict[str, str] = {}
    files = shards.get("files")
    if not isinstance(files, list):
        raise RuntimeError("layer-shard manifest has no tensor inventory")
    for entry in files:
        if not isinstance(entry, dict):
            raise RuntimeError("layer-shard manifest contains an invalid entry")
        tensors = entry.get("tensors")
        path = entry.get("path")
        if (
            not isinstance(tensors, list)
            or not all(isinstance(name, str) for name in tensors)
            or entry.get("tensor_count") != len(tensors)
            or not isinstance(path, str)
        ):
            raise RuntimeError("layer-shard tensor inventory is invalid")
        overlap = manifest_tensors.intersection(tensors)
        if overlap:
            raise RuntimeError(f"layer-shard tensor inventory overlaps: {sorted(overlap)[:5]}")
        manifest_tensors.update(tensors)
        expected_weight_map.update(dict.fromkeys(tensors, f"../layer-shards/splitted_model/{path}"))
        if deep:
            from safetensors import safe_open

            with safe_open(str(shard_root / path), framework="pt", device="cpu") as shard:
                if set(shard.keys()) != set(tensors):
                    raise RuntimeError(f"layer-shard tensor inventory mismatch: {path}")
    excluded_tensors = set(shards.get("excluded_tensors", []))
    if len(manifest_tensors) != shards.get("sharded_tensor_count") or len(manifest_tensors) + len(
        excluded_tensors
    ) != shards.get("source_tensor_count"):
        raise RuntimeError("layer-shard tensor coverage counts are inconsistent")
    index = _load_json(_resident_path() / "model.safetensors.index.json")
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, dict) or weight_map != expected_weight_map:
        raise RuntimeError("resident checkpoint index does not cover every model tensor")
    return {
        "snapshot_manifest_sha256": _sha256(_snapshot_manifest_path()),
        "shard_manifest_sha256": shard_manifest_sha256,
        "sharded_tensor_count": shards.get("sharded_tensor_count"),
    }


class FakeModel:
    architecture = "Qwen3_5ForConditionalGeneration"
    backend_kind = "fake"
    backend_name = "fake"
    implementation_class = "fake"
    context_limit = CONTEXT_LIMIT

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        prompt: str,
        max_new_tokens: int,
        seed: int | None,
        *,
        structured: bool = False,
    ) -> tuple[str, int, int, float]:
        del max_new_tokens
        del seed
        del structured
        self.calls += 1
        mode = os.environ.get("CYNTOX_AIRLLM_FAKE_MODE")
        if mode == "crash":
            os._exit(91)
        if mode == "oom":
            raise RuntimeError("CUDA out of memory (simulated)")
        if mode == "oom-once" and self.calls == 1:
            raise RuntimeError("CUDA out of memory (simulated once)")
        if mode == "timeout":
            time.sleep(float(os.environ.get("CYNTOX_AIRLLM_FAKE_DELAY", "2")))
        if mode == "malformed":
            return "<think>unterminated hidden reasoning", 10, 8, 64.0
        if mode == "bind-attempt":
            with socket.socket() as listener:
                listener.bind(("0.0.0.0", 0))  # noqa: S104 - deliberate denied-bind test
        if mode == "network-attempt":
            with socket.socket() as client:
                client.settimeout(0.1)
                client.connect(("127.0.0.1", 9))
        if mode == "udp-attempt":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.sendto(b"blocked", ("127.0.0.1", 9))
        if mode == "process-attempt":
            subprocess.run([sys.executable, "-c", "pass"], check=True)  # noqa: S603
        if mode == "invalid-schema" and "Return only JSON matching this schema" in prompt:
            return '<think>private schema reasoning</think>{"ok": "not-a-boolean"}', 10, 8, 64.0
        if mode == "stdout-flood":
            for _ in range(512):
                print("stdout-flood:" + ("x" * 4096))
            sys.stdout.flush()
        if mode == "oversized-response":
            return "x" * (MAX_RESPONSE_BYTES + 1), 10, 8, 64.0
        if "Return only JSON matching this schema" in prompt:
            return '<think>private schema reasoning</think>{"ok": true}', 10, 8, 64.0
        return (
            f"<think>private test reasoning</think>Fake Qwythos response for {len(prompt)} chars.",
            10,
            8,
            64.0,
        )


class RealModel:
    architecture = "Qwen3_5ForConditionalGeneration"
    backend_kind = "real"
    backend_name = "airllm"
    context_limit = CONTEXT_LIMIT

    def __init__(self) -> None:
        self.air = _load_real_model()
        if getattr(self.air, "trust_remote_code", True) is not False:
            raise RuntimeError("locked Qwythos load unexpectedly enabled remote code")
        expected_base = "airllm.airllm_qwen3_5.AirLLMQwen3_5"
        implementation_bases = {
            f"{base.__module__}.{base.__qualname__}" for base in type(self.air).mro()
        }
        if expected_base not in implementation_bases:
            raise RuntimeError(
                f"AirLLM did not select its locked Qwen3.5 implementation: expected {expected_base}"
            )
        self.implementation_class = expected_base

    def complete(
        self,
        prompt: str,
        max_new_tokens: int,
        seed: int | None,
        *,
        structured: bool = False,
    ) -> tuple[str, int, int, float]:
        import torch

        tokenizer = self.air.tokenizer

        def generate_once(
            generation_prompt: str,
            token_budget: int,
            thinking: bool,
            generation_seed: int | None,
        ) -> tuple[str, int, int]:
            rendered = (
                _render_thinking_prompt(tokenizer, generation_prompt)
                if thinking
                else tokenizer.apply_chat_template(
                    [{"role": "user", "content": generation_prompt}],
                    tokenize=False,
                    add_generation_prompt=True,
                    enable_thinking=False,
                )
            )
            inputs = tokenizer(rendered, return_tensors="pt", add_special_tokens=False)
            prompt_count = int(inputs.input_ids.shape[-1])
            if prompt_count + token_budget > CONTEXT_LIMIT:
                raise ValueError("request exceeds the locked 32768-token context cap")
            inputs = {key: value.to("cuda:0") for key, value in inputs.items()}
            _seed_torch(generation_seed)
            with torch.inference_mode():
                output = self.air.generate(**inputs, **_generation_kwargs(token_budget))
            generated = output[0, prompt_count:]
            return (
                tokenizer.decode(generated, skip_special_tokens=True),
                prompt_count,
                int(generated.shape[-1]),
            )

        if structured and max_new_tokens >= 64:
            text, prompt_tokens, completion_tokens = _complete_structured_generation(
                generate_once,
                prompt,
                max_new_tokens,
                seed,
            )
        else:
            reasoning_budget = min(PRIVATE_REASONING_PROBE_TOKENS, max(1, max_new_tokens // 4))
            generated_text, prompt_tokens, completion_tokens = generate_once(
                prompt,
                reasoning_budget,
                True,
                seed,
            )
            text = _restore_template_think_prefix(generated_text)
            try:
                _sanitize_generated_text(text)
            except ValueError as error:
                if str(error) != "incomplete reasoning span":
                    raise
                final_prompt = (
                    f"{prompt}\n\n"
                    "Return final answer only. No analysis, no hidden reasoning, no code fence."
                )
                final_text, final_prompt_tokens, final_tokens = generate_once(
                    final_prompt,
                    max_new_tokens,
                    False,
                    None if seed is None else seed + 1,
                )
                text = final_text
                prompt_tokens += final_prompt_tokens
                completion_tokens += final_tokens
        peak = _startup_inclusive_peak_mib(
            torch, float(getattr(self, "startup_peak_vram_mib", 0.0))
        )
        return text, prompt_tokens, completion_tokens, float(peak)


class ResidentModel:
    """Keep the text-only model resident on CUDA; never load the unused vision tower."""

    architecture = "Qwen3_5ForConditionalGeneration"
    backend_kind = "real"
    backend_name = "transformers-resident"
    context_limit = RESIDENT_CONTEXT_LIMIT

    def __init__(self) -> None:
        import torch
        from accelerate import init_empty_weights
        from accelerate.utils import set_module_tensor_to_device
        from safetensors import safe_open
        from transformers import AutoConfig, AutoTokenizer, GenerationConfig, Qwen3_5ForCausalLM

        config = AutoConfig.from_pretrained(
            _snapshot_path(),
            trust_remote_code=False,
            local_files_only=True,
        )
        with init_empty_weights(include_buffers=False):
            self.model = Qwen3_5ForCausalLM(config.text_config)
        self.implementation_class = f"{type(self.model).__module__}.{type(self.model).__qualname__}"
        expected_class = "transformers.models.qwen3_5.modeling_qwen3_5.Qwen3_5ForCausalLM"
        if self.implementation_class != expected_class:
            raise RuntimeError(
                "resident backend did not select the locked Qwen3.5 implementation: "
                f"expected {expected_class}, got {self.implementation_class}"
            )
        expected = {name for name, _ in self.model.named_parameters()}
        loaded: set[str] = set()
        shard_dir = _shards_path() / "splitted_model"
        shard_manifest = _load_json(_shard_manifest_path())
        raw_entries = shard_manifest.get("files")
        if not isinstance(raw_entries, list):
            raise RuntimeError("resident shard manifest has no files")
        shard_entries = sorted(
            (
                entry
                for entry in raw_entries
                if isinstance(entry, dict)
                and isinstance(entry.get("path"), str)
                and is_resident_text_shard(Path(str(entry["path"])))
            ),
            key=lambda entry: str(entry["path"]),
        )
        for entry in shard_entries:
            path = shard_dir / str(entry["path"])
            expected_sources = entry.get("tensors")
            if not isinstance(expected_sources, list):
                raise RuntimeError(f"resident shard has no tensor inventory: {path.name}")
            with safe_open(str(path), framework="pt", device="cpu") as shard:
                if set(shard.keys()) != set(expected_sources):
                    raise RuntimeError(f"resident shard tensor inventory mismatch: {path.name}")
                for source_name in tuple(shard.keys()):
                    target_name = resident_parameter_name(source_name)
                    if target_name in loaded:
                        raise RuntimeError(f"resident parameter was loaded twice: {target_name}")
                    value = shard.get_tensor(source_name)
                    set_module_tensor_to_device(
                        self.model,
                        target_name,
                        "cuda:0",
                        value=value,
                        dtype=torch.bfloat16,
                    )
                    loaded.add(target_name)
                    del value
            gc.collect()
        if loaded != expected:
            missing = sorted(expected - loaded)
            unexpected = sorted(loaded - expected)
            raise RuntimeError(
                "resident parameter coverage mismatch: "
                f"missing={missing[:5]} unexpected={unexpected[:5]}"
            )
        self.model.to("cuda:0")
        if any(parameter.is_meta for parameter in self.model.parameters()) or any(
            getattr(buffer, "is_meta", False) for buffer in self.model.buffers()
        ):
            raise RuntimeError("resident model retained meta tensors after loading")
        self.model.eval()
        self.model.generation_config = GenerationConfig.from_pretrained(
            _snapshot_path(), local_files_only=True
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            _snapshot_path(),
            trust_remote_code=False,
            local_files_only=True,
        )

    def complete(
        self,
        prompt: str,
        max_new_tokens: int,
        seed: int | None,
        *,
        structured: bool = False,
    ) -> tuple[str, int, int, float]:
        import torch

        def generate_once(
            generation_prompt: str,
            token_budget: int,
            thinking: bool,
            generation_seed: int | None,
        ) -> tuple[str, int, int]:
            rendered = (
                _render_thinking_prompt(self.tokenizer, generation_prompt)
                if thinking
                else self.tokenizer.apply_chat_template(
                    [{"role": "user", "content": generation_prompt}],
                    tokenize=False,
                    add_generation_prompt=True,
                    enable_thinking=False,
                )
            )
            inputs = self.tokenizer(rendered, return_tensors="pt", add_special_tokens=False)
            prompt_count = int(inputs.input_ids.shape[-1])
            if prompt_count + token_budget > RESIDENT_CONTEXT_LIMIT:
                raise ValueError("request exceeds the resident 8192-token context cap")
            inputs = {key: value.to("cuda:0") for key, value in inputs.items()}
            _seed_torch(generation_seed)
            with torch.inference_mode():
                output = self.model.generate(**inputs, **_generation_kwargs(token_budget))
            generated = output[0, prompt_count:]
            return (
                self.tokenizer.decode(generated, skip_special_tokens=True),
                prompt_count,
                int(generated.shape[-1]),
            )

        if structured and max_new_tokens >= 64:
            text, prompt_tokens, completion_tokens = _complete_structured_generation(
                generate_once,
                prompt,
                max_new_tokens,
                seed,
            )
        else:
            generated_text, prompt_tokens, completion_tokens = generate_once(
                prompt,
                max_new_tokens,
                True,
                seed,
            )
            text = _restore_template_think_prefix(generated_text)
        peak = _startup_inclusive_peak_mib(
            torch, float(getattr(self, "startup_peak_vram_mib", 0.0))
        )
        return text, prompt_tokens, completion_tokens, float(peak)


def prepare(root: Path, *, fresh_snapshot: bool = False) -> int:
    import torch
    from huggingface_hub import snapshot_download
    from transformers import AutoConfig

    home = _home()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("the selected GPU does not support BF16")
    home.mkdir(parents=True, exist_ok=True)
    snapshot = _snapshot_path()
    snapshot_download(
        repo_id=MODEL_ID,
        revision=REVISION,
        local_dir=snapshot,
        local_dir_use_symlinks=False,
        force_download=fresh_snapshot,
    )
    files = []
    total = 0
    for path in sorted(
        item
        for item in snapshot.rglob("*")
        if item.is_file() and ".cache" not in item.relative_to(snapshot).parts
    ):
        size = path.stat().st_size
        total += size
        files.append(
            {"path": path.relative_to(snapshot).as_posix(), "size": size, "sha256": _sha256(path)}
        )
    if total != EXPECTED_BYTES:
        raise RuntimeError(f"snapshot size mismatch: expected {EXPECTED_BYTES}, got {total}")
    config = AutoConfig.from_pretrained(snapshot, trust_remote_code=False, local_files_only=True)
    if "Qwen3_5ForConditionalGeneration" not in (getattr(config, "architectures", None) or []):
        raise RuntimeError(f"unexpected architecture: {getattr(config, 'architectures', None)}")
    partial_manifest = {
        "model_id": MODEL_ID,
        "revision": REVISION,
        "precision": "bf16",
        "expected_snapshot_bytes": EXPECTED_BYTES,
        "actual_snapshot_bytes": total,
        "architecture": "Qwen3_5ForConditionalGeneration",
        "trust_remote_code": False,
        "native_optional_kernels": False,
        "runtime_lock_sha256": _sha256(root / "runtimes" / "airllm" / "requirements.lock.txt"),
        "offline_reload_proven": False,
        "files": files,
    }
    _atomic_json(_snapshot_manifest_path(), partial_manifest)
    shard_manifest = _split_checkpoint()
    with contextlib.redirect_stdout(sys.stderr):
        first = _load_real_model()
        del first
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        second = _load_real_model()
        del second
    manifest = {
        **partial_manifest,
        "offline_reload_proven": True,
        "shard_manifest_sha256": _sha256(_shard_manifest_path()),
        "sharded_tensor_count": shard_manifest["sharded_tensor_count"],
        "excluded_mtp_tensor_count": shard_manifest["excluded_tensor_count"],
    }
    _atomic_json(_snapshot_manifest_path(), manifest)
    _validate_runtime(deep=True)
    return 0


def _install_network_guard() -> None:
    original_bind = socket.socket.bind

    def deny_network(_address: object = None) -> None:
        raise OSError("Qwythos inference worker is offline; outbound network is blocked")

    def require_loopback(address: object) -> None:
        host = address[0] if isinstance(address, tuple) and address else ""
        if str(host) != "127.0.0.1":
            raise OSError("Qwythos inference worker may bind only to 127.0.0.1")

    def loopback_bind(sock: socket.socket, address: object) -> Any:
        require_loopback(address)
        return original_bind(sock, address)  # type: ignore[arg-type]

    def offline_connect(sock: socket.socket, address: object) -> Any:
        del sock
        deny_network(address)

    def offline_connect_ex(sock: socket.socket, address: object) -> int:
        del sock
        deny_network(address)
        return 1

    def offline_sendto(sock: socket.socket, data: bytes, *args: object) -> int:
        del sock, data
        deny_network(args[-1] if args else None)
        return 0

    def offline_sendmsg(sock: socket.socket, *args: object, **kwargs: object) -> int:
        del sock, args, kwargs
        deny_network()
        return 0

    def local_getaddrinfo(host: object, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        if host is not None:
            deny_network(host)
        raise OSError("Qwythos inference worker DNS is disabled")

    socket.socket.connect = offline_connect  # type: ignore[assignment,method-assign]
    socket.socket.connect_ex = offline_connect_ex  # type: ignore[assignment,method-assign]
    socket.socket.bind = loopback_bind  # type: ignore[assignment,method-assign]
    socket.socket.sendto = offline_sendto  # type: ignore[assignment,method-assign]
    if hasattr(socket.socket, "sendmsg"):
        socket.socket.sendmsg = offline_sendmsg
    socket.getaddrinfo = local_getaddrinfo

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event == "socket.bind" and len(args) >= 2:
            require_loopback(args[1])
        if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo"}:
            deny_network(args[-1] if args else None)

    sys.addaudithook(audit)


def _install_process_guard() -> None:
    blocked = {
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.system",
        "os.spawn",
        "os.startfile",
        "os.posix_spawn",
        "os.posix_spawnp",
        "subprocess.Popen",
    }

    def audit(event: str, _args: tuple[object, ...]) -> None:
        if event in blocked:
            raise PermissionError("Qwythos inference worker cannot create child processes")

    sys.addaudithook(audit)


def _install_parent_death_guard() -> None:
    """Terminate the worker if its provider disappears without a normal shutdown."""
    if os.name == "nt":
        return
    parent_pid = os.getppid()
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None)
        if libc.prctl(1, signal.SIGKILL) != 0:  # PR_SET_PDEATHSIG
            raise OSError("failed to configure the AirLLM parent-death signal")
        if os.getppid() != parent_pid:
            os._exit(92)
        return

    def watch_parent() -> None:
        while os.getppid() == parent_pid and parent_pid > 1:
            time.sleep(1)
        os._exit(92)

    threading.Thread(target=watch_parent, name="airllm-parent-watch", daemon=True).start()


def serve(backend_name: str, *, qualification_mode: bool = False) -> int:
    _install_parent_death_guard()
    token = secrets.token_urlsafe(32)
    session_id = secrets.token_hex(16)
    fake = os.environ.get("CYNTOX_AIRLLM_FAKE") == "1"
    fake_mode = os.environ.get("CYNTOX_AIRLLM_FAKE_MODE") if fake else None
    if fake_mode and fake_mode not in FAKE_MODES:
        raise RuntimeError(f"unsupported fake worker mode: {fake_mode}")
    if fake_mode == "startup-crash":
        os._exit(90)
    if fake_mode == "startup-hang":
        time.sleep(float(os.environ.get("CYNTOX_AIRLLM_FAKE_DELAY", "2")))

    descendant: subprocess.Popen[bytes] | None = None
    if fake_mode == "spawned-descendant":
        descendant = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    integrity: dict[str, Any]
    if fake:
        integrity = {
            "snapshot_manifest_sha256": "fake",
            "shard_manifest_sha256": "fake",
            "sharded_tensor_count": 0,
        }
    else:
        integrity = _validate_runtime(deep=True)
    _install_network_guard()
    startup_peak_vram_mib = 0.0
    if fake:
        gpu_uuid = os.environ.get("CYNTOX_GPU_UUID") or "unknown-gpu"
        torch_module: Any | None = None
    else:
        import torch

        torch_module = torch
        gpu_uuid = _verified_gpu_uuid(torch, os.environ.get("CYNTOX_GPU_UUID"))
        torch.cuda.reset_peak_memory_stats(0)
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        if fake:
            backend: Any = FakeModel()
        elif backend_name == "resident":
            backend = ResidentModel()
        else:
            backend = RealModel()
    if torch_module is not None:
        startup_peak_vram_mib = _cuda_peak_mib(torch_module)
    backend.startup_peak_vram_mib = startup_peak_vram_mib
    noise = capture.getvalue()
    if noise:
        print(noise, file=sys.stderr, end="", flush=True)
    _install_process_guard()
    root = Path(os.environ["CYNTOX_PROJECT_ROOT"])
    runtime_lock_sha256 = _sha256(root / "runtimes" / "airllm" / "requirements.lock.txt")
    identity = {
        "model_id": MODEL_ID,
        "revision": REVISION,
        "architecture": backend.architecture,
        "backend_kind": backend.backend_kind,
        "backend_name": backend.backend_name,
        "backend_class": f"{type(backend).__module__}.{type(backend).__qualname__}",
        "implementation_class": backend.implementation_class,
        "precision": "bf16",
        "context_limit": backend.context_limit,
        "gpu_uuid": gpu_uuid,
        "startup_peak_vram_mib": startup_peak_vram_mib,
        "runtime_lock_sha256": runtime_lock_sha256,
        "session_id": session_id,
        "qualification_mode": qualification_mode,
        "offline_environment": os.environ.get("HF_HUB_OFFLINE") == "1"
        and os.environ.get("TRANSFORMERS_OFFLINE") == "1",
        **integrity,
    }
    if descendant is not None:
        identity["test_descendant_pid"] = descendant.pid
    if fake_mode == "wrong-identity":
        identity["model_id"] = "invalid/wrong-model"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(10.0)

        def log_message(self, *_: object) -> None:
            return

        def _reply(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            if len(body) > MAX_RESPONSE_BYTES:
                code = 500
                body = json.dumps({"error": "response_too_large"}).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, socket.timeout):
                self.wfile.write(body)

        def _authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}")

        def _qualification_fault(self) -> None:
            if not qualification_mode:
                self._reply(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except (TypeError, ValueError):
                self._reply(400, {"error": "invalid_content_length"})
                return
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._reply(413, {"error": "invalid_request_size"})
                return
            try:
                request = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self._reply(400, {"error": "invalid_json"})
                return
            fault = request.get("fault") if isinstance(request, dict) else None
            if fault == "crash":
                os._exit(93)
            if fault == "timeout":
                time.sleep(0.25)
                self._reply(200, {"session_id": session_id, "delayed": True})
                return
            if fault == "malformed":
                self._reply(
                    200,
                    {
                        "session_id": session_id,
                        "text": "<think>unterminated qualification reasoning",
                    },
                )
                return
            if fault == "network":
                try:
                    with socket.socket() as client:
                        client.connect(("192.0.2.1", 9))
                except OSError:
                    self._reply(200, {"session_id": session_id, "blocked": True})
                else:
                    self._reply(500, {"error": "network_guard_failed"})
                return
            if fault == "oom":
                self._reply(
                    500,
                    {
                        "error": "RuntimeError",
                        "message": "CUDA out of memory (qualification injection)",
                    },
                )
                return
            self._reply(400, {"error": "unsupported_qualification_fault"})

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            if self.path == "/health":
                if fake_mode == "health-delay":
                    time.sleep(float(os.environ.get("CYNTOX_AIRLLM_FAKE_DELAY", "2")))
                self._reply(200, {"ok": True, "pid": os.getpid(), **identity})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            if self.path == "/shutdown":
                self._reply(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path == "/qualification/fault":
                self._qualification_fault()
                return
            if self.path != "/generate":
                self._reply(404, {"error": "not found"})
                return
            try:
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except (TypeError, ValueError):
                    self._reply(400, {"error": "invalid_content_length"})
                    return
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    self._reply(413, {"error": "invalid_request_size"})
                    return
                raw_request = self.rfile.read(length)
                if len(raw_request) != length:
                    self._reply(400, {"error": "incomplete_request_body"})
                    return
                request = json.loads(raw_request)
                if not isinstance(request, dict) or not isinstance(request.get("prompt"), str):
                    raise ValueError("request must contain a string prompt")
                prompt = str(request["prompt"])
                requested_tokens = request.get("max_new_tokens", 2048)
                if (
                    not isinstance(requested_tokens, int)
                    or isinstance(requested_tokens, bool)
                    or not 1 <= requested_tokens <= 2048
                ):
                    raise ValueError("max_new_tokens must be between 1 and 2048")
                raw_seed = request.get("seed")
                if raw_seed is not None and (
                    isinstance(raw_seed, bool)
                    or not isinstance(raw_seed, int)
                    or not 0 <= raw_seed <= 2**63 - 1
                ):
                    raise ValueError("seed must be an integer between 0 and 2^63-1")
                structured = request.get("structured", False)
                if not isinstance(structured, bool):
                    raise ValueError("structured must be a boolean")
                started = time.monotonic()
                text, prompt_tokens, completion_tokens, peak = backend.complete(
                    prompt,
                    requested_tokens,
                    raw_seed,
                    structured=structured,
                )
                text = _sanitize_generated_text(text)
                elapsed = time.monotonic() - started
                if (
                    not isinstance(prompt_tokens, int)
                    or isinstance(prompt_tokens, bool)
                    or prompt_tokens <= 0
                    or not isinstance(completion_tokens, int)
                    or isinstance(completion_tokens, bool)
                    or not 0
                    < completion_tokens
                    <= _allowed_completion_tokens(requested_tokens, structured=structured)
                    or not isinstance(peak, int | float)
                    or isinstance(peak, bool)
                    or not math.isfinite(peak)
                    or peak < startup_peak_vram_mib
                    or not math.isfinite(elapsed)
                ):
                    raise RuntimeError("backend returned invalid resource measurements")
                self._reply(
                    200,
                    {
                        "text": text,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "elapsed_seconds": elapsed,
                        "peak_vram_mib": peak,
                        "backend_name": backend.backend_name,
                        "session_id": session_id,
                    },
                )
            except Exception as error:  # noqa: BLE001
                detail = str(error)
                if "out of memory" in detail.casefold():
                    message = "CUDA out of memory"
                elif isinstance(error, ValueError) and detail in SAFE_WORKER_VALIDATION_MESSAGES:
                    message = detail[:500] or "validation failed"
                else:
                    message = "worker generation failed"
                self._reply(500, {"error": type(error).__name__, "message": message})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    if fake_mode == "bad-handshake":
        print("{not-valid-json", flush=True)
    else:
        print(
            json.dumps(
                {
                    "protocol": 1,
                    "port": server.server_port,
                    "token": token,
                    "pid": os.getpid(),
                    **identity,
                }
            ),
            flush=True,
        )
    server.serve_forever()
    server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--root", type=Path, required=True)
    prep.add_argument("--fresh-snapshot", action="store_true")
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--backend", choices=SUPPORTED_BACKENDS, default="airllm")
    serve_parser.add_argument("--qualification-mode", action="store_true")
    args = parser.parse_args()
    return (
        prepare(args.root, fresh_snapshot=args.fresh_snapshot)
        if args.command == "prepare"
        else serve(args.backend, qualification_mode=args.qualification_mode)
    )


if __name__ == "__main__":
    raise SystemExit(main())
