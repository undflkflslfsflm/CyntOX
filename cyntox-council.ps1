$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$runner = Join-Path $projectRoot 'scripts\cyntox_council.py'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = 'python'
}

& $python $runner @args
exit $LASTEXITCODE
