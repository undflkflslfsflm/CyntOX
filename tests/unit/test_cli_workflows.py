# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from oslab import cli


def test_eval_group_callback_supports_spec_workflow(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    def fake_run(suite: str, seeds: str, base_commit: str) -> dict[str, object]:
        captured.update({"suite": suite, "seeds": seeds, "base_commit": base_commit})
        return {"ok": True, **captured}

    monkeypatch.setattr(cli, "_run_evaluation_payload", fake_run)

    result = CliRunner().invoke(
        cli.app,
        [
            "eval",
            "--suite",
            "seeded",
            "--seeds",
            "1,2,3",
            "--base-commit",
            "HEAD",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "base_commit": "HEAD",
        "ok": True,
        "seeds": "1,2,3",
        "suite": "seeded",
    }


def test_campaign_group_callback_supports_spec_workflow(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        target: str,
        budget: str,
        seed: int,
        iterations: int,
        live_fix: bool,
        base_commit: str,
    ) -> dict[str, object]:
        captured.update(
            {
                "target": target,
                "budget": budget,
                "seed": seed,
                "iterations": iterations,
                "live_fix": live_fix,
                "base_commit": base_commit,
            }
        )
        return {"ok": True, **captured}

    monkeypatch.setattr(cli, "_run_campaign_payload", fake_run)

    result = CliRunner().invoke(
        cli.app,
        [
            "campaign",
            "--target",
            "fixture",
            "--budget",
            "10m",
            "--seed",
            "7",
            "--iterations",
            "6",
            "--base-commit",
            "HEAD",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "base_commit": "HEAD",
        "budget": "10m",
        "iterations": 6,
        "live_fix": False,
        "ok": True,
        "seed": 7,
        "target": "fixture",
    }
