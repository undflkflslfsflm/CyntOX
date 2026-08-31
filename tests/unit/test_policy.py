from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from oslab.policy import (
    PathPolicy,
    PolicyDenied,
    detect_evaluator_exploit,
    redact,
    validate_qemu_network,
)


@given(st.lists(st.sampled_from(["a", "b", "c", ".."]), min_size=1, max_size=6))
def test_parent_components_are_never_authorized(parts: list[str]) -> None:
    root = Path.cwd()
    policy = PathPolicy.create([root], [root / "hidden"])
    candidate = Path(*parts)
    if ".." in parts:
        with pytest.raises(PolicyDenied):
            policy.authorize(candidate)


def test_denied_evaluator_root(tmp_path: Path) -> None:
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    policy = PathPolicy.create([tmp_path], [hidden])
    with pytest.raises(PolicyDenied, match="immutable"):
        policy.authorize(hidden / "ground-truth.json", write=True)


def test_network_policy_is_fail_closed() -> None:
    validate_qemu_network(["qemu-system-x86_64", "-nic", "none"])
    with pytest.raises(PolicyDenied):
        validate_qemu_network(["qemu-system-x86_64", "-netdev", "user,id=n0"])
    with pytest.raises(PolicyDenied):
        validate_qemu_network(["qemu-system-x86_64", "-display", "none"])


def test_redaction_and_evaluator_exploit_detection() -> None:
    assert "secret-value" not in redact("Authorization: Bearer secret-value")
    diff = "diff --git a/tests/hidden/score.py b/tests/hidden/score.py\n"
    assert "evaluator modification" in detect_evaluator_exploit(diff)
