from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None, reason="Requires Windows PowerShell"
)

# A real native process exercises LASTEXITCODE, including stages which PowerShell
# would otherwise continue past despite ErrorActionPreference = Stop.
FAKE_NATIVE = r"""
using System;
using System.IO;
using System.Reflection;
public class BootstrapFake {
    public static int Main(string[] args) {
        string executable = Assembly.GetExecutingAssembly().Location;
        string name = Path.GetFileName(executable);
        string stage;
        if (name == "pnpm.exe") {
            if (Environment.GetEnvironmentVariable("CI") != "true") return 23;
            stage = Array.IndexOf(args, "--offline") >= 0 ? "pnpm-offline" : "pnpm-online";
        } else if (name == "uv.exe") {
            stage = args[0] == "sync" ? "sync" : "init";
        } else if (args[0] == "-c") {
            return Environment.GetEnvironmentVariable("CYNTOX_TEST_BAD_PYTHON") == "1" ? 1 : 0;
        } else {
            stage = args[1] == "venv" ? "venv" : "pip";
        }
        File.AppendAllText(Environment.GetEnvironmentVariable("CYNTOX_TEST_LOG"), stage + "\n");
        if (Environment.GetEnvironmentVariable("CYNTOX_TEST_FAIL") == stage) return 17;
        if (stage == "pnpm-offline" && Environment.GetEnvironmentVariable("CYNTOX_TEST_OFFLINE_MISS") == "1") return 2;
        if (stage == "venv") {
            string scripts = Path.Combine(args[2], "Scripts");
            Directory.CreateDirectory(scripts);
            File.Copy(executable, Path.Combine(scripts, "python.exe"));
        }
        if (stage == "pip") File.Copy(executable, Path.Combine(Path.GetDirectoryName(executable), "uv.exe"), true);
        if (stage == "init") {
            if ((Environment.GetEnvironmentVariable("CI") ?? "") !=
                (Environment.GetEnvironmentVariable("CYNTOX_TEST_ORIGINAL_CI") ?? "")) return 24;
            Console.WriteLine("CYNTOX_TEST_SETUP_COMPLETE");
        }
        return 0;
    }
}
"""


def ps_literal(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture(scope="module")
def fake_native(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("bootstrap native")
    source = directory / "fake.cs"
    executable = directory / "python.exe"
    source.write_text(FAKE_NATIVE, encoding="utf-8")
    assert POWERSHELL is not None
    compiled = subprocess.run(  # noqa: S603 - fixed local compiler, isolated fixture output
        [
            POWERSHELL,
            "-NoProfile",
            "-Command",
            f"Add-Type -Path {ps_literal(source)} -OutputAssembly {ps_literal(executable)} "
            "-OutputType ConsoleApplication -ErrorAction Stop",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    return executable


def run_bootstrap(
    tmp_path: Path,
    fake_native: Path,
    *,
    fail: str = "",
    offline_miss: bool = False,
    bad_python: bool = False,
    original_ci: str | None = "original-user-value",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts" / "bootstrap.ps1", scripts / "bootstrap.ps1")
    (tmp_path / "package.json").write_text('{"packageManager":"pnpm@10.15.1"}', encoding="utf-8")
    pnpm = tmp_path / "pnpm.exe"
    shutil.copy2(fake_native, pnpm)
    log = tmp_path / "steps.txt"
    environment = {
        **os.environ,
        "CYNTOX_TEST_LOG": str(log),
        "CYNTOX_TEST_FAIL": fail,
        "CYNTOX_TEST_OFFLINE_MISS": "1" if offline_miss else "0",
        "CYNTOX_TEST_BAD_PYTHON": "1" if bad_python else "0",
        "CYNTOX_TEST_ORIGINAL_CI": original_ci or "",
    }
    if original_ci is None:
        environment.pop("CI", None)
    else:
        environment["CI"] = original_ci
    assert POWERSHELL is not None
    result = subprocess.run(  # noqa: S603 - checked-in script and local fake executables
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts / "bootstrap.ps1"),
            "-PythonPath",
            str(fake_native),
            "-PnpmPath",
            str(pnpm),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
        check=False,
    )
    return result, log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.mark.parametrize(
    ("fail", "expected"),
    [
        ("venv", ["venv"]),
        ("pip", ["venv", "pip"]),
        ("sync", ["venv", "pip", "sync"]),
        ("pnpm-online", ["venv", "pip", "sync", "pnpm-offline", "pnpm-online"]),
        ("init", ["venv", "pip", "sync", "pnpm-offline", "pnpm-online", "init"]),
    ],
)
def test_bootstrap_stops_after_native_failure(
    tmp_path: Path, fake_native: Path, fail: str, expected: list[str]
) -> None:
    result, steps = run_bootstrap(tmp_path, fake_native, fail=fail, offline_miss=True)
    assert result.returncode != 0
    assert "exit code 17" in result.stderr
    assert steps == expected
    assert "CYNTOX_TEST_SETUP_COMPLETE" not in result.stdout


@pytest.mark.parametrize("offline_miss", [False, True])
def test_bootstrap_completes_with_explicit_tools_and_offline_retry(
    tmp_path: Path, fake_native: Path, offline_miss: bool
) -> None:
    result, steps = run_bootstrap(tmp_path, fake_native, offline_miss=offline_miss)
    assert result.returncode == 0, result.stderr
    assert steps == ["venv", "pip", "sync", "pnpm-offline"] + (
        ["pnpm-online"] if offline_miss else []
    ) + ["init"]
    assert "CYNTOX_TEST_SETUP_COMPLETE" in result.stdout


def test_bootstrap_rejects_unsupported_explicit_python(
    tmp_path: Path,
    fake_native: Path,
) -> None:
    result, steps = run_bootstrap(tmp_path, fake_native, bad_python=True)
    assert result.returncode != 0
    assert "Python 3.12, 3.13, or 3.14" in result.stderr
    assert steps == []


@pytest.mark.parametrize("original_ci", [None, "user-ci-setting"])
def test_bootstrap_runs_pnpm_without_prompts_and_restores_ci_environment(
    tmp_path: Path,
    fake_native: Path,
    original_ci: str | None,
) -> None:
    # The fake pnpm exits 23 without CI=true; fake init exits 24 if CI leaked.
    result, steps = run_bootstrap(tmp_path, fake_native, offline_miss=True, original_ci=original_ci)
    assert result.returncode == 0, result.stderr
    assert steps[-3:] == ["pnpm-offline", "pnpm-online", "init"]


def test_bootstrap_python_version_boundaries_match_project_requirement() -> None:
    assert POWERSHELL is not None
    script = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {ps_literal(ROOT / "scripts" / "bootstrap.ps1")}, [ref]$tokens, [ref]$errors
)
if ($errors.Count) {{ throw 'Bootstrap has a PowerShell syntax error.' }}
$function = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Test-BootstrapPython'
}}, $true)
Invoke-Expression $function.Extent.Text
function Invoke-VersionedPython {{
    & {ps_literal(Path(sys.executable))} -c ("import sys; sys.version_info=(3, $minor); " + $args[1])
}}
$results = @()
foreach ($minor in @(11, 12, 13, 14, 15)) {{
    $results += Test-BootstrapPython -Executable Invoke-VersionedPython
}}
ConvertTo-Json -InputObject $results -Compress
"""
    result = subprocess.run(  # noqa: S603 - checked-in function and local Python interpreter
        [POWERSHELL, "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [False, True, True, True, False]
