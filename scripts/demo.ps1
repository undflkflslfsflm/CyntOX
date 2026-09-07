$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RepoRoot

function Invoke-Oslab {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]] $Args)
    $Uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $Uv) {
        & uv run oslab @Args
        return
    }
    $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    & $Python -m oslab.cli @Args
}

Invoke-Oslab doctor --json
Invoke-Oslab build --target fixture --profile debug --json
Invoke-Oslab boot --target fixture --seed 1 --json
Invoke-Oslab test --target fixture --test fail --seed 1 --json
Invoke-Oslab test --target fixture --test crash --seed 1 --json
Invoke-Oslab reproduce --mode crash --cold-boots 2 --json
Invoke-Oslab fuzz run --campaign-id demo-fixture --seed 101 --iterations 6 --json
Invoke-Oslab training dry-run --json
Invoke-Oslab report --experiment demo --json
Invoke-Oslab integrity check --json
