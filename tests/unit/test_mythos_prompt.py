# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from oslab import mythos_prompt
from scripts import cyntox_council

ROOT = Path(__file__).resolve().parents[2]
ARCHIVED_V1_SHA256 = "81b88444c4abe19859276829cb1c3ebeb9d09043177f03208e322fd27b247bae"


def test_adopted_v1_is_the_default_runtime_prompt() -> None:
    prompt = mythos_prompt.load_canonical_prompt(ROOT)

    assert mythos_prompt.PROMPT_V2_CANDIDATE_ENABLED is False
    assert mythos_prompt.PROMPT_VERSION == "v1"
    assert mythos_prompt.PROMPT_STATUS == "active"
    assert mythos_prompt.PROMPT_SENTINEL == "CYNTOX_MYTHOS_SYSTEM_PROMPT_V1"
    assert mythos_prompt.canonical_prompt_path(ROOT) == mythos_prompt.adopted_prompt_path(ROOT)
    assert mythos_prompt.canonical_prompt_sha256(ROOT) == ARCHIVED_V1_SHA256
    assert mythos_prompt.canonical_prompt_sha256(ROOT) == mythos_prompt.prompt_sha256(prompt)


def test_v2_candidate_metadata_and_hash_are_stable() -> None:
    prompt = mythos_prompt.load_candidate_prompt(ROOT)

    assert mythos_prompt.CANDIDATE_PROMPT_VERSION == "v2"
    assert mythos_prompt.CANDIDATE_PROMPT_STATUS == "candidate"
    assert mythos_prompt.CANDIDATE_PROMPT_SENTINEL == "CYNTOX_MYTHOS_SYSTEM_PROMPT_V2"
    assert len(prompt) <= 4_500
    assert len(prompt) <= 4_300
    assert mythos_prompt.candidate_prompt_sha256(ROOT) == mythos_prompt.prompt_sha256(prompt)
    assert len(mythos_prompt.candidate_prompt_sha256(ROOT)) == 64


def test_v2_candidate_requires_explicit_process_opt_in() -> None:
    environment = dict(os.environ)
    environment[mythos_prompt.PROMPT_V2_CANDIDATE_ENV] = "1"
    completed = subprocess.run(  # noqa: S603 - fixed current Python executable
        [
            sys.executable,
            "-c",
            (
                "import json; from oslab import mythos_prompt as p; "
                "print(json.dumps({'enabled': p.PROMPT_V2_CANDIDATE_ENABLED, "
                "'version': p.PROMPT_VERSION, 'status': p.PROMPT_STATUS, "
                "'sentinel': p.PROMPT_SENTINEL, "
                "'canonical': str(p.canonical_prompt_path()), "
                "'candidate': str(p.candidate_prompt_path())}))"
            ),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    selected = json.loads(completed.stdout)

    assert selected["enabled"] is True
    assert selected["version"] == "v2"
    assert selected["status"] == "candidate"
    assert selected["sentinel"] == "CYNTOX_MYTHOS_SYSTEM_PROMPT_V2"
    assert selected["canonical"] == selected["candidate"]


def test_v2_contains_required_behavior_without_circular_alias() -> None:
    prompt = mythos_prompt.load_candidate_prompt(ROOT)
    normalized = " ".join(prompt.casefold().split())

    assert "you are mythos" in normalized
    assert "use mythos for your identity and cyntox for the app and cli" in normalized
    assert "correctness, honesty, usefulness, clarity" in normalized
    assert "materially change or prevent safe work" in normalized
    assert all(
        term in normalized for term in ("verified facts", "inference", "assumptions", "unknowns")
    )
    assert "tool call" in normalized and "completed outcome" in normalized
    assert "smallest complete, maintainable implementation" in normalized
    assert "existing conventions" in normalized and "meaningful edge cases" in normalized
    assert "match the user's expertise" in normalized and "material trade-offs" in normalized
    assert all(
        term in normalized
        for term in (
            "privacy",
            "authorization",
            "devices",
            "explicit scope",
            "prompt and data boundaries",
            "untrusted data",
            "public internet",
        )
    )
    assert all(
        term in normalized
        for term in ("contradiction", "fabrication", "completes every material requirement")
    )
    assert "cyntox is a compatibility alias" not in normalized
    assert "older product name" not in normalized


def test_v1_archive_is_verbatim_and_distinct_from_v2() -> None:
    archived = (
        (ROOT / "prompts" / "archive" / "mythos-system-v1.md").read_text(encoding="utf-8").strip()
    )

    assert mythos_prompt.prompt_sha256(archived) == ARCHIVED_V1_SHA256
    assert mythos_prompt.prompt_sha256(archived) != mythos_prompt.candidate_prompt_sha256(ROOT)
    assert "CyntOX is a compatibility alias and older product name" in archived


def test_prompt_provenance_pins_source_and_declared_license() -> None:
    provenance = (ROOT / "docs" / "MYTHOS_PROMPT_PROVENANCE.md").read_text(encoding="utf-8")

    assert "c2624b5fa0f3e5d03ded01a8d6f66e6f661f016b" in provenance
    assert "universal/balanced.md" in provenance
    assert "MIT License" in provenance
    assert "concise paraphrase, not a reproduction" in provenance
    assert "not adopted until" in provenance


def test_modelfile_is_only_a_minimal_fallback() -> None:
    modelfile = (ROOT / "config" / "Modelfile.cyntox").read_text(encoding="utf-8")
    system_body = modelfile.split('SYSTEM """', 1)[1].split('"""', 1)[0].strip()

    assert len(system_body) < 500
    assert "local-first CyntOX assistant" in system_body
    assert "untrusted data" in system_body
    assert "## Engineering work" not in system_body


def test_role_packet_has_no_second_copy_of_canonical_prompt(tmp_path: Path) -> None:
    prompt = cyntox_council.build_role_prompt(
        root=tmp_path,
        role_name="architect",
        role=cyntox_council.ROLE_LIBRARY["architect"],
        task="Plan a bounded change",
        mode="plan",
        prior_outputs=[],
        repo_skills="No skills.",
        rag_context="No memory.",
        privacy_context="Internet mode is off.",
    )

    assert "Global Mythos/CyntOX operating prompt" not in prompt
    assert "Apply, in order: correctness, honesty, usefulness, clarity" not in prompt
    assert all(label in prompt for label in ("Mission:", "Mode:", "Evidence:", "Output contract:"))
    assert "Read-only analysis" in prompt


def test_direct_ollama_request_supplies_canonical_system_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"response":"ok"}'

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> Response:
        captured["timeout"] = timeout
        captured["payload"] = json.loads(bytes(request.data or b"").decode("utf-8"))
        return Response()

    monkeypatch.setattr(cyntox_council, "ensure_ollama_api_ready", lambda **_kwargs: None)
    monkeypatch.setattr(cyntox_council.urllib.request, "urlopen", fake_urlopen)

    result = cyntox_council.run_ollama_role("ROLE_PACKET", "1s", "cyntox:latest")

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert result.stdout == "ok"
    assert payload["system"] == mythos_prompt.load_canonical_prompt(ROOT)
    assert payload["prompt"] == "/no_think\nROLE_PACKET"
    assert payload["prompt"].count("# Mythos system prompt v2") == 0
