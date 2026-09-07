# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from oslab.mythos_prompt import PROMPT_SENTINEL, canonical_prompt_path, canonical_prompt_sha256

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "cyntox-code.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def run_launcher_functions(expression: str) -> object:
    if POWERSHELL is None:
        pytest.skip("PowerShell is unavailable")
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{str(LAUNCHER).replace("'", "''")}',
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -gt 0) {{ throw ($errors | ForEach-Object Message) -join '; ' }}
$functions = $ast.FindAll({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}}, $true)
foreach ($function in $functions) {{ Invoke-Expression $function.Extent.Text }}
{expression}
"""
    completed = subprocess.run(  # noqa: S603 - fixed local PowerShell executable
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


def test_launcher_hash_matches_python_normalized_prompt_hash() -> None:
    prompt_path = canonical_prompt_path(ROOT).relative_to(ROOT).as_posix()
    result = run_launcher_functions(
        f"""
$prompt = (Get-Content -Raw -LiteralPath '{prompt_path}' -Encoding UTF8).Trim()
@{{ hash = Get-CyntOXTextSha256 -Value $prompt }} | ConvertTo-Json -Compress
"""
    )
    assert result == {"hash": canonical_prompt_sha256(ROOT)}


def test_launcher_defaults_to_v1_and_only_selects_v2_for_exact_opt_in() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "$env:CYNTOX_MYTHOS_V2_CANDIDATE -eq '1'" in source
    assert "prompts\\archive\\mythos-system-v1.md" in source
    assert "CYNTOX_MYTHOS_SYSTEM_PROMPT_V1" in source
    assert "prompts\\mythos-system.md" in source
    assert "CYNTOX_MYTHOS_SYSTEM_PROMPT_V2" in source


def test_launcher_metadata_only_refreshes_generated_settings_before_exit() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    metadata_block = source.index("if ($isMetadataOnly) {")
    settings_write = source.index("Write-InteractiveSettings", metadata_block)
    metadata_exec = source.index("& $nodePath $cyntoxCli @CyntOXArgs", metadata_block)
    metadata_exit = source.index("exit $LASTEXITCODE", metadata_block)
    live_proxy_start = source.index("Start-LocalOllamaIfNeeded", metadata_block)

    assert settings_write < metadata_exec < metadata_exit < live_proxy_start
    assert "$cyntoxBaseUrl = $cyntoxProxyBaseUrl" in source[metadata_block:metadata_exit]


def test_launcher_extracts_last_explicit_base_url_and_wraps_direct_ollama() -> None:
    result = run_launcher_functions(
        """
$arguments = @(
    '--openai-base-url', 'http://127.0.0.1:1/v1',
    '--openai-base-url=http://127.0.0.1:11434/v1'
)
@{
    value = Get-CyntOXOptionValue -Arguments $arguments -Name '--openai-base-url'
} | ConvertTo-Json -Compress
"""
    )
    source = LAUNCHER.read_text(encoding="utf-8")

    assert result == {"value": "http://127.0.0.1:11434/v1"}
    assert "scripts\\cyntox_gpu_lease_exec.py" in source
    assert "if ($useDirectGpuLease)" in source


def test_launcher_fails_closed_for_missing_or_empty_canonical_prompt(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"
    empty = tmp_path / "empty.md"
    empty.write_text(" \r\n\t", encoding="utf-8")
    result = run_launcher_functions(
        rf"""
$messages = @()
foreach ($path in @(
    '{str(missing).replace("'", "''")}',
    '{str(empty).replace("'", "''")}'
)) {{
    try {{
        $null = Get-CyntOXCanonicalPrompt -Path $path
        $messages += 'unexpected success'
    }} catch {{
        $messages += $_.Exception.Message
    }}
}}
@{{ messages = $messages }} | ConvertTo-Json -Compress
"""
    )
    assert "Canonical Mythos prompt is missing" in result["messages"][0]
    assert "Canonical Mythos prompt is empty" in result["messages"][1]


def test_launcher_merges_prompt_once_for_direct_and_preappended_paths() -> None:
    result = run_launcher_functions(
        rf"""
$sentinel = '{PROMPT_SENTINEL}'
$required = "/no_think`n[$sentinel]`nCanonical v2`n[/$sentinel]`n`nLauncher context"
$old = "/no_think`n[$sentinel]`nStale copy`n[/$sentinel]`n`nUser preference"
$direct = @(Merge-CyntOXSystemPromptArguments -Arguments @('-p', 'ping') -RequiredPrompt $required -Sentinel $sentinel)
$preappended = @(Merge-CyntOXSystemPromptArguments -Arguments @('--append-system-prompt', $old, '-p', 'ping') -RequiredPrompt $required -Sentinel $sentinel)
$malformed = @(Merge-CyntOXSystemPromptArguments -Arguments @('--append-system-prompt=[' + $sentinel + ']broken', '-p', 'ping') -RequiredPrompt $required -Sentinel $sentinel)
@{{ direct = $direct; preappended = $preappended; malformed = $malformed }} | ConvertTo-Json -Depth 8 -Compress
"""
    )

    for arguments in result.values():
        assert arguments.count("--append-system-prompt") == 1
        prompt = arguments[arguments.index("--append-system-prompt") + 1]
        assert prompt.count(f"[{PROMPT_SENTINEL}]") == 1
        assert prompt.count(f"[/{PROMPT_SENTINEL}]") == 1
        assert "Canonical v2" in prompt
        assert "Stale copy" not in prompt
    preappended_prompt = result["preappended"][
        result["preappended"].index("--append-system-prompt") + 1
    ]
    assert "User preference" in preappended_prompt


def test_launcher_proxy_config_rejects_stale_prompt_hash() -> None:
    result = run_launcher_functions(
        """
$cyntoxDefaultMaxTokens = 8192
$cyntoxMaxAllowedTokens = 32768
$cyntoxDefaultNumCtx = 32768
$cyntoxUpstreamModel = 'cyntox:latest'
$health = [pscustomobject]@{
    ok = $true
    service = 'cyntox-openai-proxy'
    max_tokens = 8192
    num_ctx = 32768
    upstream_model = 'cyntox:latest'
    target_base = 'http://127.0.0.1:11434'
    prompt_version = 'v2'
    prompt_sha256 = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
}
@{
    current = Test-CyntOXProxyConfigCurrent -Health $health -TargetBaseUrl 'http://127.0.0.1:11434/v1' -ExpectedPromptVersion 'v2' -ExpectedPromptSha256 ('a' * 64)
    stale = Test-CyntOXProxyConfigCurrent -Health $health -TargetBaseUrl 'http://127.0.0.1:11434/v1' -ExpectedPromptVersion 'v2' -ExpectedPromptSha256 ('b' * 64)
} | ConvertTo-Json -Compress
"""
    )
    assert result == {"stale": False, "current": True}


def test_launcher_restarts_stale_hash_proxy_before_reuse() -> None:
    result = run_launcher_functions(
        rf"""
$projectRoot = '{str(ROOT).replace("'", "''")}'
$runtimeDir = [System.IO.Path]::GetTempPath()
$cyntoxProxyPort = 11437
$cyntoxProxyBaseUrl = 'http://127.0.0.1:11437/v1'
$cyntoxUpstreamModel = 'cyntox:latest'
$cyntoxDefaultMaxTokens = 8192
$cyntoxMaxAllowedTokens = 32768
$cyntoxDefaultNumCtx = 32768
$mythosPromptVersion = 'v2'
$mythosPromptHash = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
$mythosPromptPath = Join-Path $projectRoot 'prompts/mythos-system.md'
$script:healthCalls = 0
$script:stopCalls = 0
$script:startCalls = 0
$script:launchArguments = @()
function Get-CyntOXProxyHealth {{
    param([string]$HealthUrl)
    $script:healthCalls++
    $hash = if ($script:healthCalls -eq 1) {{ 'a' * 64 }} else {{ 'b' * 64 }}
    return [pscustomobject]@{{
        ok = $true
        service = 'cyntox-openai-proxy'
        max_tokens = 8192
        num_ctx = 32768
        upstream_model = 'cyntox:latest'
        target_base = 'http://127.0.0.1:11434'
        prompt_version = 'v2'
        prompt_sha256 = $hash
    }}
}}
function Test-CyntOXProxyHealth {{ param([string]$HealthUrl) return $false }}
function Stop-StaleCyntOXProxyOnPort {{ param([int]$Port) $script:stopCalls++ }}
function Get-CyntOXProxyPython {{ return 'C:\fake\python.exe' }}
function Start-Process {{
    param($FilePath, $ArgumentList, $WindowStyle, $RedirectStandardOutput, $RedirectStandardError)
    $script:startCalls++
    $script:launchArguments = @($ArgumentList)
}}
$endpoint = Start-CyntOXProxyIfAvailable -TargetBaseUrl 'http://127.0.0.1:11434/v1'
@{{ endpoint = $endpoint; stops = $script:stopCalls; starts = $script:startCalls; arguments = $script:launchArguments }} | ConvertTo-Json -Depth 4 -Compress
"""
    )
    assert result["endpoint"] == "http://127.0.0.1:11437/v1"
    assert result["stops"] == 1
    assert result["starts"] == 1
    launch_arguments = result["arguments"]
    listen_host_index = launch_arguments.index("--listen-host")
    assert launch_arguments[listen_host_index + 1] == "127.0.0.1"
    assert launch_arguments[listen_host_index + 2] == "--listen-port"
    assert launch_arguments.count("127.0.0.1") == 1


def test_launcher_disabled_proxy_uses_direct_endpoint() -> None:
    result = run_launcher_functions(
        """
$env:OSLAB_CYNTOX_DISABLE_PROXY = '1'
$endpoint = Start-CyntOXProxyIfAvailable -TargetBaseUrl 'http://127.0.0.1:11434/v1'
@{ endpoint = $endpoint } | ConvertTo-Json -Compress
"""
    )
    assert result == {"endpoint": "http://127.0.0.1:11434/v1"}
