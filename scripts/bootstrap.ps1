$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$pythonPath = $null
if ($pythonCommand) {
    try {
        & $pythonCommand.Source -c 'import sys; assert sys.version_info >= (3, 12)' 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonPath = $pythonCommand.Source }
    } catch {}
}
if (-not $pythonPath) {
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundled) { $pythonPath = $bundled }
}
if (-not $pythonPath) { throw 'Python 3.12+ was not found. Install Python or run inside Codex with bundled dependencies.' }
$venv = Join-Path $projectRoot '.venv'
if (-not (Test-Path -LiteralPath $venv)) { & $pythonPath -m venv $venv }
$venvPython = Join-Path $venv 'Scripts\python.exe'
& $venvPython -m pip install --disable-pip-version-check 'uv==0.8.14'
$uv = Join-Path $venv 'Scripts\uv.exe'
& $uv sync --all-groups --locked
if (Test-Path -LiteralPath (Join-Path $projectRoot 'package.json')) {
    $pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
    if (-not $pnpm) { throw 'pnpm was not found in PATH or Codex bundled runtime.' }
    & $pnpm.Source install --frozen-lockfile --offline
    if ($LASTEXITCODE -ne 0) { & $pnpm.Source install --frozen-lockfile }
    if ($LASTEXITCODE -ne 0) { throw 'pnpm install failed.' }
}
& $uv run oslab init --json
