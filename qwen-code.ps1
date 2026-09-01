$ErrorActionPreference = 'Stop'
$QwenArgs = @($args)
$projectRoot = $PSScriptRoot
$bootstrap = Join-Path $projectRoot 'scripts\bootstrap.ps1'
$qwenCli = Join-Path $projectRoot 'node_modules\@qwen-code\qwen-code\cli-entry.js'

if (-not (Test-Path -LiteralPath $qwenCli)) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $bootstrap
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$nodePath = $null
if ($nodeCommand) {
    $nodePath = $nodeCommand.Source
}

if (-not $nodePath) {
    $bundledNode = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
    if (Test-Path -LiteralPath $bundledNode) {
        $nodePath = $bundledNode
    }
}

if (-not $nodePath) {
    throw 'Node.js was not found. Run scripts\bootstrap.ps1 inside Codex or install Node.js.'
}

$nodeDir = Split-Path -Parent $nodePath
$env:PATH = "$nodeDir;$env:PATH"
if (-not $env:OSLAB_OLLAMA_API_KEY) {
    $env:OSLAB_OLLAMA_API_KEY = 'ollama-local-no-auth'
}
$env:QWEN_RUNTIME_DIR = Join-Path $projectRoot '.oslab\qwen-code'
$env:QWEN_CODE_SUPPRESS_YOLO_WARNING = '1'

$hasOutputFormat = $false
foreach ($arg in $QwenArgs) {
    if ($arg -eq '-o' -or $arg -eq '--output-format' -or $arg.StartsWith('--output-format=')) {
        $hasOutputFormat = $true
    }
}

$finalArgs = @()
if (-not $hasOutputFormat) {
    $finalArgs += @('--output-format', 'text')
}
$finalArgs += $QwenArgs

Push-Location -LiteralPath $projectRoot
try {
    & $nodePath $qwenCli @finalArgs
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

exit $exitCode
