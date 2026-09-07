param(
    [string]$PythonPath = '',
    [string]$PnpmPath = ''
)

$ErrorActionPreference = 'Stop'
# Native failures are checked explicitly, including the expected offline pnpm retry.
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

function Test-BootstrapPython {
    param([string]$Executable)
    try {
        & $Executable -c 'import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] < (3, 15) else 1)' *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Invoke-BootstrapCommand {
    param([string]$Executable, [string[]]$Arguments, [string]$Step)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code $LASTEXITCODE). Setup did not complete."
    }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$selectedPython = $null
if ($PythonPath) {
    if (-not (Test-BootstrapPython -Executable $PythonPath)) {
        throw 'The supplied PythonPath must point to Python 3.12, 3.13, or 3.14.'
    }
    $selectedPython = $PythonPath
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand -and (Test-BootstrapPython -Executable $pythonCommand.Source)) {
        $selectedPython = $pythonCommand.Source
    }
}
if (-not $selectedPython) {
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if ((Test-Path -LiteralPath $bundled) -and (Test-BootstrapPython -Executable $bundled)) {
        $selectedPython = $bundled
    }
}
if (-not $selectedPython) {
    throw 'Python 3.12, 3.13, or 3.14 was not found. Install a supported Python or supply -PythonPath.'
}
$venv = Join-Path $projectRoot '.venv'
if (-not (Test-Path -LiteralPath $venv)) {
    Invoke-BootstrapCommand -Executable $selectedPython -Arguments @('-m', 'venv', $venv) -Step 'Python environment creation'
}
$venvPython = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-BootstrapPython -Executable $venvPython)) {
    throw 'The existing .venv is incomplete or uses an unsupported Python. Move it aside and rerun setup with Python 3.12, 3.13, or 3.14.'
}
Invoke-BootstrapCommand -Executable $venvPython -Arguments @('-m', 'pip', 'install', '--no-input', '--disable-pip-version-check', 'uv==0.8.14') -Step 'uv installation'
$uv = Join-Path $venv 'Scripts\uv.exe'
Push-Location -LiteralPath $projectRoot
try {
    Invoke-BootstrapCommand -Executable $uv -Arguments @('sync', '--all-groups', '--locked', '--python', $venvPython) -Step 'Python dependency installation'
    if (Test-Path -LiteralPath (Join-Path $projectRoot 'package.json')) {
        $selectedPnpm = $PnpmPath
        if (-not $selectedPnpm) {
            $pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
            if ($pnpm) { $selectedPnpm = $pnpm.Source }
        }
        if (-not $selectedPnpm) { throw 'pnpm was not found. Install pnpm or supply -PnpmPath.' }
        $previousCI = $env:CI
        try {
            # pnpm must rebuild incompatible generated node_modules without a
            # hidden confirmation prompt; CI also suppresses update notices.
            $env:CI = 'true'
            & $selectedPnpm install --frozen-lockfile --offline
            if ($LASTEXITCODE -ne 0) {
                Invoke-BootstrapCommand -Executable $selectedPnpm -Arguments @('install', '--frozen-lockfile') -Step 'pnpm dependency installation'
            }
        } finally {
            $env:CI = $previousCI
        }
    }
    Invoke-BootstrapCommand -Executable $uv -Arguments @('run', 'oslab', 'init', '--json') -Step 'CyntOX initialization'
} finally {
    Pop-Location
}
