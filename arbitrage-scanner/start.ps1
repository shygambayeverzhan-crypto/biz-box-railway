$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    throw 'Environment not installed. Run .\setup.ps1 first.'
}
if (-not (Test-Path -LiteralPath '.env')) {
    throw '.env not found. Run .\setup.ps1 and set BOT_TOKEN.'
}
if (-not (Select-String -LiteralPath '.env' -Pattern '^BOT_TOKEN=.+$' -Quiet)) {
    throw 'BOT_TOKEN is empty. Set it in .env; do not paste the token into chat.'
}

& .\.venv\Scripts\python.exe -m arbscanner
exit $LASTEXITCODE

