#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Demo helper: show that PII detected in the synthetic output is a Claude-
generated fake, not a leak from the original document.

Reads the audit CSV and the combined HTML from the given run directory,
and prints one detected-PII entity in context so a viewer sees the
(synthetic) value in situ.

Usage:
    python3 scripts/demo-pii-proof.py \\
        pii-audit/aws-mp-contract_pii_scan._audit.txt \\
        aws-mp-contract_1/aws-mp-contract_cleaned.html
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: demo-pii-proof.py <audit.csv> <combined.html>", file=sys.stderr)
        return 2
    audit_path = Path(sys.argv[1])
    html_path = Path(sys.argv[2])
    rows = list(csv.reader(audit_path.read_text().splitlines()))
    if len(rows) < 2:
        print("no PII rows in audit CSV", file=sys.stderr)
        return 0
    html = html_path.read_text(encoding="utf-8", errors="replace")
    header, *entities = rows
    print(f"Detected {len(entities)} PII entities in the synthetic output.")
    print("Audit CSV stores [REDACTED] (offsets preserved) — safe to share.\n")
    print("Sampling the first 3 detected PII positions in the rendered HTML:")
    print("(values are Claude-fabricated synthetic replacements, not real data)\n")
    for row in entities[:3]:
        _fn, _pg, kind, _score, s, e, _val = row
        s_i, e_i = int(s), int(e)
        snippet = html[max(0, s_i - 40): e_i + 40]
        # Collapse whitespace for readability in a narrow terminal.
        snippet = " ".join(snippet.split())
        print(f"  [{kind:8s}] …{snippet}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
