param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $OslabArgs
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$bootstrap = Join-Path $projectRoot 'scripts\bootstrap.ps1'

if (-not (Test-Path -LiteralPath $venvPython)) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $bootstrap
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Push-Location -LiteralPath $projectRoot
try {
    & $venvPython -m oslab.cli @OslabArgs
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

exit $exitCode
