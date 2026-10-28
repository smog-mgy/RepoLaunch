param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& $Python -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
if ($LASTEXITCODE -ne 0) { throw '请使用 -Python 参数指定 Python 3.11 或更高版本。' }
& $Python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw '创建独立环境失败。' }
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '安装依赖失败，请检查网络。' }
Write-Host '准备完成。执行 .\start.ps1 后打开 http://127.0.0.1:8787'
