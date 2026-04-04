$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$scriptPath = Join-Path $root "build_executable.py"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Build script not found at $scriptPath"
}

$embeddedPython = Join-Path $root ".python\\python.exe"
if (Test-Path -LiteralPath $embeddedPython) {
    & $embeddedPython $scriptPath
    exit $LASTEXITCODE
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $scriptPath
    exit $LASTEXITCODE
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    & python $scriptPath
    exit $LASTEXITCODE
}

throw "Python not found. Install Python 3.11+ or provide .python\\python.exe."
