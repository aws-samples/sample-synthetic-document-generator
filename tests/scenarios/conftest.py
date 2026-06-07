# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared fixtures + stubs for the SA demo-data scenario tests.

These scenarios are written as executable acceptance specs for three realistic
ways a Solutions Architect produces synthetic data for a customer demo:

  S1  Customer-run, customer-data-seeded:
      the customer runs the tool themselves on a REAL document containing their
      own PII (so the data never leaves their account). The pipeline must audit
      that PII and ensure NONE of the real values survive into the synthetic
      dataset that gets shared back.

  S2  SA-run, public-data-seeded:
      the SA seeds generation from a PUBLIC (non-sensitive) sample document and
      builds a believable demo dataset. No customer PII is ever involved.

  S3  SA-run, prompt-seeded (no document):
      the SA has no document at all and describes the customer's business in
      natural language; the schema is inferred from the prompt.

All three are driven WITHOUT touching AWS: the Bedrock + Comprehend clients are
injected as MagicMock/Stubber, mirroring tests/unit/test_run_conversion.py.

NOTE (forward-looking): these tests target the structured-data pipeline modules
described in docs/plan/structured-data-support.md (extract / schema / generate /
test, the presets package, and the FastAPI+HTMX UI). They are committed alongside
the plan as the acceptance bar Slice 1/2/4 must clear, and will be skipped until
the corresponding modules land (see the importorskip guard below).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import fitz
import pytest

# Whether the structured-data pipeline has landed yet. Test MODULES use this in a
# module-level `pytestmark = pytest.mark.skipif(...)` so the suite skips cleanly
# until implementation exists, then flips to enforced. (Kept out of conftest's
# module body: an importorskip here would error collection rather than skip.)
PIPELINE_AVAILABLE = importlib.util.find_spec("pocsynth.generate") is not None
UI_AVAILABLE = (
    importlib.util.find_spec("fastapi") is not None
    and importlib.util.find_spec("pocsynth.ui.app") is not None
)
PIPELINE_SKIP_REASON = (
    "structured-data pipeline not implemented yet (see docs/plan/structured-data-support.md)"
)
UI_SKIP_REASON = "demo UI / [ui] extra not present (see docs/adr/0009-demo-ui.md)"


# --------------------------------------------------------------------------- #
# Seed documents
# --------------------------------------------------------------------------- #

# Real customer values that MUST NOT appear in any synthetic output. The S1
# assertions grep generated rows + the inferred schema for every one of these.
CUSTOMER_PII_VALUES = [
    "Alice Hernandez",
    "555-22-7788",            # SSN
    "alice.hernandez@acme.example",
    "MRN-009132",
]


def _make_pdf(tmp_path: Path, lines: list[str], name: str) -> Path:
    """Build a tiny single-page PDF whose text layer is `lines`."""
    doc = fitz.open()
    page = doc.new_page()
    y = 96
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 22
    pdf_path = tmp_path / name
    doc.save(pdf_path)
    doc.close()
    return pdf_path


@pytest.fixture
def customer_pdf(tmp_path: Path) -> Path:
    """A REAL intake form carrying customer PII (Scenario 1)."""
    return _make_pdf(
        tmp_path,
        [
            "Patient: Alice Hernandez",
            "SSN: 555-22-7788",
            "Email: alice.hernandez@acme.example",
            "MRN: MRN-009132   State: CA   Plan: Gold",
            "Patient: Bob Tran   SSN: 555-22-9001   State: NY   Plan: Silver",
            "Patient: Cara Liu   SSN: 555-22-3322   State: CA   Plan: Gold",
        ],
        "customer_intake.pdf",
    )


@pytest.fixture
def public_pdf(tmp_path: Path) -> Path:
    """A PUBLIC, non-sensitive product catalog the SA may freely seed from (S2)."""
    return _make_pdf(
        tmp_path,
        [
            "Product Catalog (public sample)",
            "SKU: SKU-100  Category: Widget  Price: 19.99  Region: US",
            "SKU: SKU-101  Category: Gadget  Price: 49.50  Region: EU",
            "SKU: SKU-102  Category: Widget  Price: 12.00  Region: US",
        ],
        "public_catalog.pdf",
    )


# --------------------------------------------------------------------------- #
# AWS client stubs (no network)
# --------------------------------------------------------------------------- #

def bedrock_extract_stub(records: list[dict], *, mode: str = "discovery") -> MagicMock:
    """A Bedrock client whose `converse` returns a forced-toolUse extract payload.

    Mirrors the ADR-0002 contract. In discovery mode it derives `observe_fields`
    `value_counts` from the sample records; in conform mode it returns the
    records under `extract_records`.
    """
    if mode == "conform":
        payload = {"records": records}
        tool_name = "extract_records"
    else:
        counts: dict[str, dict] = {}
        for rec in records:
            for k, v in rec.items():
                counts.setdefault(k, {})
                counts[k][str(v)] = counts[k].get(str(v), 0) + 1
        payload = {"fields": [
            {"name": k, "type_hint": "string", "value_counts": vc}
            for k, vc in counts.items()
        ]}
        tool_name = "observe_fields"

    client = MagicMock()
    client.converse.return_value = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"name": tool_name, "input": payload}}],
            }
        },
        "usage": {"inputTokens": 120, "outputTokens": 40, "totalTokens": 160},
        "stopReason": "tool_use",
    }
    return client


def bedrock_schema_stub(schema_fields: list[dict]) -> MagicMock:
    """A Bedrock client whose `converse` returns a forced `emit_schema` toolUse.

    Used by `schema --infer` and `schema --from-prompt` (same tool, ADR-0008).
    """
    client = MagicMock()
    client.converse.return_value = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"name": "emit_schema",
                                 "input": {"schema": 1, "name": "demo", "fields": schema_fields}}}
                ],
            }
        },
        "usage": {"inputTokens": 200, "outputTokens": 90, "totalTokens": 290},
        "stopReason": "tool_use",
    }
    return client


def comprehend_stub(entities_per_call=None, *, pii_markers=None) -> MagicMock:
    """A content-aware Comprehend client (no real network).

    `detect_pii_entities(Text=...)` returns a PII entity (with offsets/score the
    audit CSV needs) whenever the scanned text contains any of `pii_markers`
    (defaults to the known customer PII values). This is order-independent — it
    works for both the whole-document audit scan and the per-field PII probes,
    so a field carrying a real PII value gets flagged and a clean field does not.

    `entities_per_call` is accepted for backwards-compatible call sites: a
    non-empty first element means "this document contains PII" and falls back to
    the default markers.
    """
    markers = list(pii_markers) if pii_markers is not None else list(CUSTOMER_PII_VALUES)
    if entities_per_call and not entities_per_call[0]:
        # Caller explicitly modeled a clean document (e.g. public data).
        markers = []

    def _detect(Text: str = "", **_kw):
        if any(m in Text for m in markers):
            return {"Entities": [
                {"Type": "OTHER", "Score": 0.99, "BeginOffset": 0, "EndOffset": 3}
            ]}
        return {"Entities": []}

    client = MagicMock()
    client.detect_pii_entities.side_effect = _detect
    return client
