$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$pythonExe = Join-Path $root ".python\\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Embedded Python not found at $pythonExe"
}

$distDir = Join-Path $root "dist"
if (-not (Test-Path -LiteralPath $distDir)) {
    New-Item -ItemType Directory -Path $distDir | Out-Null
}

$prefix = "SmartInventory_v"
$existing = Get-ChildItem -Path $distDir -Directory -ErrorAction SilentlyContinue |
    ForEach-Object {
        if ($_.Name -match "^${prefix}(\d{3})$") {
            [int]$Matches[1]
        }
    }

$nextVersion = if ($existing) { [int](($existing | Measure-Object -Maximum).Maximum + 1) } else { 1 }
$buildName = "{0}{1:D3}" -f $prefix, $nextVersion
$tempSpec = Join-Path $root "$buildName.spec"

Write-Host "Building $buildName ..."

& $pythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name $buildName `
    --add-data "config;config" `
    main.py

if (Test-Path -LiteralPath $tempSpec) {
    Remove-Item -LiteralPath $tempSpec -Force
}

$exePath = Join-Path $root "dist\\$buildName\\$buildName.exe"
Write-Host ""
Write-Host "Build complete:"
Write-Host "  $exePath"
