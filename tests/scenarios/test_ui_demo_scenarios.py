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
# Page + pill sentence
# --------------------------------------------------------------------------- #
class TestPageAndPills:
    def test_index_renders_pill_sentence(self, client):
        r = client.get("/")
        assert r.status_code == 200
        # The Metabase-style fill-in-the-blank sentence (with pills) is present.
        assert "row dataset for a" in r.text.lower()
        assert 'class="pill' in r.text
        assert "healthz" not in r.text

    def test_index_includes_advertising_and_marketing(self, client):
        r = client.get("/")
        assert ">Advertising<" in r.text and ">Marketing<" in r.text

    def test_index_carries_the_worked_example(self, client):
        r = client.get("/")
        # The strong custom-prompt example is embedded in the describe textarea.
        assert "advertising platform" in r.text.lower()
        assert "roas" in r.text.lower()

    def test_healthz(self, client):
        assert client.get("/healthz").status_code == 200


# --------------------------------------------------------------------------- #
# Scenario A — compose with pills (the default utility path)
# --------------------------------------------------------------------------- #
class TestPillCompose:
    def test_pills_compose_prompt_and_preview(self, client):
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "campaign_id", "type": "string", "regex": "C[0-9]{5}"},
            {"name": "channel", "type": "string", "enum": ["search", "social"]},
            {"name": "spend_usd", "type": "number", "faker": "pyfloat"},
        ]))
        r = client.post("/preview", data={
            "business": "Advertising", "shape": "one-big-table", "year": "2025",
            "growth": "seasonal", "variation": "high", "granularity": "daily",
            "rows": "5000",
        })
        assert r.status_code == 200
        assert "campaign_id" in r.text
        # The download form carries the full requested row count, not a demo cap.
        assert 'value="5000"' in r.text

    def test_download_streams_full_count_uncapped(self, client):
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "id", "type": "integer", "faker": "random_int"},
            {"name": "tier", "type": "string", "enum": ["A", "B", "C"]},
        ]))
        client.post("/preview", data={"business": "Marketing", "rows": "100"})
        # Far above the old 1k demo cap — must stream the full set.
        r = client.post("/download", data={"rows": "25000", "format": "csv", "seed": "7"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        assert len(lines) == 25001  # header + 25,000 rows

    def test_download_json_full_count(self, client):
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "x", "type": "string", "faker": "word"}]))
        client.post("/preview", data={"business": "Fintech", "rows": "10"})
        r = client.post("/download", data={"rows": "3000", "format": "json", "seed": "1"})
        assert r.status_code == 200
        rows = json.loads(r.text)
        assert len(rows) == 3000

    def test_download_filename_is_header_safe(self, client):
        # A model-generated schema name with a quote/CRLF must not corrupt or
        # inject the content-disposition header — it's slugified.
        app = client.app
        bedrock = bedrock_schema_stub(schema_fields=[
            {"name": "x", "type": "string", "faker": "word"}])
        # Force a hostile schema name through the stub.
        bedrock.converse.return_value["output"]["message"]["content"][0][
            "toolUse"]["input"]["name"] = 'evil"\r\nSet-Cookie: x=1'
        _override(app, bedrock=bedrock)
        client.post("/preview", data={"business": "Retail", "rows": "10"})
        r = client.post("/download", data={"rows": "5", "format": "csv"})
        assert r.status_code == 200
        cd = r.headers["content-disposition"]
        # The CR/LF that would split the header is gone, and the quote that
        # would break out of filename="..." was slugified — so the only two
        # quotes are the wrapping pair. (Set-Cookie may survive as inert text.)
        assert "\r" not in cd and "\n" not in cd
        assert cd.count('"') == 2
        assert cd.startswith('attachment; filename="') and cd.endswith('.csv"')


# --------------------------------------------------------------------------- #
# Scenario B — describe your own dataset (custom prompt)
# --------------------------------------------------------------------------- #
class TestDescribeCustom:
    def test_custom_prompt_drives_schema(self, client):
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "marker_field", "type": "string", "faker": "word"}]))
        r = client.post("/preview", data={
            "prompt": "a marketplace with sellers, listings, and gross merchandise value",
            "business": "B2B SaaS",  # pill default also submitted; prompt must win
            "rows": "200",
        })
        assert r.status_code == 200
        assert "marker_field" in r.text


# --------------------------------------------------------------------------- #
# Scenario C — match a real (PII-bearing) document, output stays clean
# --------------------------------------------------------------------------- #
class TestMatchDocument:
    def test_upload_audits_pii_and_output_has_no_real_values(self, client, customer_pdf):
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(schema_fields=[
                {"name": "full_name", "type": "string", "faker": "name", "pii": True},
                {"name": "ssn", "type": "string", "faker": "ssn", "pii": True},
                {"name": "state", "type": "string", "enum": ["CA", "NY"]},
            ]),
            comprehend=comprehend_stub(entities_per_call=[[{"Type": "NAME"}, {"Type": "SSN"}]]),
        )
        with open(customer_pdf, "rb") as fh:
            r = client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "300"},
            )
        assert r.status_code == 200
        assert "pii" in r.text.lower()
        for real in CUSTOMER_PII_VALUES:
            assert real not in r.text, f"real PII leaked into UI preview: {real!r}"

    def test_download_full_dataset_is_pii_free(self, client, customer_pdf):
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(schema_fields=[
                {"name": "full_name", "type": "string", "faker": "name", "pii": True},
                {"name": "state", "type": "string", "enum": ["CA", "NY"]},
            ]),
            comprehend=comprehend_stub(entities_per_call=[[{"Type": "NAME"}]]),
        )
        with open(customer_pdf, "rb") as fh:
            client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "1000"},
            )
        r = client.post("/download", data={"rows": "1000", "format": "csv", "seed": "42"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        for real in CUSTOMER_PII_VALUES:
            assert real not in r.text


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
