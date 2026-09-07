from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install.ps1"
POWERSHELL = shutil.which("powershell.exe")
GIT = shutil.which("git.exe") or shutil.which("git")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None, reason="installer requires Windows PowerShell"
)


def ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def installer_functions(expression: str, *, isolate_user_path: bool = False) -> Any:
    """Load functions without running setup; never touch the real persistent PATH."""
    assert POWERSHELL is not None
    facade = (
        r"""
Add-Type -TypeDefinition @'
public static class CyntOXTestEnvironment {
    public static string UserPath = @"C:\existing-bin";
    public static int Writes = 0;
    public static void ClearUserPath() { UserPath = null; }
    public static string GetEnvironmentVariable(string name, string target) {
        if (name != "Path" || target != "User") throw new System.Exception("unexpected environment read");
        return UserPath;
    }
    public static void SetEnvironmentVariable(string name, string value, string target) {
        if (name != "Path" || target != "User") throw new System.Exception("unexpected environment write");
        UserPath = value;
        Writes++;
    }
}
'@
"""
        if isolate_user_path
        else ""
    )
    replacement = (
        "$source = $source.Replace('[Environment]', '[CyntOXTestEnvironment]')"
        if isolate_user_path
        else ""
    )
    command = rf"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
# Python can inherit PowerShell 7's PSModulePath; explicitly load the 5.1 module under test.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1')
{facade}
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {ps_literal(INSTALLER)}, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) {{ throw (($parseErrors | ForEach-Object Message) -join '; ') }}
$functions = $ast.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}}, $true)
foreach ($function in $functions) {{
    $source = $function.Extent.Text
    {replacement}
    Invoke-Expression $source
}}
{expression}
"""
    completed = subprocess.run(  # noqa: S603 - local Windows PowerShell, isolated test data
        [POWERSHELL, "-NoProfile", "-Command", command],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def test_installer_parses_in_windows_powershell_without_running_setup() -> None:
    result = installer_functions(
        "@{version = $PSVersionTable.PSVersion.Major; functions = @($functions.Name)} | ConvertTo-Json -Compress"
    )
    assert result["version"] == 5
    assert "Install-CyntOX" in result["functions"]
    assert "Register-CyntOXCommand" in result["functions"]


def test_native_failure_stops_setup_with_exit_status() -> None:
    result = installer_functions(
        r"""
$message = $null
try {
    Invoke-CyntOXInstallCommand $env:ComSpec @('/d', '/c', 'exit 23')
} catch { $message = $_.Exception.Message }
@{ message = $message } | ConvertTo-Json -Compress
"""
    )
    assert "exit 23" in result["message"]
    assert "Setup stopped" in result["message"]


def test_node_version_probe_accepts_supported_node_under_powershell_5() -> None:
    node = shutil.which("node.exe")
    if node is None:
        pytest.skip("Node.js is unavailable")
    version = subprocess.run(  # noqa: S603 - read-only local Node version check
        [node, "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if int(version.lstrip("v").split(".")[0]) < 22:
        pytest.skip("Node.js 22+ is unavailable")
    result = installer_functions(
        rf"""
$nodeProbe = $ast.FindAll({{
    param($statement)
    $statement -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $statement.Left.Extent.Text -eq '$nodeArgs'
}}, $true)
if ($nodeProbe.Count -ne 1) {{ throw 'Expected one Node version probe in installer' }}
Invoke-Expression $nodeProbe[0].Extent.Text
@{{ node = Find-CyntOXInstallTool 'cyntox-test-node.exe' -Candidates @({ps_literal(node)}) -VersionArguments $nodeArgs }} | ConvertTo-Json -Compress
"""
    )
    assert result["node"] == node


@pytest.mark.parametrize("checkout_state", ["unrelated", "wrong-origin", "wrong-branch", "dirty"])
def test_checkout_refusals_never_pull_or_overwrite(tmp_path: Path, checkout_state: str) -> None:
    if GIT is None:
        pytest.skip("Git is unavailable")
    destination = tmp_path / "existing checkout"
    destination.mkdir()
    sentinel = destination / "precious.txt"
    sentinel.write_text("keep this unchanged", encoding="utf-8")
    if checkout_state != "unrelated":
        branch = "work-in-progress" if checkout_state == "wrong-branch" else "main"
        for arguments in (
            ["init", "--initial-branch", branch],
            [
                "remote",
                "add",
                "origin",
                "https://example.invalid/another.git"
                if checkout_state == "wrong-origin"
                else "https://github.com/undflkflslfsflm/CyntOX.git",
            ],
        ):
            subprocess.run(  # noqa: S603 - Git writes only this isolated temporary checkout
                [GIT, "-C", str(destination), *arguments],
                check=True,
                capture_output=True,
            )
    result = installer_functions(
        rf"""
function Update-CyntOXInstallPath {{ }}
function Find-CyntOXInstallTool {{ return {ps_literal(GIT)} }}
$script:mutationCalls = 0
function Invoke-CyntOXInstallCommand {{ $script:mutationCalls++; throw 'unexpected mutation' }}
$message = $null
try {{ $null = Get-CyntOXCheckout -Destination {ps_literal(destination)} }}
catch {{ $message = $_.Exception.Message }}
@{{ message = $message; mutationCalls = $script:mutationCalls }} | ConvertTo-Json -Compress
"""
    )
    expected = {
        "unrelated": "not a Git checkout",
        "wrong-origin": "Unexpected Git origin",
        "wrong-branch": "main branch",
        "dirty": "Local changes found",
    }
    assert expected[checkout_state] in result["message"]
    assert result["mutationCalls"] == 0
    assert sentinel.read_text(encoding="utf-8") == "keep this unchanged"


@pytest.mark.parametrize("initial_user_path", [None, "", r"C:\existing-bin"])
def test_global_command_registration_is_idempotent_and_preserves_launch_arguments(
    tmp_path: Path,
    initial_user_path: str | None,
) -> None:
    root = tmp_path / "checkout with spaces (test)"
    root.mkdir()
    (root / "cyntox.ps1").write_text(
        "@{ forwarded = @($args); path = $env:PATH } | ConvertTo-Json -Compress\nexit 29\n",
        encoding="utf-8-sig",
    )
    bin_dir = tmp_path / "global bin"
    node_dir = tmp_path / "tool node"
    initialize_path = (
        "[CyntOXTestEnvironment]::ClearUserPath()"
        if initial_user_path is None
        else f"[CyntOXTestEnvironment]::UserPath = {ps_literal(initial_user_path)}"
    )
    result = installer_functions(
        rf"""
{initialize_path}
$initialPathWasNull = $null -eq [CyntOXTestEnvironment]::UserPath
Register-CyntOXCommand -Root {ps_literal(root)} -ToolDirectories @({ps_literal(node_dir)}) -BinDir {ps_literal(bin_dir)}
Register-CyntOXCommand -Root {ps_literal(root)} -ToolDirectories @({ps_literal(node_dir)}) -BinDir {ps_literal(bin_dir)}
$command = Get-Command cyntox -CommandType Application
$output = @(& $command.Source run '--prompt' 'a file with spaces.py')
$code = $LASTEXITCODE
@{{
    command = $command.Source
    code = $code
    output = ($output -join "`n") | ConvertFrom-Json
    userPath = [CyntOXTestEnvironment]::UserPath
    pathWrites = [CyntOXTestEnvironment]::Writes
    initialPathWasNull = $initialPathWasNull
}} | ConvertTo-Json -Depth 5 -Compress
""",
        isolate_user_path=True,
    )
    assert result["command"] == str(bin_dir / "cyntox.cmd")
    assert result["code"] == 29
    assert result["output"]["forwarded"] == ["run", "--prompt", "a file with spaces.py"]
    assert result["output"]["path"].split(";")[0] == str(node_dir)
    expected_path = [str(bin_dir)] + ([initial_user_path] if initial_user_path else [])
    assert result["userPath"].split(";") == expected_path
    assert result["pathWrites"] == 1
    assert result["initialPathWasNull"] is (initial_user_path is None)
    config = json.loads((bin_dir / "install.json").read_text(encoding="utf-8-sig"))
    assert config["root"] == str(root)
    assert config["tool_directories"] == [str(node_dir)]


def test_register_only_requires_an_existing_source_path() -> None:
    result = installer_functions(
        """
$message = $null
try { Install-CyntOX -Destination 'C:\\unused' -OnlyRegister }
catch { $message = $_.Exception.Message }
@{ message = $message } | ConvertTo-Json -Compress
"""
    )
    assert "RegisterOnly requires SourcePath" in result["message"]


def model_fixture(tmp_path: Path) -> tuple[Path, Path, Path, bytes]:
    root = tmp_path / "checkout"
    config = root / "config"
    config.mkdir(parents=True)
    data = b"GGUF synthetic model for installer hash tests"
    filename = "locked-model.gguf"
    lock = {
        "repository": "fixture/installer-model",
        "revision": "a" * 40,
        "filename": filename,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    (config / "install-model.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    local_app_data = tmp_path / "isolated app data"
    model_path = local_app_data / "CyntOX" / "models" / filename
    model_path.parent.mkdir(parents=True)
    return root, local_app_data, model_path, data


def test_verified_model_cache_never_downloads(tmp_path: Path) -> None:
    root, app_data, model_path, data = model_fixture(tmp_path)
    model_path.write_bytes(data)
    result = installer_functions(
        rf"""
$env:LOCALAPPDATA = {ps_literal(app_data)}
function Invoke-CyntOXInstallCommand {{ throw 'Unexpected network or native command' }}
@{{ path = Get-CyntOXInstallModelFile {ps_literal(root)} }} | ConvertTo-Json -Compress
"""
    )
    assert result == {"path": str(model_path)}
    assert model_path.read_bytes() == data


@pytest.mark.parametrize("corruption", ["truncated", "checksum"])
def test_corrupt_cached_model_stops_without_overwriting(tmp_path: Path, corruption: str) -> None:
    root, app_data, model_path, data = model_fixture(tmp_path)
    damaged = data[:-1] if corruption == "truncated" else b"x" * len(data)
    model_path.write_bytes(damaged)
    result = installer_functions(
        rf"""
$env:LOCALAPPDATA = {ps_literal(app_data)}
function Invoke-CyntOXInstallCommand {{ throw 'Unexpected network or native command' }}
$message = $null
try {{ $null = Get-CyntOXInstallModelFile {ps_literal(root)} }}
catch {{ $message = $_.Exception.Message }}
@{{ message = $message }} | ConvertTo-Json -Compress
"""
    )
    assert "Cached model failed verification" in result["message"]
    assert model_path.read_bytes() == damaged


@pytest.mark.parametrize("valid", [True, False])
def test_complete_partial_download_is_verified_before_promotion(
    tmp_path: Path, valid: bool
) -> None:
    root, app_data, model_path, data = model_fixture(tmp_path)
    partial = model_path.with_name(model_path.name + ".part")
    downloaded = data if valid else b"x" * len(data)
    partial.write_bytes(downloaded)
    result = installer_functions(
        rf"""
$env:LOCALAPPDATA = {ps_literal(app_data)}
function Write-Host {{ }}
function New-Object {{ param($TypeName, $ArgumentList) return @{{ AvailableFreeSpace = 60GB }} }}
function Invoke-CyntOXInstallCommand {{ throw 'Unexpected download: partial is already complete' }}
$message = $null
$path = $null
try {{ $path = Get-CyntOXInstallModelFile {ps_literal(root)} }}
catch {{ $message = $_.Exception.Message }}
@{{ message = $message; path = $path }} | ConvertTo-Json -Compress
"""
    )
    assert not partial.exists()
    if valid:
        assert result["message"] is None
        assert result["path"] == str(model_path)
        assert model_path.read_bytes() == data
    else:
        assert "Model verification failed" in result["message"]
        assert not model_path.exists()
        quarantined = list(model_path.parent.glob(model_path.name + ".part.invalid-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == downloaded


def test_partial_download_resumes_using_exact_locked_https_url(tmp_path: Path) -> None:
    root, app_data, model_path, data = model_fixture(tmp_path)
    partial = model_path.with_name(model_path.name + ".part")
    partial.write_bytes(data[:4])
    source = tmp_path / "synthetic source.gguf"
    source.write_bytes(data)
    result = installer_functions(
        rf"""
$env:LOCALAPPDATA = {ps_literal(app_data)}
function Write-Host {{ }}
function New-Object {{ param($TypeName, $ArgumentList) return @{{ AvailableFreeSpace = 60GB }} }}
function Get-Command {{ param($Name, $CommandType, $ErrorAction) return @{{ Source = 'fake-curl.exe' }} }}
$script:downloadCalls = @()
function Invoke-CyntOXInstallCommand {{
    param($Executable, [string[]]$Arguments)
    $script:downloadCalls += ,@($Executable, $Arguments)
    $outputIndex = [array]::IndexOf($Arguments, '--output')
    Copy-Item -LiteralPath {ps_literal(source)} -Destination $Arguments[$outputIndex + 1]
}}
$path = Get-CyntOXInstallModelFile {ps_literal(root)}
@{{ path = $path; downloads = $script:downloadCalls }} | ConvertTo-Json -Depth 5 -Compress
"""
    )
    assert result["path"] == str(model_path)
    assert model_path.read_bytes() == data
    assert not partial.exists()
    assert len(result["downloads"]) == 1
    executable, arguments = result["downloads"][0]
    assert executable == "fake-curl.exe"
    assert arguments[arguments.index("--continue-at") + 1] == "-"
    assert arguments[arguments.index("--output") + 1] == str(partial)
    assert arguments[arguments.index("--proto") + 1] == "=https"
    assert arguments[arguments.index("--proto-redir") + 1] == "=https"
    assert arguments[-1] == (
        "https://huggingface.co/fixture/installer-model/resolve/" + "a" * 40 + "/locked-model.gguf"
    )


def test_model_download_refuses_insufficient_disk_space(tmp_path: Path) -> None:
    root, app_data, model_path, _ = model_fixture(tmp_path)
    result = installer_functions(
        rf"""
$env:LOCALAPPDATA = {ps_literal(app_data)}
function New-Object {{ param($TypeName, $ArgumentList) return @{{ AvailableFreeSpace = 1GB }} }}
function Invoke-CyntOXInstallCommand {{ throw 'Unexpected download with insufficient disk space' }}
$message = $null
try {{ $null = Get-CyntOXInstallModelFile {ps_literal(root)} }}
catch {{ $message = $_.Exception.Message }}
@{{ message = $message }} | ConvertTo-Json -Compress
"""
    )
    assert "requires 50 GiB free" in result["message"]
    assert not model_path.exists()


def test_existing_cyntox_model_is_preserved_without_downloading(tmp_path: Path) -> None:
    result = installer_functions(
        rf"""
function Write-Host {{ }}
function Start-CyntOXInstallOllama {{ return @{{ models = @(@{{ name = 'cyntox:latest' }}) }} }}
function Get-CyntOXInstallModelFile {{ throw 'Unexpected download for existing model' }}
function Invoke-CyntOXInstallCommand {{ throw 'Unexpected model replacement' }}
Initialize-CyntOXInstallModel -Ollama 'fake-ollama.exe' -Root {ps_literal(tmp_path)}
@{{ preserved = $true }} | ConvertTo-Json -Compress
"""
    )
    assert result == {"preserved": True}


@pytest.mark.parametrize("fail_import", [False, True])
def test_model_import_uses_verified_file_and_restores_ollama_host(
    tmp_path: Path, fail_import: bool
) -> None:
    root, _, model_path, data = model_fixture(tmp_path)
    model_path.write_bytes(data)
    template = 'FROM old/source\nPARAMETER temperature 0.2\nSYSTEM "Local safety boundary"\n'
    (root / "config" / "Modelfile.cyntox").write_text(template, encoding="utf-8")
    result = installer_functions(
        rf"""
function Write-Host {{ }}
function Start-CyntOXInstallOllama {{ return @{{ models = @() }} }}
function Get-CyntOXInstallModelFile {{ return {ps_literal(model_path)} }}
$env:OLLAMA_HOST = 'saved-setting'
$script:creation = $null
function Invoke-CyntOXInstallCommand {{
    param($Executable, [string[]]$Arguments)
    $script:creation = @{{ executable = $Executable; arguments = $Arguments; host = $env:OLLAMA_HOST }}
    if (${str(fail_import).lower()}) {{ throw 'simulated import failure' }}
}}
$message = $null
try {{ Initialize-CyntOXInstallModel -Ollama 'fake-ollama.exe' -Root {ps_literal(root)} }}
catch {{ $message = $_.Exception.Message }}
@{{ message = $message; host = $env:OLLAMA_HOST; creation = $script:creation }} | ConvertTo-Json -Depth 5 -Compress
"""
    )
    assert result["host"] == "saved-setting"
    assert result["creation"]["host"] == "127.0.0.1:11434"
    modelfile = root / ".oslab" / "install" / "Modelfile"
    assert result["creation"]["executable"] == "fake-ollama.exe"
    assert result["creation"]["arguments"] == ["create", "cyntox", "-f", str(modelfile)]
    assert modelfile.read_text(encoding="utf-8") == template.replace(
        "FROM old/source", f'FROM "{model_path.as_posix()}"'
    )
    assert result["message"] == ("simulated import failure" if fail_import else None)


@pytest.mark.parametrize(
    ("done", "response", "ready"),
    [(True, "READY", True), (False, "READY", False), (True, "", False), (True, "  ", False)],
)
def test_model_readiness_requires_a_complete_nonempty_answer(
    done: bool, response: str, ready: bool
) -> None:
    result = installer_functions(
        rf"""
function Write-Host {{ }}
$script:request = $null
function Invoke-RestMethod {{
    param($Uri, $Method, $ContentType, $Body, $TimeoutSec)
    $script:request = @{{ uri = $Uri; method = $Method; body = ($Body | ConvertFrom-Json) }}
    return @{{ done = ${str(done).lower()}; response = {ps_literal(response)} }}
}}
$message = $null
try {{ Test-CyntOXInstallModel }} catch {{ $message = $_.Exception.Message }}
@{{ message = $message; request = $script:request }} | ConvertTo-Json -Depth 6 -Compress
"""
    )
    if ready:
        assert result["message"] is None
    else:
        assert "did not return a complete answer" in result["message"]
    assert result["request"]["uri"] == "http://127.0.0.1:11434/api/generate"
    assert result["request"]["method"] == "Post"
    assert result["request"]["body"]["model"] == "cyntox:latest"
    assert result["request"]["body"]["stream"] is False
    assert result["request"]["body"]["think"] is False


@pytest.mark.parametrize("fail_smoke", [False, True])
def test_model_smoke_uses_gpu_lease_and_restores_timeout(tmp_path: Path, fail_smoke: bool) -> None:
    result = installer_functions(
        rf"""
$env:CYNTOX_GPU_LEASE_TIMEOUT_SECONDS = '47'
$script:smokeCommand = $null
function Invoke-CyntOXInstallCommand {{
    param($Executable, [string[]]$Arguments)
    $script:smokeCommand = @{{
        executable = $Executable; arguments = $Arguments
        timeout = $env:CYNTOX_GPU_LEASE_TIMEOUT_SECONDS
    }}
    if (${str(fail_smoke).lower()}) {{ throw 'simulated smoke failure' }}
}}
$message = $null
try {{ Invoke-CyntOXInstallModelSmoke -Root {ps_literal(tmp_path)} }}
catch {{ $message = $_.Exception.Message }}
$smokePath = Join-Path {ps_literal(tmp_path)} '.oslab\install\smoke.ps1'
$smokeTokens = $null
$smokeErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile(
    $smokePath, [ref]$smokeTokens, [ref]$smokeErrors)
@{{
    message = $message; command = $script:smokeCommand
    timeout = $env:CYNTOX_GPU_LEASE_TIMEOUT_SECONDS
    parseErrors = $smokeErrors.Count
}} | ConvertTo-Json -Depth 5 -Compress
"""
    )
    assert result["message"] == ("simulated smoke failure" if fail_smoke else None)
    assert result["timeout"] == "47"
    assert result["command"]["timeout"] == "300"
    assert result["command"]["executable"] == str(tmp_path / ".venv" / "Scripts" / "python.exe")
    smoke_path = tmp_path / ".oslab" / "install" / "smoke.ps1"
    assert result["command"]["arguments"] == [
        str(tmp_path / "scripts" / "cyntox_gpu_lease_exec.py"),
        "--",
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(smoke_path),
    ]
    assert result["parseErrors"] == 0
    smoke_code = smoke_path.read_text(encoding="utf-8-sig")
    assert "$ErrorActionPreference = 'Stop'" in smoke_code
    assert "http://127.0.0.1:11434/api/generate" in smoke_code
