#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Build pocsynth binaries on Linux using PyInstaller.
# Produces both --onedir and --onefile. No codesigning on Linux.
#
# Gotchas:
#   - Must build on the same libc family (glibc vs musl) as the target runtime.
#     CI uses the standard python:3.11-slim image (glibc); Alpine users need
#     a separate musl build.
#   - Hidden imports: same --collect-all strategy as build-macos.sh.

set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> uv sync --group pyinstaller"
uv sync --group pyinstaller

echo "==> cleaning previous build artifacts"
rm -rf build dist

echo "==> pyinstaller --onedir"
uv run pyinstaller --onedir --name pocsynth \
    --collect-all boto3 --collect-all botocore --collect-all pymupdf \
    --paths src \
    src/pocsynth/cli.py

echo "==> onedir smoke: pocsynth --json models"
./dist/pocsynth/pocsynth --json models | python3 -c "import json,sys; json.load(sys.stdin)"
echo "onedir OK"

echo "==> moving onedir artifact out of the way"
mv dist/pocsynth dist/pocsynth-onedir

echo "==> pyinstaller --onefile"
uv run pyinstaller --onefile --name pocsynth --noconfirm \
    --collect-all boto3 --collect-all botocore --collect-all pymupdf \
    --paths src \
    src/pocsynth/cli.py

echo "==> onefile smoke: pocsynth --json models"
./dist/pocsynth --json models | python3 -c "import json,sys; json.load(sys.stdin)"
echo "onefile OK"
echo ""
echo "Artifacts:"
echo "  dist/pocsynth-onedir/pocsynth  (onedir)"
echo "  dist/pocsynth                  (onefile)"
