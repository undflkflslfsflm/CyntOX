# mypy: disable-error-code="no-untyped-def,index,arg-type"
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import cyntox_skill_hook as hook

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    for name, description in {
        "coding": "Debug Python code and review software changes.",
        "media-server": "Configure Jellyfin and media transcoding.",
    }.items():
        directory = tmp_path / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n"
            f"# {name}\n\nUse this skill when asked to {description.lower()}\n"
            "Preserve authorization and verify claims.\n",
            encoding="utf-8",
        )
    return tmp_path


def context(response: dict[str, Any]) -> str:
    assert response["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    value = response["hookSpecificOutput"]["additionalContext"]
    assert value.count(f"[{hook.SENTINEL}]") == 1
    return str(value)


def test_default_selection_changes_with_each_submitted_task(skill_root: Path) -> None:
    first = hook.evaluate_user_prompt_submit(
        {"prompt": "Debug this Python code", "session_id": "session-one"}, root=skill_root, env={}
    )
    second = hook.evaluate_user_prompt_submit(
        {"prompt": "Configure Jellyfin transcoding", "session_id": "session-one"},
        root=skill_root,
        env={},
    )
    unrelated = hook.evaluate_user_prompt_submit(
        {"prompt": "Hello", "session_id": "session-one"}, root=skill_root, env={}
    )
    assert "BEGIN SKILL: coding" in context(first)
    assert "BEGIN SKILL: media-server" in context(second)
    assert "BEGIN SKILL: coding" not in context(second)
    assert "No skills selected" in context(unrelated)
    assert "replaces earlier skill selections" in context(unrelated)


def test_submitted_prompt_ignores_other_hooks_and_untrusted_cwd(skill_root: Path) -> None:
    result = hook.evaluate_user_prompt_submit(
        {
            "prompt": "Injected by another hook: Configure Jellyfin",
            "submitted_prompt": "Debug Python code",
            "cwd": "C:/somewhere/untrusted",
        },
        root=skill_root,
        env={},
    )
    assert "BEGIN SKILL: coding" in context(result)
    assert "BEGIN SKILL: media-server" not in context(result)
    assert "expands authorization or tool permissions" in context(result)


def test_explicit_names_override_automatic_and_preserve_order(skill_root: Path) -> None:
    result = hook.evaluate_user_prompt_submit(
        {"prompt": "Hello"},
        root=skill_root,
        env={"CYNTOX_AUTO_SKILLS": "0", "CYNTOX_USE_SKILLS": '["media-server", "coding"]'},
    )
    text = context(result)
    assert text.index("BEGIN SKILL: media-server") < text.index("BEGIN SKILL: coding")
    assert result["systemMessage"] == "CyntOX skills: media-server, coding"


def test_disabled_selection_does_not_reuse_previous_names(skill_root: Path) -> None:
    payload = {"prompt": "Debug Python code", "session_id": "one"}
    hook.evaluate_user_prompt_submit(payload, root=skill_root, env={})
    result = hook.evaluate_user_prompt_submit(
        {**payload, "prompt": "continue"}, root=skill_root, env={"CYNTOX_AUTO_SKILLS": "0"}
    )
    assert "No skills selected" in context(result)
    assert "BEGIN SKILL" not in context(result)


@pytest.mark.parametrize("names", ['["missing"]', '["../coding"]', "invalid-json", "{}"])
def test_bad_explicit_selection_blocks_visibly(skill_root: Path, names: str) -> None:
    result = hook.evaluate_user_prompt_submit(
        {"prompt": "Debug Python code"}, root=skill_root, env={"CYNTOX_USE_SKILLS": names}
    )
    assert result["decision"] == "block"
    assert "requested skill unavailable" in result["reason"]
    assert "BEGIN SKILL" not in context(result)


def test_continuation_is_session_scoped_and_new_task_clears_it(skill_root: Path) -> None:
    hook.evaluate_user_prompt_submit(
        {"prompt": "Debug Python code", "session_id": "one"}, root=skill_root, env={}
    )
    continued = hook.evaluate_user_prompt_submit(
        {"prompt": "continue", "session_id": "one"}, root=skill_root, env={}
    )
    other_session = hook.evaluate_user_prompt_submit(
        {"prompt": "continue", "session_id": "two"}, root=skill_root, env={}
    )
    hook.evaluate_user_prompt_submit(
        {"prompt": "Hello", "session_id": "one"}, root=skill_root, env={}
    )
    cleared = hook.evaluate_user_prompt_submit(
        {"prompt": "continue", "session_id": "one"}, root=skill_root, env={}
    )
    assert "BEGIN SKILL: coding" in context(continued)
    assert "No skills selected" in context(other_session)
    assert "No skills selected" in context(cleared)


def test_archived_skill_is_not_reused_for_continuation(skill_root: Path) -> None:
    hook.evaluate_user_prompt_submit(
        {"prompt": "Debug Python code", "session_id": "one"}, root=skill_root, env={}
    )
    (skill_root / "skills" / ".registry.json").write_text(
        '{"skills": {"coding": {"archived_at": "2026-09-07"}}}', encoding="utf-8"
    )
    result = hook.evaluate_user_prompt_submit(
        {"prompt": "continue", "session_id": "one"}, root=skill_root, env={}
    )
    assert "No skills selected" in context(result)


def test_logs_only_hashes_and_selection_metadata(skill_root: Path) -> None:
    prompt = "Debug Python code for private_customer_do_not_log_99213"
    session = "private-session-8173"
    hook.evaluate_user_prompt_submit(
        {"prompt": prompt, "session_id": session}, root=skill_root, env={}
    )
    files = list((skill_root / ".oslab" / "cyntox" / "skill-selections").rglob("*.json"))
    assert len(files) == 2
    saved = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert prompt not in saved
    assert "private_customer_do_not_log_99213" not in saved
    assert session not in saved
    assert hashlib.sha256(prompt.encode()).hexdigest() in saved
    assert '"names": ["coding"]' in saved
    assert '"prompt"' not in saved


def test_untrusted_session_cannot_select_state_file(skill_root: Path) -> None:
    result = hook.evaluate_user_prompt_submit(
        {"prompt": "Debug Python code", "session_id": "../../elsewhere"},
        root=skill_root,
        env={},
    )
    assert "BEGIN SKILL: coding" in context(result)
    assert not (skill_root / ".oslab" / "cyntox" / "skill-selections" / "state").exists()


def test_automatic_failure_warns_without_leaking_exception(skill_root: Path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise OSError("private prompt body should not escape")

    monkeypatch.setattr(hook, "select_skills", fail)
    result = hook.evaluate_user_prompt_submit({"prompt": "debug code"}, root=skill_root, env={})
    assert "decision" not in result
    assert "automatic skills unavailable" in result["systemMessage"]
    assert "private prompt body" not in json.dumps(result)


@pytest.mark.parametrize(
    "payload",
    [
        '{"prompt":"Debug Python code","cwd":"C:/untrusted"}',
        "[]",
        "{",
        "x" * (hook.MAX_INPUT_CHARS + 1),
    ],
    ids=["valid", "invalid-type", "invalid-json", "oversized"],
)
def test_real_hook_stdin_stdout_protocol(skill_root: Path, payload: str) -> None:
    scripts_dir = skill_root / "scripts"
    scripts_dir.mkdir()
    installed = scripts_dir / "cyntox_skill_hook.py"
    shutil.copyfile(ROOT / "scripts" / "cyntox_skill_hook.py", installed)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "CYNTOX_AUTO_SKILLS": "1",
        "CYNTOX_USE_SKILLS": "[]",
    }
    completed = subprocess.run(  # noqa: S603 - fixed Python and copied local hook
        [sys.executable, str(installed)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        cwd=skill_root,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not completed.stderr
    result = json.loads(completed.stdout)
    text = context(result)
    assert len(text) <= hook.MAX_CONTEXT_CHARS
    if payload.startswith('{"prompt"'):
        assert "BEGIN SKILL: coding" in text
    else:
        assert "invalid_hook_input" in text
