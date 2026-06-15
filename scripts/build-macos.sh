#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Build pocsynth binaries on macOS using PyInstaller.
# Produces both --onedir (dist/pocsynth/) for debugging and --onefile (dist/pocsynth)
# for release. Uses ad-hoc codesigning (--codesign-identity="-") which is enough to
# survive Gatekeeper's launch check on the local machine but is not notarized.
#
# Gotchas:
#   - Hidden imports are handled via --collect-all for boto3, botocore, and pymupdf.
#     If a frozen binary fails with ModuleNotFoundError, commit the fix into
#     pocsynth.spec (once it exists) rather than extending this script.
#   - Onefile binaries unpack to a temp dir on first run; first invocation is
#     noticeably slower than onedir.
#   - macOS quarantine: if you distribute the binary via download, recipients
#     may need `xattr -d com.apple.quarantine <binary>`. Notarization is future work.

set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> uv sync --group pyinstaller"
uv sync --group pyinstaller

echo "==> cleaning previous build artifacts"
rm -rf build dist

echo "==> pyinstaller --onedir"
# Note: we deliberately don't pass --codesign-identity. PyInstaller's default
# behavior on macOS produces a working onedir without the Team ID mismatch
# that explicit ad-hoc signing can cause between the loader and bundled dylibs.
# Users downloading the binary may still need `xattr -d com.apple.quarantine`.
uv run pyinstaller --onedir --name pocsynth \
    --collect-all boto3 --collect-all botocore --collect-all pymupdf \
    --paths src \
    src/pocsynth/cli.py

echo "==> onedir smoke: pocsynth --json models"
./dist/pocsynth/pocsynth --json models | python3 -c "import json,sys; json.load(sys.stdin)"
echo "onedir OK"

echo "==> moving onedir artifact out of the way"
# PyInstaller refuses to write dist/pocsynth when dist/pocsynth/ exists.
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
echo "  dist/pocsynth-onedir/pocsynth  (onedir — faster startup, easier to debug)"
echo "  dist/pocsynth                  (onefile — single binary, slower startup)"
