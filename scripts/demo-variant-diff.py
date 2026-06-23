#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Demo helper: show the first few rendered lines from each num-docs variant.

Reads variants at `<stem>_1/<stem>_cleaned.html`, `<stem>_2/...`, etc. and
prints a short snippet from each so a viewer can see the rewrites differ.

Usage:
    python3 scripts/demo-variant-diff.py <filename_stem> [num_docs]
    # e.g.
    python3 scripts/demo-variant-diff.py aws-mp-contract 3
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: demo-variant-diff.py <stem> [num_docs]", file=sys.stderr)
        return 2
    stem = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) >= 3 else 3
    # Show (a) document size, (b) an md5 snippet of each variant so a viewer
    # sees at a glance that the files differ even when the first paragraphs
    # look similar, (c) one sampled paragraph far enough into the doc that
    # synthetic rewrites diverge visibly.
    from hashlib import md5

    for i in range(1, n + 1):
        html = Path(f"{stem}_{i}") / f"{stem}_cleaned.html"
        if not html.exists():
            print(f"--- variant {i}: {html} missing ---")
            continue
        raw = html.read_bytes()
        digest = md5(raw).hexdigest()[:12]
        print(f"--- variant {i}: {len(raw):>6,} bytes  md5={digest} ---")

        out = subprocess.run(
            ["elinks", "-dump", "-dump-width", "100", str(html)],
            capture_output=True, text=True, check=False,
        ).stdout.splitlines()
        # Skip the shared "STANDARD AGREEMENT" header and print lines 20-26
        # where the synthetic rewrites actually diverge.
        for line in out[20:26]:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
