$ErrorActionPreference = 'Stop'
$QwenArgs = @($args)
$projectRoot = $PSScriptRoot
$bootstrap = Join-Path $projectRoot 'scripts\bootstrap.ps1'
$qwenCli = Join-Path $projectRoot 'node_modules\@qwen-code\qwen-code\cli-entry.js'
$runtimeDir = Join-Path $projectRoot '.oslab\qwen-code'
$qwenHome = Join-Path $projectRoot '.oslab\qwen-code-home'
$interactiveWorkspace = Join-Path $projectRoot '.oslab\qwen-code-workspace'
$interactiveQwenDir = Join-Path $interactiveWorkspace '.qwen'
$interactiveSettingsPath = Join-Path $interactiveQwenDir 'settings.json'

function Test-QwenFlag {
    param(
        [string[]]$Arguments,
        [string[]]$Names
    )

    foreach ($argument in $Arguments) {
        foreach ($name in $Names) {
            if ($argument -eq $name -or $argument.StartsWith("$name=")) {
                return $true
            }
        }
    }
    return $false
}

function Ensure-SettingObject {
    param(
        [Parameter(Mandatory = $true)]$Parent,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if (-not $Parent.PSObject.Properties[$Name] -or $null -eq $Parent.$Name) {
        $Parent | Add-Member -MemberType NoteProperty -Name $Name -Value ([pscustomobject]@{}) -Force
    }
    return $Parent.$Name
}

function Set-SettingProperty {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)]$Value
    )

    $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value -Force
}

function Write-InteractiveSettings {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$DestinationPath
    )

    $settings = Get-Content -Raw -LiteralPath $SourcePath | ConvertFrom-Json
    $model = Ensure-SettingObject -Parent $settings -Name 'model'
    Set-SettingProperty -Object $model -Name 'maxSessionTurns' -Value -1
    Set-SettingProperty -Object $model -Name 'maxWallTimeSeconds' -Value -1
    Set-SettingProperty -Object $model -Name 'maxToolCalls' -Value -1
    Set-SettingProperty -Object $model -Name 'maxToolCallsPerTurn' -Value 500
    Set-SettingProperty -Object $model -Name 'skipStartupContext' -Value $true

    $tools = Ensure-SettingObject -Parent $settings -Name 'tools'
    Set-SettingProperty -Object $tools -Name 'approvalMode' -Value 'auto-edit'
    if ($tools.PSObject.Properties['disabled']) {
        $tools.PSObject.Properties.Remove('disabled')
    }
    if ($tools.PSObject.Properties['visible']) {
        $tools.PSObject.Properties.Remove('visible')
    }
    Set-SettingProperty -Object $tools -Name 'eager' -Value @(
        'read_file',
        'list_directory',
        'grep_search',
        'glob',
        'edit',
        'write_file'
    )
    $toolSearch = Ensure-SettingObject -Parent $tools -Name 'toolSearch'
    Set-SettingProperty -Object $toolSearch -Name 'enabled' -Value $true

    $context = Ensure-SettingObject -Parent $settings -Name 'context'
    $fileFiltering = Ensure-SettingObject -Parent $context -Name 'fileFiltering'
    Set-SettingProperty -Object $fileFiltering -Name 'respectGitIgnore' -Value $true
    Set-SettingProperty -Object $fileFiltering -Name 'respectQwenIgnore' -Value $true

    $permissions = Ensure-SettingObject -Parent $settings -Name 'permissions'
    $existingDeny = @()
    if ($permissions.PSObject.Properties['deny'] -and $null -ne $permissions.deny) {
        $existingDeny = @($permissions.deny)
    }
    $deny = @($existingDeny + 'display_image' | Where-Object { $_ } | Select-Object -Unique)
    Set-SettingProperty -Object $permissions -Name 'deny' -Value $deny

    $output = Ensure-SettingObject -Parent $settings -Name 'output'
    Set-SettingProperty -Object $output -Name 'format' -Value 'text'

    $settings | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $DestinationPath -Encoding utf8
}

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
$env:QWEN_HOME = $qwenHome
$env:QWEN_RUNTIME_DIR = $runtimeDir
$env:QWEN_CODE_SUPPRESS_YOLO_WARNING = '1'

New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $qwenHome | Out-Null
New-Item -ItemType Directory -Force -Path $interactiveQwenDir | Out-Null
$userSettingsPath = Join-Path $qwenHome 'settings.json'
if (-not (Test-Path -LiteralPath $userSettingsPath)) {
    '{ "$version": 4 }' | Set-Content -LiteralPath $userSettingsPath -Encoding utf8
}
Write-InteractiveSettings -SourcePath (Join-Path $projectRoot '.qwen\settings.json') -DestinationPath $interactiveSettingsPath

$hasOutputFormat = $false
$hasOutputFormat = Test-QwenFlag -Arguments $QwenArgs -Names @('-o', '--output-format')
$hasMaxSessionTurns = Test-QwenFlag -Arguments $QwenArgs -Names @('--max-session-turns')
$hasMaxToolCalls = Test-QwenFlag -Arguments $QwenArgs -Names @('--max-tool-calls')
$hasApprovalMode = Test-QwenFlag -Arguments $QwenArgs -Names @('--approval-mode')
$hasYolo = Test-QwenFlag -Arguments $QwenArgs -Names @('-y', '--yolo')
$hasExcludeTools = Test-QwenFlag -Arguments $QwenArgs -Names @('--exclude-tools')
$hasAppendSystemPrompt = Test-QwenFlag -Arguments $QwenArgs -Names @('--append-system-prompt')
$hasIncludeDirectories = Test-QwenFlag -Arguments $QwenArgs -Names @('--include-directories', '--add-dir')

$finalArgs = @()
if (-not $hasOutputFormat) {
    $finalArgs += @('--output-format', 'text')
}
if (-not $hasMaxSessionTurns) {
    $finalArgs += @('--max-session-turns', '-1')
}
if (-not $hasMaxToolCalls) {
    $finalArgs += @('--max-tool-calls', '-1')
}
if (-not $hasApprovalMode -and -not $hasYolo) {
    $finalArgs += @('--approval-mode', 'auto-edit')
}
if (-not $hasExcludeTools) {
    $finalArgs += @('--exclude-tools', 'display_image')
}
if (-not $hasIncludeDirectories) {
    $finalArgs += @('--include-directories', $projectRoot)
}
if (-not $hasAppendSystemPrompt) {
    $finalArgs += @(
        '--append-system-prompt',
        "You are running from the repo-local qwen-code.ps1 human-use launcher. The primary project root is: $projectRoot. Keep startup context lean. Treat .md, .json, .py, .ps1, .toml, .yaml, .txt, and similar repository files as text. Use absolute paths under the primary project root with read_file, list_directory, glob, and grep_search for text files. Do not use display_image for text files; display_image is denied in this profile."
    )
}
$finalArgs += $QwenArgs

Push-Location -LiteralPath $interactiveWorkspace
try {
    & $nodePath $qwenCli @finalArgs
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

exit $exitCode
