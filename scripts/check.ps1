$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DefaultPython = "O:\Code_dependency\python_envs\resource-co-debug-py311\Scripts\python.exe"

if (Test-Path -LiteralPath $DefaultPython) {
    $Python = $DefaultPython
} else {
    $Python = "python"
}

Push-Location $ProjectRoot
try {
    & $Python -m compileall app tests
    & $Python -m ruff check .
    & $Python -m pytest -q
} finally {
    Pop-Location
}

