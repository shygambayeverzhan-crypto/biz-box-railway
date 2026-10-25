$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Python Launcher not found. Install Python 3.12 from https://www.python.org/downloads/windows/ and open a new PowerShell window.'
}
py -3.12 --version
if ($LASTEXITCODE -ne 0) {
    throw 'Python 3.12 not found. Install CPython 3.12, then run this script again.'
}

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv' }
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Could not update pip' }
& .\.venv\Scripts\python.exe -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Could not install project dependencies' }

if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
Write-Host 'Setup complete. Open .env, set BOT_TOKEN, then run .\start.ps1.'

