from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from oslab.skill_routing import select_skills
from scripts import cyntox_cli, cyntox_council


def install_skill(root: Path, name: str, description: str) -> Path:
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n"
        f"Use this skill for {description}\nVerify the result before claiming completion.\n",
        encoding="utf-8",
    )
    return path


def test_jobs_select_only_installed_skills_without_registry_writes(tmp_path: Path) -> None:
    install_skill(tmp_path, "coding", "Python debugging and unit tests.")
    _, _, job = cyntox_cli.create_job(
        tmp_path, "Debug the Python function and Jellyfin", dry_run=True
    )
    assert job["skills"] == ["coding"]
    assert job["skill_selection"]["mode"] == "automatic"
    assert job["skill_selection"]["names"] == job["skills"]
    assert job["skill_selection"]["selected"][0]["reason"]
    assert len(job["skill_selection"]["selected"][0]["sha256"]) == 64
    assert not (tmp_path / "skills" / ".registry.json").exists()


def test_jobs_manual_selection_overrides_automatic_and_empty_disables(tmp_path: Path) -> None:
    install_skill(tmp_path, "coding", "Python debugging.")
    install_skill(tmp_path, "research-notes", "Research summaries.")
    _, _, manual = cyntox_cli.create_job(
        tmp_path, "Debug Python", skills=["research-notes"], dry_run=True
    )
    assert manual["skills"] == ["research-notes"]
    assert manual["skill_selection"]["mode"] == "manual"
    _, _, disabled = cyntox_cli.create_job(tmp_path, "Debug Python", auto_skills=False)
    assert disabled["skills"] == []
    assert disabled["skill_selection"]["mode"] == "disabled"
    _, _, empty = cyntox_cli.create_job(tmp_path, "Debug Python", skills=[])
    assert empty["skills"] == []


def test_invalid_manual_skill_fails_before_creating_job(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        cyntox_cli.create_job(tmp_path, "Debug Python", skills=["missing"])
    assert not (tmp_path / cyntox_cli.DEFAULT_JOBS_DIR).exists()


def test_enqueue_opt_out_needs_no_other_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_skill(tmp_path, "coding", "Python debugging.")
    monkeypatch.setattr(cyntox_cli, "start_worker", lambda *args, **kwargs: 1234)
    assert cyntox_cli.enqueue_task(tmp_path, ["Debug Python", "--no-auto-skills"]) == 0
    job = cyntox_cli.list_jobs(tmp_path)[0]
    assert job["skills"] == []
    assert job["skill_selection"]["mode"] == "disabled"


@pytest.mark.parametrize("foreground", [False, True])
def test_enqueue_announces_skills_before_background_or_foreground_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    foreground: bool,
) -> None:
    install_skill(tmp_path, "coding", "Python debugging.")
    notices_at_execution: list[str] = []

    def begin_execution(*args: Any, **kwargs: Any) -> int:
        notices_at_execution.append(capsys.readouterr().out)
        return 0 if foreground else 1234

    monkeypatch.setattr(cyntox_cli, "start_worker", begin_execution)
    monkeypatch.setattr(cyntox_cli, "run_job", begin_execution)
    monkeypatch.setattr(cyntox_cli, "print_foreground_result", lambda *args: print("Job complete."))
    options = ["--foreground"] if foreground else []
    assert cyntox_cli.enqueue_task(tmp_path, ["Debug Python", *options]) == 0
    assert notices_at_execution == ["Skills (automatic): coding\n"]
    status = capsys.readouterr().out
    assert ("Job complete." if foreground else "Queued cyntox job") in status


@pytest.mark.parametrize("mode", ["automatic", "manual", "disabled"])
def test_retry_and_resume_preserve_selection_mode_and_exact_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    install_skill(tmp_path, "coding", "Python debugging.")
    options: dict[str, Any] = {"dry_run": True}
    if mode == "manual":
        options["skills"] = ["coding"]
    if mode == "disabled":
        options["auto_skills"] = False
    job_id, job_dir, job = cyntox_cli.create_job(tmp_path, "Debug Python", **options)
    original_selection = job["skill_selection"]
    install_skill(tmp_path, "python-specialist", "Python debugging.")
    monkeypatch.setattr(cyntox_cli, "start_worker", lambda *args, **kwargs: 1234)
    assert cyntox_cli.cmd_jobs(tmp_path, ["retry", job_id]) == 0
    retried = next(
        item for item in cyntox_cli.list_jobs(tmp_path) if item.get("retry_of") == job_id
    )
    assert retried["skill_selection"] == original_selection
    assert retried["skills"] == job["skills"]
    cyntox_cli.set_job_state(job_dir, job, "failed")
    assert cyntox_cli.cmd_jobs(tmp_path, ["resume", job_id]) == 0
    assert cyntox_cli.load_job(tmp_path, job_id)[1]["skill_selection"] == original_selection


def test_legacy_empty_job_does_not_gain_skills_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_skill(tmp_path, "coding", "Python debugging.")
    job_id, job_dir, job = cyntox_cli.create_job(tmp_path, "Debug Python", skills=[])
    del job["skill_selection"]
    cyntox_cli.save_job(job_dir, job)
    monkeypatch.setattr(cyntox_cli, "start_worker", lambda *args, **kwargs: 1234)
    assert cyntox_cli.cmd_jobs(tmp_path, ["retry", job_id]) == 0
    retried = next(
        item for item in cyntox_cli.list_jobs(tmp_path) if item.get("retry_of") == job_id
    )
    assert retried["skills"] == []


@pytest.mark.parametrize(
    ("options", "expected", "mode"),
    [
        ([], ["coding"], "automatic"),
        (["--use-skill", "research-notes"], ["research-notes"], "manual"),
        (["--no-auto-skills"], [], "disabled"),
    ],
)
def test_direct_council_selects_and_records_actual_skills_without_dry_run_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
    expected: list[str],
    mode: str,
) -> None:
    install_skill(tmp_path, "coding", "Python debugging.")
    install_skill(tmp_path, "research-notes", "Research summaries.")
    monkeypatch.setattr(cyntox_council, "project_root", lambda: tmp_path)
    assert (
        cyntox_council.main(
            [
                "--roles",
                "architect",
                "--dry-run",
                "--no-memory",
                "--run-id",
                "auto-test",
                *options,
                "Debug Python",
            ]
        )
        == 0
    )
    run_dir = tmp_path / ".oslab" / "council" / "runs" / "auto-test"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["active_skills"] == expected
    assert manifest["skill_selection"]["mode"] == mode
    prompt = (run_dir / "01-architect.prompt.md").read_text(encoding="utf-8")
    for name in ["coding", "research-notes"]:
        assert (f"--- BEGIN SKILL: {name} ---" in prompt) == (name in expected)
    assert f"Skills ({mode}): {', '.join(expected) or 'none'}" in capsys.readouterr().out
    assert not (tmp_path / "skills" / ".registry.json").exists()


def test_worker_preserves_automatic_provenance_and_copies_actual_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_skill(tmp_path, "coding", "Python debugging.")
    job_id, _, created = cyntox_cli.create_job(
        tmp_path, "Debug Python", dry_run=True, save_memory=False
    )
    monkeypatch.setattr(cyntox_council, "project_root", lambda: tmp_path)

    def run_council(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert "--use-skill" not in argv
        saved = json.loads(argv[argv.index("--skill-selection-json") + 1])
        assert saved == created["skill_selection"]
        code = cyntox_council.main([*argv[2:-1], "--no-memory", argv[-1]])
        return subprocess.CompletedProcess(argv, code, "", "")

    monkeypatch.setattr(subprocess, "run", run_council)
    assert cyntox_cli.run_job(tmp_path, job_id) == 0
    _, completed = cyntox_cli.load_job(tmp_path, job_id)
    assert completed["skill_selection"] == created["skill_selection"]
    assert completed["skills"] == ["coding"]
    assert not (tmp_path / "skills" / ".registry.json").exists()


def test_changed_skill_cannot_silently_run_different_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = install_skill(tmp_path, "coding", "Python debugging.")
    job_id, _, _ = cyntox_cli.create_job(tmp_path, "Debug Python", dry_run=True)
    path.write_text(path.read_text(encoding="utf-8") + "Changed instructions.\n", encoding="utf-8")
    calls: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    assert cyntox_cli.run_job(tmp_path, job_id) == 1
    assert calls == []
    assert cyntox_cli.load_job(tmp_path, job_id)[1]["state"] == "failed"


def test_council_does_not_load_archived_symlink(tmp_path: Path) -> None:
    archived = tmp_path / "skills" / "_archive" / "coding"
    archived.mkdir(parents=True)
    (archived / "SKILL.md").write_text("Forbidden archived content.", encoding="utf-8")
    try:
        (tmp_path / "skills" / "coding").symlink_to(archived, target_is_directory=True)
    except OSError:
        pytest.skip("Creating Windows symlinks requires an enabled developer mode.")
    assert "coding" not in cyntox_council.discover_skill_names(tmp_path, "skills")
    assert "Forbidden" not in cyntox_council.load_repo_skills(
        tmp_path, "skills", active_names=["coding"]
    )


def test_disabled_skill_context_reads_no_catalogue_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_skill(tmp_path, "coding", "Python debugging.")
    selection = select_skills(tmp_path, "Debug Python", enabled=False)

    def unexpected_read(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("Disabled skill context must not read any skill document")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    assert cyntox_council.load_repo_skills(tmp_path, "skills", selection=selection) == (
        "No repo-local skills selected for this task."
    )


def test_council_usage_and_scoring_reference_only_loaded_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_skill(tmp_path, "coding", "Python debugging.")
    install_skill(tmp_path, "research-notes", "Research summaries.")
    monkeypatch.setattr(cyntox_council, "project_root", lambda: tmp_path)
    monkeypatch.setattr(cyntox_council, "gpu_free_vram_mib", lambda: None)
    prompts: list[str] = []
    scored_names: list[list[str]] = []

    def fake_generation(
        root: Path, prompt: str, max_wall_time: str, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        prompts.append(prompt)
        return subprocess.CompletedProcess([], 0, "Local fake result.", "")

    def fake_score(root: Path, directory: str, names: list[str], score: float | None) -> None:
        scored_names.append(names)

    monkeypatch.setattr(cyntox_council, "run_role_with_generation_lease", fake_generation)
    monkeypatch.setattr(cyntox_council, "record_skill_score", fake_score)
    assert (
        cyntox_council.main(
            ["--roles", "architect", "--no-memory", "--run-id", "usage-test", "Debug Python"]
        )
        == 0
    )
    registry = cyntox_council.load_registry(tmp_path, "skills")["skills"]
    assert registry["coding"]["use_count"] == 1
    assert registry["research-notes"]["use_count"] == 0
    assert scored_names == [["coding"]]
    assert len(prompts) == 1
    assert "BEGIN SKILL: coding" in prompts[0]
    assert "BEGIN SKILL: research-notes" not in prompts[0]
