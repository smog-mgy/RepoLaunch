param(
    [ValidateRange(1024, 65535)][int]$Port = 8787,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$startupLog = Join-Path $PSScriptRoot 'startup.log'
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'

function Write-StartupFailure([string]$Message) {
    $line = ('{0} ERROR launcher: {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
    Write-Host $line -ForegroundColor Red
    try { Add-Content -LiteralPath $startupLog -Value $line -Encoding UTF8 } catch {}
}

if (-not (Test-Path -LiteralPath $projectPython -PathType Leaf)) {
    Write-StartupFailure 'Project Python was not found. Run setup.ps1 first to install Python 3.11+ and dependencies.'
    exit 1
}
try {
    Write-Host 'RepoLaunch is starting. Keep this window open while using the app.'
    Write-Host ('Startup log: {0}' -f $startupLog)
    $launchArgs = @('-m', 'repolaunch', '--port', $Port, '--log-file', $startupLog)
    if (-not $PSBoundParameters.ContainsKey('Port')) { $launchArgs += '--auto-port' }
    if ($NoBrowser) { $launchArgs += '--no-browser' } else { $launchArgs += '--open-browser' }
    & $projectPython @launchArgs
    $launchCode = $LASTEXITCODE
    if ($launchCode -ne 0) {
        Write-StartupFailure ('RepoLaunch exited with code {0}. See startup.log for details.' -f $launchCode)
    }
    exit $launchCode
}
catch {
    Write-StartupFailure $_.Exception.Message
    exit 1
}
