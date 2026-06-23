#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate tiny deterministic fixtures for the skill evals.

Writes:
  skills/pocsynth/evals/fixtures/contract.pdf          (2 pages)
  skills/pocsynth/evals/fixtures/120-page-contract.pdf (120 pages)
  skills/pocsynth/evals/fixtures/sample.pdf            (1 page)
  skills/pocsynth/evals/fixtures/output.html           (synthetic PII test text)
"""

from __future__ import annotations

from pathlib import Path

import fitz

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "skills" / "pocsynth" / "evals" / "fixtures"


def _write_pdf(path: Path, pages: int, title: str) -> None:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 96), title, fontsize=18)
        page.insert_text(
            (72, 130),
            f"Page {i + 1} of {pages}. This is synthetic placeholder text.",
            fontsize=11,
        )
        page.insert_text(
            (72, 150),
            "No real party, no real terms, no real PII.",
            fontsize=11,
        )
    doc.save(path)
    doc.close()


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)

    _write_pdf(FIXTURES / "contract.pdf", pages=2, title="Synthetic Contract")
    _write_pdf(
        FIXTURES / "120-page-contract.pdf",
        pages=120,
        title="Synthetic 120-Page Document",
    )
    _write_pdf(FIXTURES / "sample.pdf", pages=1, title="Synthetic Sample")

    html = """<!DOCTYPE html>
<html><body>
<h1>Synthetic test document</h1>
<p>This file is used only for skill evals. All PII-looking tokens below
are synthetic reserved values.</p>
<p>Contact: Alex Smith, alex@example.com, 206-555-0123.</p>
<p>Address: 1234 Main St, Anytown, WA 98101.</p>
</body></html>
"""
    (FIXTURES / "output.html").write_text(html, encoding="utf-8")

    sizes = {p.name: p.stat().st_size for p in sorted(FIXTURES.iterdir())}
    for name, size in sizes.items():
        print(f"{name:40s} {size:>10,} bytes")


if __name__ == "__main__":
    main()
