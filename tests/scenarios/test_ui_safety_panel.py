# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""F4: the UI safety/attestation panel on the document-upload flow (ADR-0010/0011).

The panel is wired into the existing upload→preview→download flow (no new
screens). It surfaces: PII entities found, fields suppressed by the guard, the
verify verdict (✓ PASSED / ✗ FAILED + leaked fields), and a Download-attestation
link. On FAILED it states the output is NOT cleared for sharing and `/download`
is fail-closed (HTTP 409).

These run through the FastAPI TestClient with injected Bedrock/Comprehend stubs;
the browser-driven smoke is in tests/browser/.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from .conftest import UI_AVAILABLE, UI_SKIP_REASON, bedrock_schema_stub

pytestmark = pytest.mark.skipif(not UI_AVAILABLE, reason=UI_SKIP_REASON)

if UI_AVAILABLE:
    from fastapi.testclient import TestClient

    from pocsynth.ui.app import create_app, get_bedrock_client, get_comprehend_client


@pytest.fixture
def client():
    return TestClient(create_app())


def _override(app, *, bedrock=None, comprehend=None):
    if bedrock is not None:
        app.dependency_overrides[get_bedrock_client] = lambda: bedrock
    if comprehend is not None:
        app.dependency_overrides[get_comprehend_client] = lambda: comprehend


def _comprehend_whole_value(*markers):
    """A Comprehend stub returning a whole-value PII span for each marker present
    in the scanned text (so `_real_pii_from_scan` captures the full real value)."""
    client = MagicMock()

    def _detect(Text="", **_kw):
        ents = []
        for m in markers:
            idx = Text.find(m)
            if idx != -1:
                ents.append({"Type": "OTHER", "Score": 0.99,
                             "BeginOffset": idx, "EndOffset": idx + len(m)})
        return {"Entities": ents}

    client.detect_pii_entities.side_effect = _detect
    return client


class TestSafetyPanelPass:
    def test_clean_document_shows_no_leak_and_disclaimer(self, client, customer_pdf):
        # The model binds the PII field to faker.name (guard suppresses the real
        # value), so no real value reaches rows or schema → verify finds no leak.
        # The panel must NOT over-promise: no "PASSED"/"Cleared for sharing"; it
        # states "no leak detected" plus the best-effort disclaimer.
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(schema_fields=[
                {"name": "patient", "type": "string", "faker": "name", "pii": True},
                {"name": "state", "type": "string", "enum": ["CA", "NY"]},
            ]),
            comprehend=_comprehend_whole_value("Alice Hernandez", "555-22-7788"),
        )
        with open(customer_pdf, "rb") as fh:
            r = client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "300"},
            )
        assert r.status_code == 200
        assert 'id="safety"' in r.text
        assert "✓ NO LEAK DETECTED" in r.text
        assert "No real value detected" in r.text
        # No absolute safety claims.
        assert "Cleared for sharing" not in r.text
        assert "PASSED" not in r.text
        # The best-effort disclaimer is always shown.
        assert "best-effort" in r.text
        assert "Review the output" in r.text
        # The full real values must not appear anywhere in the panel/preview.
        assert "Alice Hernandez" not in r.text
        assert "555-22-7788" not in r.text

    def test_attestation_downloadable_after_pass(self, client, customer_pdf):
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(schema_fields=[
                {"name": "patient", "type": "string", "faker": "name", "pii": True}]),
            comprehend=_comprehend_whole_value("Alice Hernandez"),
        )
        with open(customer_pdf, "rb") as fh:
            client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "50"})
        r = client.get("/attestation")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        att = json.loads(r.text)
        assert att["verdict"] == "pass"
        assert att["tool_version"]
        # The attestation never carries a full real value.
        assert "Alice Hernandez" not in r.text


class TestSafetyPanelFailClosed:
    def _setup_leak(self, app):
        # The fragment hole: the guard strips the PII enum, but the model echoed
        # the real value into the field DESCRIPTION, which the guard doesn't touch.
        _override(
            app,
            bedrock=bedrock_schema_stub(schema_fields=[
                {"name": "patient", "type": "string", "faker": "name", "pii": True,
                 "description": "patient full name, e.g. Alice Hernandez"},
                {"name": "state", "type": "string", "enum": ["CA", "NY"]},
            ]),
            comprehend=_comprehend_whole_value("Alice Hernandez"),
        )

    def test_leak_shows_failed_panel(self, client, customer_pdf):
        app = client.app
        self._setup_leak(app)
        with open(customer_pdf, "rb") as fh:
            r = client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "100"})
        assert r.status_code == 200
        assert "✗ LEAK DETECTED" in r.text
        assert "Not cleared for sharing" in r.text
        assert "Download blocked" in r.text
        # The best-effort disclaimer shows on the fail panel too.
        assert "best-effort" in r.text
        # Even when reporting the leak, the full real value is masked.
        assert "Alice Hernandez" not in r.text

    def test_leak_names_the_source_field(self, client, customer_pdf):
        # The customer PDF has "Patient: Alice Hernandez", so the leaked value
        # maps to the `patient` field — the panel names it (vs a bare ****).
        app = client.app
        self._setup_leak(app)
        with open(customer_pdf, "rb") as fh:
            r = client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "100"})
        assert r.status_code == 200
        assert "patient:" in r.text  # field-qualified leak label

    def test_download_is_fail_closed(self, client, customer_pdf):
        app = client.app
        self._setup_leak(app)
        with open(customer_pdf, "rb") as fh:
            client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "100"})
        r = client.post("/download", data={"rows": "100", "format": "csv", "seed": "1"})
        assert r.status_code == 409
        assert "NOT cleared for sharing" in r.text


class TestSyntheticSeedNoPanel:
    def test_pills_path_has_no_safety_panel(self, client):
        # Synthetic-by-construction seed → no real source → no panel, no block.
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "x", "type": "string", "faker": "word"}]))
        r = client.post("/preview", data={"business": "Fintech", "rows": "10"})
        assert r.status_code == 200
        assert 'id="safety"' not in r.text
        # /attestation 404s when nothing was attested this session.
        r2 = client.get("/attestation")
        assert r2.status_code == 404

    def test_download_not_blocked_on_synthetic(self, client):
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "x", "type": "string", "faker": "word"}]))
        client.post("/preview", data={"business": "Retail", "rows": "10"})
        r = client.post("/download", data={"rows": "20", "format": "csv", "seed": "1"})
        assert r.status_code == 200


class TestSinglePassPiiFieldDerivation:
    """Field-level PII flags are derived from the ONE whole-document Comprehend
    scan (no second per-field pass). A field is flagged iff one of its parsed
    values contains a detected entity value."""

    def test_flags_only_fields_carrying_a_detected_value(self):
        from pocsynth.ui.app import _pii_field_names

        field_values = {
            "patient": ["Alice Hernandez"],
            "state": ["CA"],
            "ssn": ["555-22-7788"],
        }
        # Whole-doc scan flagged the name and the SSN, not the state code.
        detected = [
            {"Value": "Alice Hernandez"},
            {"Value": "555-22-7788"},
        ]
        assert _pii_field_names(field_values, detected) == {"patient", "ssn"}

    def test_short_detected_values_ignored(self):
        from pocsynth.ui.app import _pii_field_names

        # A sub-MIN_PII_VALUE_LEN detected value is a false-positive risk and is
        # not used to flag a field.
        assert _pii_field_names({"code": ["CA"]}, [{"Value": "CA"}]) == set()

    def test_no_detected_pii_flags_nothing(self):
        from pocsynth.ui.app import _pii_field_names

        assert _pii_field_names({"a": ["x" * 8], "b": ["y" * 8]}, []) == set()
