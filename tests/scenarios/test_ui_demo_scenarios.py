# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The same three SA demo-data scenarios, driven through the FastAPI + HTMX UI.

These exercise the demo UI (docs/adr/0009-demo-ui.md) as a thin layer over the
core: the endpoints must call the same pipeline functions and surface the same
guarantees as the CLI — crucially, the Scenario-1 PII non-leak guarantee must
hold through the web path too.

The UI's paid Bedrock/Comprehend clients are injected via FastAPI dependency
overrides (the app exposes `get_bedrock_client` / `get_comprehend_client`
dependencies precisely so tests and the real app can swap them). Preset/preview
paths that don't spend tokens run fully offline.

Skipped until the `[ui]` extra + module exist (see importorskip guards).
"""

from __future__ import annotations

import json

import pytest

from .conftest import (
    CUSTOMER_PII_VALUES,
    UI_AVAILABLE,
    UI_SKIP_REASON,
    bedrock_schema_stub,
    comprehend_stub,
)

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


# --------------------------------------------------------------------------- #
# Page + presets load (offline)
# --------------------------------------------------------------------------- #
class TestPageAndPresets:
    def test_index_renders_sentence_builder(self, client):
        r = client.get("/")
        assert r.status_code == 200
        # The Metabase-style fill-in-the-blank sentence is present.
        assert "row dataset" in r.text.lower()
        assert "healthz" not in r.text

    def test_presets_endpoint_lists_bundled_schemas(self, client):
        r = client.get("/presets")
        assert r.status_code == 200
        # at least one pill option comes back
        assert r.text.strip()

    def test_healthz(self, client):
        assert client.get("/healthz").status_code == 200


# --------------------------------------------------------------------------- #
# Scenario 1 — customer uploads their own PII document to the local UI
# --------------------------------------------------------------------------- #
class TestScenario1UICustomerDataSeeded:
    def test_preview_audits_pii_and_output_has_no_real_values(
        self, client, customer_pdf
    ):
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(
                schema_fields=[
                    {"name": "full_name", "type": "string", "faker": "name", "pii": True},
                    {"name": "ssn", "type": "string", "faker": "ssn", "pii": True},
                    {"name": "state", "type": "string", "enum": ["CA", "NY"]},
                ]
            ),
            comprehend=comprehend_stub(entities_per_call=[[{"Type": "NAME"}, {"Type": "SSN"}]]),
        )
        # Customer uploads the real document as the generation seed.
        with open(customer_pdf, "rb") as fh:
            r = client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "10"},
            )
        assert r.status_code == 200
        # The preview pane reports the PII audit ran...
        assert "pii" in r.text.lower()
        # ...and none of the real customer values appear in the rendered 10-row preview.
        for real in CUSTOMER_PII_VALUES:
            assert real not in r.text, f"real PII leaked into UI preview: {real!r}"

    def test_download_full_dataset_is_pii_free(self, client, customer_pdf):
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(
                schema_fields=[
                    {"name": "full_name", "type": "string", "faker": "name", "pii": True},
                    {"name": "state", "type": "string", "enum": ["CA", "NY"]},
                ]
            ),
            comprehend=comprehend_stub(entities_per_call=[[{"Type": "NAME"}]]),
        )
        with open(customer_pdf, "rb") as fh:
            client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "10"},
            )
        # Reuse the previewed schema (session) to download the full set — no second model call.
        r = client.post("/download", data={"rows": "300", "format": "csv", "seed": "42"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        body = r.text
        for real in CUSTOMER_PII_VALUES:
            assert real not in body


# --------------------------------------------------------------------------- #
# Scenario 2 — SA seeds the UI from public data
# --------------------------------------------------------------------------- #
class TestScenario2UIPublicDataSeeded:
    def test_public_seed_preview_then_download(self, client, public_pdf):
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(
                schema_fields=[
                    {"name": "sku", "type": "string", "regex": "SKU-[0-9]{3}"},
                    {"name": "category", "type": "string", "enum": ["Widget", "Gadget"]},
                ]
            ),
            comprehend=comprehend_stub(entities_per_call=[[]]),
        )
        with open(public_pdf, "rb") as fh:
            r = client.post(
                "/preview",
                files={"seed_document": ("public_catalog.pdf", fh, "application/pdf")},
                data={"rows": "10"},
            )
        assert r.status_code == 200
        r2 = client.post("/download", data={"rows": "1000", "format": "json", "seed": "5"})
        assert r2.status_code == 200
        rows = json.loads(r2.text)
        assert len(rows) == 1000

    def test_preset_preview_is_offline(self, client):
        """Preset path must not require any injected AWS client."""
        presets = client.get("/presets")
        assert presets.status_code == 200
        # No bedrock/comprehend override registered → must still work.
        r = client.post("/preview", data={"preset": "b2b_saas", "rows": "10"})
        assert r.status_code == 200
        assert "no data" not in r.text.lower()


# --------------------------------------------------------------------------- #
# Scenario 3 — SA describes the business in the UI text box (no document)
# --------------------------------------------------------------------------- #
class TestScenario3UIPromptSeeded:
    def test_describe_business_preview_and_cost_surfaced(self, client):
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(
                schema_fields=[
                    {"name": "account_name", "type": "string", "faker": "company"},
                    {"name": "plan", "type": "string",
                     "enum": ["Starter", "Pro", "Enterprise"]},
                ]
            ),
        )
        r = client.post(
            "/preview",
            data={"prompt": "A B2B SaaS company's customer accounts with plan tier",
                  "rows": "10"},
        )
        assert r.status_code == 200
        # The paid path must surface a cost figure in the preview (ADR-0007 gate).
        assert "$" in r.text or "cost" in r.text.lower()

    def test_prompt_path_requires_explicit_submit_not_on_load(self, client):
        """GET / must not trigger a paid call; only POST /preview does."""
        app = client.app
        bedrock = bedrock_schema_stub(schema_fields=[{"name": "x", "type": "string",
                                                      "faker": "word"}])
        _override(app, bedrock=bedrock)
        client.get("/")
        bedrock.converse.assert_not_called()
