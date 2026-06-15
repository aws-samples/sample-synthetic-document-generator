# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure-pricing estimate tests for the paid stages (ADR-0007). No AWS."""

from __future__ import annotations

from pathlib import Path

import fitz

from pocsynth.pricing import (
    estimate_convert_cost,
    estimate_extract_cost,
    estimate_schema_infer_cost,
    load_pricing,
)


def _make_pdf(tmp_path: Path, pages: int = 2) -> Path:
    doc = fitz.open()
    for _ in range(pages):
        p = doc.new_page()
        p.insert_text((72, 96), "Dense intake content " * 40, fontsize=10)
    path = tmp_path / "in.pdf"
    doc.save(path)
    doc.close()
    return path


def test_extract_envelope_shape_and_cheaper_than_convert(tmp_path):
    pricing = load_pricing()
    pdf = _make_pdf(tmp_path, pages=2)
    ext = estimate_extract_cost(pdf, "sonnet", pricing, region="us-east-1")
    conv = estimate_convert_cost(pdf, "sonnet", pricing, region="us-east-1")
    assert ext["target"] == "extract"
    assert set(ext) >= {"bedrock", "comprehend", "total_cost_usd", "estimate", "warnings"}
    # Lower output ratio => extract's bedrock output cost < convert's.
    assert ext["bedrock"]["output_cost_usd"] <= conv["bedrock"]["output_cost_usd"]


def test_extract_no_pii_skips_comprehend(tmp_path):
    pricing = load_pricing()
    pdf = _make_pdf(tmp_path, pages=1)
    ext = estimate_extract_cost(pdf, "sonnet", pricing, pii_audit=False, region="us-east-1")
    assert ext["comprehend"] is None


def test_schema_infer_offline_small(tmp_path):
    pricing = load_pricing()
    sample = tmp_path / "sample.json"
    sample.write_text('{"schema":1,"fields":[{"name":"a","value_counts":{"x":1}}]}')
    est = estimate_schema_infer_cost(sample, "sonnet", pricing, region="us-east-1")
    assert est["target"] == "schema"
    assert est["comprehend"] is None
    assert est["total_cost_usd"] >= 0
