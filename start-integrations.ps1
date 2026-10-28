param([ValidateRange(60,1200)][int]$Timeout = 600, [switch]$NoApp)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    Write-Host 'Run setup.ps1 first. The project Python environment was not found.' -ForegroundColor Red
    exit 1
}
Write-Host 'Preparing real local MySQL, Milvus and Langfuse services.'
Write-Host 'Images and data can require several GB. Keep Docker Desktop running.'
Write-Host 'Data location: .repolaunch\services (inside this project on F drive).'
& $pythonPath -c "import pymilvus,pymysql,langfuse,cryptography,httpx"
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing missing optional Python integrations...'
    & $pythonPath -m pip install --disable-pip-version-check -e '.[integrations]' 'PyMySQL[rsa]>=1,<2'
    if ($LASTEXITCODE -ne 0) { Write-Host 'Dependency installation failed.' -ForegroundColor Red; exit 1 }
}
& $pythonPath -m tools.integrations --timeout $Timeout
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Deployment or validation did not complete. Existing containers and data were preserved.' -ForegroundColor Red
    exit 1
}
Write-Host 'Running real database, vector search, trace and controlled repair verification...'
& $pythonPath -m tools.verify_full_stack
$verificationCode = $LASTEXITCODE
if ($verificationCode -ne 0) {
    Write-Host 'Some acceptance checks failed. Report: .repolaunch\full-stack-verification.json' -ForegroundColor Yellow
    Write-Host 'The web app will still open so you can inspect the service state.'
}
if (-not $NoApp) {
    $statusPath = Join-Path $PSScriptRoot '.repolaunch\services\status.json'
    $status = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $appPort = [int]$status.app_port
    Write-Host ('Starting RepoLaunch with validated integration settings on port {0}.' -f $appPort)
    Write-Host 'Keep this window open while using RepoLaunch.'
    & $pythonPath -m repolaunch --port $appPort --auto-port --open-browser
    exit $LASTEXITCODE
}

if ($NoApp) { exit $verificationCode }
