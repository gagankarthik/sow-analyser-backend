<#
.SYNOPSIS
  Build the shared Lambda layer zip on Windows.

.DESCRIPTION
  Installs pip deps for linux/arm64 Python 3.12 and zips with the shared module.
  Output: build\shared-layer.zip

  Requires: Python 3.12 and pip on PATH.
  Note: cross-platform wheels (--platform manylinux2014_aarch64) require an
  internet connection. For purely local testing, omit the --platform flags.
#>

$Root      = $PSScriptRoot
$BuildDir  = "$Root\build"
$LayerDir  = "$BuildDir\shared-layer"
$PythonDir = "$LayerDir\python"

Write-Host "→ Cleaning $LayerDir"
if (Test-Path $LayerDir) { Remove-Item $LayerDir -Recurse -Force }
New-Item -ItemType Directory -Path $PythonDir -Force | Out-Null

Write-Host "→ Installing pip deps for linux/arm64 (Python 3.12)..."
pip install `
  -r "$Root\lambdas\shared\requirements.txt" `
  -t $PythonDir `
  --platform manylinux2014_aarch64 `
  --implementation cp `
  --python-version 3.12 `
  --only-binary=:all: `
  --upgrade `
  --quiet

Write-Host "→ Copying shared/ module..."
Copy-Item -Path "$Root\lambdas\shared\*" -Destination "$PythonDir\shared\" -Recurse -Force -Exclude "__pycache__"

Write-Host "→ Zipping → build\shared-layer.zip"
$ZipPath = "$BuildDir\shared-layer.zip"
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path "$LayerDir\*" -DestinationPath $ZipPath

$size = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
Write-Host "✓ Done: $ZipPath ($size MB)"
