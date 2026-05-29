# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Build pocsynth.exe on Windows using PyInstaller. Produces onedir + onefile.
# Not codesigned by default; unsigned binaries trigger SmartScreen warnings,
# especially for executables downloaded from the internet. Signing is future
# work and requires a code-signing certificate.
#
# Gotchas:
#   - Hidden imports: same --collect-all approach as the other platforms.
#   - SmartScreen: on first run from a non-local location, Windows may block
#     the binary. Signing resolves this for wide distribution.

$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "==> uv sync --group pyinstaller"
uv sync --group pyinstaller

Write-Host "==> cleaning previous build artifacts"
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }

Write-Host "==> pyinstaller --onedir"
uv run pyinstaller --onedir --name pocsynth `
    --collect-all boto3 --collect-all botocore --collect-all pymupdf `
    --paths src `
    src\pocsynth\cli.py

Write-Host "==> onedir smoke: pocsynth --json models"
.\dist\pocsynth\pocsynth.exe --json models | python -c "import json,sys; json.load(sys.stdin)"
Write-Host "onedir OK"

Write-Host "==> moving onedir artifact out of the way"
Rename-Item dist\pocsynth dist\pocsynth-onedir

Write-Host "==> pyinstaller --onefile"
uv run pyinstaller --onefile --name pocsynth --noconfirm `
    --collect-all boto3 --collect-all botocore --collect-all pymupdf `
    --paths src `
    src\pocsynth\cli.py

Write-Host "==> onefile smoke: pocsynth.exe --json models"
.\dist\pocsynth.exe --json models | python -c "import json,sys; json.load(sys.stdin)"
Write-Host "onefile OK"
