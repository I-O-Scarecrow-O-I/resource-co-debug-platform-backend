$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = "O:\Code_dependency\python_envs\resource-co-debug-py311\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Expected project Python environment was not found: $Python"
}

Push-Location $ProjectRoot
try {
    & $Python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
} finally {
    Pop-Location
}
