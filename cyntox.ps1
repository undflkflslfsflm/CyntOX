$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$cliScript = Join-Path $projectRoot 'scripts\cyntox_cli.py'

if (-not (Test-Path -LiteralPath $cliScript)) {
    throw 'scripts\cyntox_cli.py was not found next to cyntox.ps1.'
}

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = 'python'
}

& $python $cliScript @args
exit $LASTEXITCODE
