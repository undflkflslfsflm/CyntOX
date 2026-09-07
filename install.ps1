param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'CyntOX\app'),
    [string]$SourcePath,
    [switch]$RegisterOnly
)

$ErrorActionPreference = 'Stop'

function Invoke-CyntOXInstallCommand {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Setup stopped: $Executable failed (exit $LASTEXITCODE). Fix the error above and rerun the installer."
    }
}

function Update-CyntOXInstallPath {
    $paths = @(
        $env:PATH
        [Environment]::GetEnvironmentVariable('Path', 'User')
        [Environment]::GetEnvironmentVariable('Path', 'Machine')
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312')
        (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd')
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama')
        (Join-Path $env:ProgramFiles 'nodejs')
    )
    $env:PATH = ($paths | Where-Object { $_ }) -join ';'
}

function Find-CyntOXInstallTool {
    param([string]$Name, [string[]]$Candidates = @(), [string[]]$VersionArguments = @('--version'))
    $commands = @(Get-Command $Name -All -CommandType Application -ErrorAction SilentlyContinue)
    foreach ($candidate in @($Candidates) + @($commands | ForEach-Object Source)) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            & $candidate @VersionArguments *> $null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { continue }
    }
    return $null
}

function Install-CyntOXPackage {
    param([string]$Id, [string]$Scope)
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Installing $Id requires Windows App Installer (winget). Install App Installer from Microsoft Store, then rerun this command."
    }
    Write-Host "Installing $Id..."
    $arguments = @('install', '--exact', '--id', $Id, '--source', 'winget', '--silent',
        '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
    if ($Scope) { $arguments += @('--scope', $Scope) }
    Invoke-CyntOXInstallCommand $winget.Source $arguments
    Update-CyntOXInstallPath
}

function Get-CyntOXInstallTools {
    param([string]$Root)
    Update-CyntOXInstallPath
    $pythonArgs = @('-c', 'import sys; assert sys.version_info[:2] == (3, 12)')
    $pythonCandidates = @(
        (Join-Path $Root '.venv\Scripts\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
    )
    $python = Find-CyntOXInstallTool 'python.exe' $pythonCandidates $pythonArgs
    if (-not $python) {
        Install-CyntOXPackage 'Python.Python.3.12' 'user'
        $python = Find-CyntOXInstallTool 'python.exe' $pythonCandidates $pythonArgs
    }
    if (-not $python) { throw 'Python 3.12 could not be started.' }

    $nodeArgs = @('-e', 'process.exit(parseInt(process.versions.node) >= 22 ? 0 : 1)')
    $nodeCandidates = @((Join-Path $env:ProgramFiles 'nodejs\node.exe'))
    $node = Find-CyntOXInstallTool 'node.exe' $nodeCandidates $nodeArgs
    if (-not $node) {
        Install-CyntOXPackage 'OpenJS.NodeJS.LTS'
        $node = Find-CyntOXInstallTool 'node.exe' $nodeCandidates $nodeArgs
    }
    if (-not $node) { throw 'Node.js 22+ could not be started.' }
    $env:PATH = (Split-Path -Parent $node) + ';' + $env:PATH

    $ollama = Find-CyntOXInstallTool 'ollama.exe'
    if (-not $ollama) {
        Install-CyntOXPackage 'Ollama.Ollama' 'user'
        $ollama = Find-CyntOXInstallTool 'ollama.exe'
    }
    if (-not $ollama) { throw 'Ollama could not be started.' }

    $package = Get-Content -LiteralPath (Join-Path $Root 'package.json') -Raw | ConvertFrom-Json
    $pnpmVersion = $package.packageManager -replace '^pnpm@', ''
    if ($pnpmVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid pinned pnpm version in package.json.' }
    $toolsRoot = Join-Path $env:LOCALAPPDATA 'CyntOX\tools'
    $pnpm = Join-Path $toolsRoot 'node_modules\.bin\pnpm.cmd'
    $installedVersion = if (Test-Path -LiteralPath $pnpm) { & $pnpm --version }
    if ($installedVersion -ne $pnpmVersion) {
        $npm = Join-Path (Split-Path -Parent $node) 'npm.cmd'
        New-Item -ItemType Directory -Force -Path $toolsRoot | Out-Null
        if (Test-Path -LiteralPath $npm) {
            Invoke-CyntOXInstallCommand $npm @('install', '--prefix', $toolsRoot,
                '--save-exact', '--no-audit', '--no-fund', "pnpm@$pnpmVersion")
        } else {
            $existingPnpm = Get-Command pnpm.cmd -CommandType Application -ErrorAction SilentlyContinue
            if (-not $existingPnpm) { throw 'Node.js needs npm or pnpm to install the pinned package manager.' }
            Invoke-CyntOXInstallCommand $existingPnpm.Source @('--dir', $toolsRoot, 'add',
                '--ignore-workspace', '--save-exact', "pnpm@$pnpmVersion")
        }
    }
    return @{ Python = $python; Node = $node; Ollama = $ollama; Pnpm = $pnpm }
}

function Get-CyntOXCheckout {
    param([string]$Destination, [string]$ExistingSource)
    if ($ExistingSource) {
        $root = (Resolve-Path -LiteralPath $ExistingSource).Path
        if (-not (Test-Path -LiteralPath (Join-Path $root 'cyntox.cmd'))) {
            throw 'SourcePath must point to a CyntOX checkout.'
        }
        return $root
    }
    Update-CyntOXInstallPath
    $git = Find-CyntOXInstallTool 'git.exe'
    if (-not $git) {
        Install-CyntOXPackage 'Git.Git' 'user'
        $git = Find-CyntOXInstallTool 'git.exe'
    }
    if (-not $git) { throw 'Git could not be started.' }
    $repository = 'https://github.com/undflkflslfsflm/CyntOX.git'
    $root = [IO.Path]::GetFullPath($Destination)
    if (Test-Path -LiteralPath $root) {
        if (-not (Test-Path -LiteralPath (Join-Path $root '.git'))) {
            throw "Install directory already exists and is not a Git checkout: $root. Choose another InstallDir."
        }
        $remote = & $git -C $root remote get-url origin
        if ($LASTEXITCODE -ne 0 -or $remote -ne $repository) { throw "Unexpected Git origin in $root." }
        $branch = & $git -C $root branch --show-current
        if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') { throw 'Installer updates require the main branch.' }
        $dirty = & $git -C $root status --porcelain
        if ($LASTEXITCODE -ne 0 -or $dirty) {
            throw "Local changes found in $root. Commit or move them before rerunning; nothing was overwritten."
        }
        Invoke-CyntOXInstallCommand $git @('-C', $root, 'pull', '--ff-only', 'origin', 'main')
    } else {
        Write-Host 'Downloading CyntOX from GitHub...'
        Invoke-CyntOXInstallCommand $git @('clone', '--branch', 'main', '--single-branch', $repository, $root)
    }
    return $root
}

function Start-CyntOXInstallOllama {
    param([string]$Ollama, [string]$Root)
    $endpoint = 'http://127.0.0.1:11434'
    try { return Invoke-RestMethod "$endpoint/api/tags" -TimeoutSec 3 } catch { }
    $logs = Join-Path $Root '.oslab\install'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $oldHost = $env:OLLAMA_HOST
    try {
        $env:OLLAMA_HOST = '127.0.0.1:11434'
        Start-Process -FilePath $Ollama -ArgumentList 'serve' -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logs 'ollama.log') `
            -RedirectStandardError (Join-Path $logs 'ollama-error.log') | Out-Null
    } finally { $env:OLLAMA_HOST = $oldHost }
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try { return Invoke-RestMethod "$endpoint/api/tags" -TimeoutSec 2 } catch { Start-Sleep -Seconds 1 }
    }
    throw "Ollama did not start. Inspect $logs\ollama-error.log and rerun setup."
}

function Initialize-CyntOXInstallModel {
    param([string]$Ollama, [string]$Root)
    $tags = Start-CyntOXInstallOllama $Ollama $Root
    if (@($tags.models | Where-Object { $_.name -eq 'cyntox:latest' }).Count -gt 0) {
        Write-Host 'Keeping your existing cyntox model.'
        return
    }
    $modelFile = Get-CyntOXInstallModelFile $Root
    $template = Get-Content -LiteralPath (Join-Path $Root 'config\Modelfile.cyntox') -Raw
    $modelPath = $modelFile.Replace('\', '/')
    $modelfile = Join-Path $Root '.oslab\install\Modelfile'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $modelfile) | Out-Null
    $content = [regex]::Replace($template, '(?m)^FROM [^\r\n]+', [System.Text.RegularExpressions.MatchEvaluator]{
        param($match)
        return 'FROM "' + $modelPath + '"'
    })
    [IO.File]::WriteAllText($modelfile, $content, (New-Object System.Text.UTF8Encoding($false)))
    $oldHost = $env:OLLAMA_HOST
    try {
        $env:OLLAMA_HOST = '127.0.0.1:11434'
        Write-Host 'Importing the verified CyntOX model into Ollama...'
        Invoke-CyntOXInstallCommand $Ollama @('create', 'cyntox', '-f', $modelfile)
    } finally { $env:OLLAMA_HOST = $oldHost }
}

function Get-CyntOXInstallModelFile {
    param([string]$Root)
    $modelLock = Get-Content -LiteralPath (Join-Path $Root 'config\install-model.lock.json') -Raw | ConvertFrom-Json
    if ($modelLock.sha256 -notmatch '^[a-f0-9]{64}$' -or $modelLock.revision -notmatch '^[a-f0-9]{40}$' -or
        $modelLock.filename -ne [IO.Path]::GetFileName($modelLock.filename) -or $modelLock.size_bytes -le 0) {
        throw 'Invalid installer model lock.'
    }
    $modelRoot = Join-Path $env:LOCALAPPDATA 'CyntOX\models'
    New-Item -ItemType Directory -Force -Path $modelRoot | Out-Null
    $destination = Join-Path $modelRoot $modelLock.filename
    if (-not (Test-Path -LiteralPath $destination)) {
        $ollamaStorage = if ($env:OLLAMA_MODELS) { $env:OLLAMA_MODELS } else { Join-Path $env:USERPROFILE '.ollama\models' }
        foreach ($storagePath in @($modelRoot, $ollamaStorage)) {
            $driveRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($storagePath))
            $drive = New-Object System.IO.DriveInfo($driveRoot)
            if ($drive.AvailableFreeSpace -lt 50GB) { throw "Model setup requires 50 GiB free on $driveRoot." }
        }
        $partial = $destination + '.part'
        if (-not (Test-Path -LiteralPath $partial) -or (Get-Item -LiteralPath $partial).Length -ne $modelLock.size_bytes) {
            $curl = Get-Command curl.exe -CommandType Application -ErrorAction SilentlyContinue
            if (-not $curl) { throw 'Windows curl.exe is required for the resumable model download.' }
            $url = "https://huggingface.co/$($modelLock.repository)/resolve/$($modelLock.revision)/$($modelLock.filename)"
            Write-Host 'Downloading the CyntOX model (about 17 GB). Interrupted downloads resume on rerun...'
            Invoke-CyntOXInstallCommand $curl.Source @('--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
                '--connect-timeout', '30', '--retry', '3', '--continue-at', '-', '--output', $partial, $url)
        }
        Write-Host 'Verifying model size and checksum...'
        if ((Get-Item -LiteralPath $partial).Length -ne $modelLock.size_bytes -or
            (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $modelLock.sha256) {
            $quarantine = $partial + '.invalid-' + [guid]::NewGuid().ToString('N')
            Move-Item -LiteralPath $partial -Destination $quarantine
            throw "Model verification failed. Invalid download saved at $quarantine. Rerun to download a fresh copy."
        }
        Move-Item -LiteralPath $partial -Destination $destination
    } elseif ((Get-Item -LiteralPath $destination).Length -ne $modelLock.size_bytes -or
        (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $modelLock.sha256) {
        throw "Cached model failed verification: $destination. Move it aside before rerunning setup."
    }
    return $destination
}

function Test-CyntOXInstallModel {
    Write-Host 'Checking that the local model can answer...'
    $body = @{ model = 'cyntox:latest'; prompt = 'Reply with the word READY.'; stream = $false; think = $false
        options = @{ num_ctx = 1024; num_predict = 16; temperature = 0 } } | ConvertTo-Json -Depth 5
    $answer = Invoke-RestMethod 'http://127.0.0.1:11434/api/generate' -Method Post `
        -ContentType 'application/json' -Body $body -TimeoutSec 300
    if (-not $answer.done -or [string]::IsNullOrWhiteSpace($answer.response)) {
        throw 'The local model did not return a complete answer. Setup is not yet ready.'
    }
}

function Invoke-CyntOXInstallModelSmoke {
    param([string]$Root)
    $smokeFile = Join-Path $Root '.oslab\install\smoke.ps1'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $smokeFile) | Out-Null
    $smokeCode = "$" + "ErrorActionPreference = 'Stop'`r`n" + ${function:Test-CyntOXInstallModel}.ToString()
    [IO.File]::WriteAllText($smokeFile, $smokeCode, (New-Object System.Text.UTF8Encoding($true)))
    $oldTimeout = $env:CYNTOX_GPU_LEASE_TIMEOUT_SECONDS
    try {
        $env:CYNTOX_GPU_LEASE_TIMEOUT_SECONDS = '300'
        Invoke-CyntOXInstallCommand (Join-Path $Root '.venv\Scripts\python.exe') @(
            (Join-Path $Root 'scripts\cyntox_gpu_lease_exec.py'), '--', 'powershell.exe',
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $smokeFile)
    } finally { $env:CYNTOX_GPU_LEASE_TIMEOUT_SECONDS = $oldTimeout }
}

function Register-CyntOXCommand {
    param([string]$Root, [string[]]$ToolDirectories = @(), [string]$BinDir = (Join-Path $env:LOCALAPPDATA 'CyntOX\bin'))
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    $utf8 = New-Object System.Text.UTF8Encoding($true)
    $config = @{ root = $Root; tool_directories = @($ToolDirectories) } | ConvertTo-Json
    [IO.File]::WriteAllText((Join-Path $BinDir 'install.json'), $config, $utf8)
    $launcher = @'
$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$env:PATH = (@($config.tool_directories) + @($env:PATH)) -join ';'
& (Join-Path $config.root 'cyntox.ps1') @args
exit $LASTEXITCODE
'@
    [IO.File]::WriteAllText((Join-Path $BinDir 'launch.ps1'), $launcher, $utf8)
    $shim = "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%~dp0launch.ps1`" %*`r`nexit /b %ERRORLEVEL%`r`n"
    [IO.File]::WriteAllText((Join-Path $BinDir 'cyntox.cmd'), $shim, [Text.Encoding]::ASCII)
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $entries = @($userPath -split ';' | Where-Object { $_ })
    $normalizedEntries = @($entries | ForEach-Object { $_.TrimEnd('\') })
    if ($normalizedEntries -notcontains $BinDir.TrimEnd('\')) {
        [Environment]::SetEnvironmentVariable('Path', (@($BinDir) + $entries) -join ';', 'User')
    }
    $env:PATH = $BinDir + ';' + $env:PATH
}

function Install-CyntOX {
    param([string]$Destination, [string]$ExistingSource, [switch]$OnlyRegister)
    if ($env:OS -ne 'Windows_NT') { throw 'This installer supports Windows. See README.md for Bash/WSL bootstrap.' }
    if ($OnlyRegister -and -not $ExistingSource) { throw 'RegisterOnly requires SourcePath to an already prepared checkout.' }
    $root = Get-CyntOXCheckout $Destination $ExistingSource
    $toolDirectories = @()
    if (-not $OnlyRegister) {
        $tools = Get-CyntOXInstallTools $root
        $git = Find-CyntOXInstallTool 'git.exe'
        if (-not $git) { Install-CyntOXPackage 'Git.Git' 'user'; $git = Find-CyntOXInstallTool 'git.exe' }
        if (-not $git) { throw 'Git could not be started.' }
        $toolDirectories = @($tools.Node, $tools.Ollama, $tools.Pnpm, $git) | ForEach-Object { Split-Path -Parent $_ }
        Write-Host 'Setting up the locked CyntOX environment...'
        Invoke-CyntOXInstallCommand 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', (Join-Path $root 'scripts\bootstrap.ps1'), '-PythonPath', $tools.Python, '-PnpmPath', $tools.Pnpm)
        Initialize-CyntOXInstallModel $tools.Ollama $root
        Invoke-CyntOXInstallModelSmoke $root
    }
    Write-Host 'Checking the launcher...'
    Invoke-CyntOXInstallCommand (Join-Path $root 'cyntox.cmd') @('run', '--version')
    Register-CyntOXCommand $root $toolDirectories
    Write-Host ''
    Write-Host 'CyntOX is ready. Type: cyntox run'
}

Install-CyntOX -Destination $InstallDir -ExistingSource $SourcePath -OnlyRegister:$RegisterOnly
