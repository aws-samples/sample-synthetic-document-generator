# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""End-to-end scenarios inspired by anonymized real-world workflows.

Each class is a GENERIC industry archetype distilled from common Solutions
Architect document/structured-data workflows — no customer names, no real data.
They exercise pocsynth end-to-end across BOTH modalities:

  Unstructured (documents):
    U1  Multilingual lease abstraction        — convert + PII audit
    U2  Insurance inbound-mail intake          — extract (conform) + PII flags
    U3  Grant-application metadata extraction  — extract (discovery) -> schema -> generate -> test

  Structured (tabular):
    S1  Utility meter / billing analytics      — schema -> generate (ranged) -> test
    S2  Loyalty / POS transaction generation   — weighted enums hold their skew
    S3  Application-telemetry compliance corpus — prompt -> schema -> generate -> test
    S4  Clickstream analytics instrumentation  — large deterministic generation

All paid stages use injected stub clients (no AWS). The keystone invariant for
every structured path: generated rows VALIDATE against the schema they came from.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from .conftest import (
    PIPELINE_AVAILABLE,
    PIPELINE_SKIP_REASON,
    _make_pdf,
    bedrock_extract_stub,
    bedrock_schema_stub,
    comprehend_stub,
)

pytestmark = pytest.mark.skipif(not PIPELINE_AVAILABLE, reason=PIPELINE_SKIP_REASON)

if PIPELINE_AVAILABLE:
    import boto3
    from botocore.stub import Stubber

    from pocsynth.core import ConversionConfig, run_conversion
    from pocsynth.extract import ExtractConfig, run_extraction
    from pocsynth.generate import GenerateConfig, run_generation
    from pocsynth.schemagen import SchemaConfig, run_schema
    from pocsynth.validate import ValidateConfig, run_validation


def _read_csv(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _convert_bedrock_stub(text: str):
    """A Bedrock client whose converse returns rendered document text (convert path)."""
    from unittest.mock import MagicMock
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": 30, "outputTokens": 20, "totalTokens": 50},
        "stopReason": "end_turn",
    }
    return client


def _zero_pii_comprehend():
    """Stubber-backed comprehend returning no entities (clean / synthetic doc)."""
    client = boto3.client("comprehend", region_name="us-east-1")
    stubber = Stubber(client)
    for _ in range(20):
        stubber.add_response("detect_pii_entities", {"Entities": []})
    stubber.activate()
    return client


# ===========================================================================
# UNSTRUCTURED (document) archetypes
# ===========================================================================
class TestU1_LeaseAbstraction:
    """Commercial real estate: abstract key terms from a (synthetic) lease and
    produce a shareable synthetic rendering with PII audited. The shared output
    must carry none of the original tenant identifiers."""

    TENANT_PII = ["Margaret O'Donnell", "tenant@northwind.example", "555-44-1212"]

    def test_convert_synthetic_render_audits_pii(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        lease = _make_pdf(
            tmp_path,
            [
                "COMMERCIAL LEASE AGREEMENT",
                "Tenant: Margaret O'Donnell",
                "Contact: tenant@northwind.example   Tax ID: 555-44-1212",
                "Premises: Suite 1200   Monthly Rent: $14,500   Term: 60 months",
                "Commencement: 2025-03-01   Renewal: two 5-year options",
            ],
            "lease.pdf",
        )
        # The model returns a SYNTHETIC rendering — fabricated tenant, same shape.
        bedrock = _convert_bedrock_stub(
            "<h1>Commercial Lease Agreement</h1>"
            "<p>Tenant: Jordan Vance</p>"
            "<p>Premises: Suite 1200 — Monthly Rent: $14,500 — Term: 60 months</p>"
        )
        comprehend = _zero_pii_comprehend()
        res = run_conversion(ConversionConfig(
            pdf_url=str(lease), export_format="html", synthetic=True,
            pii_audit=True, redact_values=True,
            bedrock_client=bedrock, comprehend_client=comprehend,
            output_dir=str(tmp_path / "out"),
        ))
        assert res["input"]["mode"] == "synthetic"
        assert res["pii_audit"]["enabled"] is True
        combined = Path(res["output"]["combined_path"]).read_text(encoding="utf-8")
        # The shareable synthetic rendering carries none of the real tenant PII.
        for real in self.TENANT_PII:
            assert real not in combined, f"real lease PII leaked: {real!r}"
        # Non-PII business numerics are preserved by the synthetic rewrite.
        assert "14,500" in combined


class TestU2_InsuranceMailIntake:
    """Insurance: classify + extract key fields from inbound mail (claims,
    medical, underwriting) into structured records, flagging PII fields so they
    never become enums downstream."""

    def test_extract_conform_records_and_pii_flags(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mail = _make_pdf(
            tmp_path,
            [
                "CLAIM FORM",
                "Claimant: Harold Webb   Policy: POL-88231   SSN: 555-66-7788",
                "Claim Type: Auto   Amount: 4200.00   Status: Open",
            ],
            "claim.pdf",
        )
        schema = {"schema": 1, "name": "claim", "fields": [
            {"name": "claimant", "type": "string"},
            {"name": "policy", "type": "string"},
            {"name": "claim_type", "type": "string"},
            {"name": "amount", "type": "number"},
            {"name": "status", "type": "string"},
        ]}
        bedrock = bedrock_extract_stub(
            records=[{"claimant": "Harold Webb", "policy": "POL-88231",
                      "claim_type": "Auto", "amount": "4200.00", "status": "Open"}],
            mode="conform",
        )
        comprehend = comprehend_stub(pii_markers=["Harold Webb", "555-66-7788"])
        ext = run_extraction(ExtractConfig(
            pdf_url=str(mail), schema=schema, pii_audit=True,
            output_dir=str(tmp_path / "out"),
            bedrock_client=bedrock, comprehend_client=comprehend,
        ))
        assert ext["input"]["mode"] == "conform"
        assert ext["output"]["records_extracted"] == 1
        assert "claimant" in ext["pii_audit"]["pii_fields"]


class TestU3_GrantApplicationPipeline:
    """Government/research: extract proposal metadata from grant PDFs (discovery),
    infer a schema, then generate a synthetic triage corpus and validate it."""

    def test_discovery_to_generated_corpus(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        grant = _make_pdf(
            tmp_path,
            [
                "RESEARCH GRANT APPLICATION",
                "Title: Adaptive Materials   PI: Dr. Lena Fischer",
                "Budget: 480000   Duration Months: 36   Area: Materials Science",
            ],
            "grant.pdf",
        )
        # Discovery extraction observes the fields + value frequencies.
        bedrock_ext = bedrock_extract_stub(
            records=[
                {"area": "Materials Science", "duration_months": "36", "budget": "480000"},
                {"area": "Materials Science", "duration_months": "24", "budget": "250000"},
                {"area": "Computer Science", "duration_months": "36", "budget": "510000"},
            ],
            mode="discovery",
        )
        comprehend = comprehend_stub(pii_markers=[])  # public-style fields
        ext = run_extraction(ExtractConfig(
            pdf_url=str(grant), schema=None, pii_audit=True,
            output_dir=str(tmp_path / "out"),
            bedrock_client=bedrock_ext, comprehend_client=comprehend,
        ))
        assert ext["input"]["mode"] == "discovery"

        # Infer a generation-ready schema from the discovery sample.
        bedrock_schema = bedrock_schema_stub(schema_fields=[
            {"name": "area", "type": "string", "enum": ["Materials Science", "Computer Science"]},
            {"name": "duration_months", "type": "integer", "enum": ["24", "36"]},
            {"name": "budget", "type": "number", "faker": "pyfloat",
             "faker_args": {"min_value": 100000, "max_value": 800000, "right_digits": 0}},
        ])
        sch = run_schema(SchemaConfig(
            sample_path=ext["output"]["sample_path"], distribution="infer",
            output_dir=str(tmp_path / "out"), bedrock_client=bedrock_schema,
        ))
        schema = json.loads(Path(sch["output"]["schema_path"]).read_text())

        gen = run_generation(GenerateConfig(
            schema=schema, rows=100, seed=4, output_dir=str(tmp_path / "out")))
        val = run_validation(ValidateConfig(
            rows_path=gen["output"]["rows_path"], schema=schema))
        assert val["valid"] is True, val["violations"][:3]
        assert val["rows_checked"] == 100


# ===========================================================================
# STRUCTURED (tabular) archetypes
# ===========================================================================
class TestS1_MeterBillingAnalytics:
    """Utilities: generate a large synthetic meter-reading dataset with values in
    realistic ranges, then validate the whole set conforms (load-test feedstock)."""

    def test_ranged_meter_dataset_validates(self, tmp_path):
        schema = {"schema": 1, "name": "meter_reading", "fields": [
            {"name": "meter_id", "type": "string", "regex": "MTR-[0-9]{8}"},
            {"name": "usage_kwh", "type": "number", "faker": "pyfloat",
             "faker_args": {"min_value": 0, "max_value": 1000, "right_digits": 2}},
            {"name": "rate_plan", "type": "string", "enum": ["TOU", "Tiered", "Flat"],
             "weights": {"TOU": 0.5, "Tiered": 0.3, "Flat": 0.2}},
            {"name": "reading_date", "type": "date", "faker": "date_this_year"},
            {"name": "billed", "type": "boolean", "faker": "boolean"},
        ]}
        gen = run_generation(GenerateConfig(
            schema=schema, rows=5000, seed=11, export_format="csv",
            output_dir=str(tmp_path)))
        rows = _read_csv(gen["output"]["rows_path"])
        assert len(rows) == 5000
        for r in rows[:50]:
            assert r["meter_id"].startswith("MTR-") and len(r["meter_id"]) == 12
        val = run_validation(ValidateConfig(
            rows_path=gen["output"]["rows_path"], schema=schema))
        assert val["valid"] is True, val["violations"][:3]

    def test_meter_dataset_is_reproducible(self, tmp_path):
        schema = {"schema": 1, "name": "m", "fields": [
            {"name": "usage_kwh", "type": "number", "faker": "pyfloat"}]}
        a, b = tmp_path / "a", tmp_path / "b"
        run_generation(GenerateConfig(schema=schema, rows=200, seed=99, output_dir=str(a)))
        run_generation(GenerateConfig(schema=schema, rows=200, seed=99, output_dir=str(b)))
        assert (a / "rows.csv").read_bytes() == (b / "rows.csv").read_bytes()


class TestS2_LoyaltyTransactions:
    """Retail/loyalty: a weighted transaction-type distribution (purchase 80% /
    redemption 15% / chargeback 5%) must hold its skew at scale."""

    def test_weighted_transaction_mix_holds(self, tmp_path):
        schema = {"schema": 1, "name": "loyalty_txn", "fields": [
            {"name": "txn_id", "type": "string", "regex": "T[0-9]{10}"},
            {"name": "txn_type", "type": "string",
             "enum": ["purchase", "redemption", "chargeback"],
             "weights": {"purchase": 0.80, "redemption": 0.15, "chargeback": 0.05}},
            {"name": "points", "type": "integer", "faker": "random_int",
             "faker_args": {"min": 0, "max": 5000}},
        ]}
        gen = run_generation(GenerateConfig(
            schema=schema, rows=4000, seed=7, output_dir=str(tmp_path)))
        rows = _read_csv(gen["output"]["rows_path"])
        share = {t: sum(1 for r in rows if r["txn_type"] == t) / len(rows)
                 for t in ("purchase", "redemption", "chargeback")}
        assert share["purchase"] > 0.72           # dominant
        assert share["chargeback"] < 0.12          # rare
        assert share["purchase"] > share["redemption"] > share["chargeback"]
        val = run_validation(ValidateConfig(
            rows_path=gen["output"]["rows_path"], schema=schema))
        assert val["valid"] is True


class TestS3_TelemetryComplianceCorpus:
    """Enterprise software: describe a telemetry/audit dataset in natural language,
    infer a schema (paid, from-prompt), generate, and validate a compliance corpus."""

    def test_prompt_to_validated_telemetry(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bedrock = bedrock_schema_stub(schema_fields=[
            {"name": "trace_id", "type": "string", "regex": "[0-9a-f]{16}"},
            {"name": "service", "type": "string",
             "enum": ["auth", "billing", "gateway", "worker"]},
            {"name": "control_type", "type": "string",
             "enum": ["access", "encryption", "logging", "backup"]},
            {"name": "severity", "type": "string", "enum": ["low", "medium", "high"],
             "weights": {"low": 0.6, "medium": 0.3, "high": 0.1}},
            {"name": "event_time", "type": "datetime", "faker": "date_time_this_year"},
        ])
        sch = run_schema(SchemaConfig(
            prompt="application telemetry as audit events with compliance control "
                   "mappings: trace id, service, control type, severity, timestamp",
            distribution="synthetic", output_dir=str(tmp_path / "out"),
            bedrock_client=bedrock,
        ))
        assert sch["input"]["mode"] == "from_prompt"
        schema = json.loads(Path(sch["output"]["schema_path"]).read_text())
        gen = run_generation(GenerateConfig(
            schema=schema, rows=2000, seed=3, export_format="json",
            output_dir=str(tmp_path / "out")))
        rows = json.loads(Path(gen["output"]["rows_path"]).read_text())
        assert len(rows) == 2000
        val = run_validation(ValidateConfig(
            rows_path=gen["output"]["rows_path"], schema=schema))
        assert val["valid"] is True, val["violations"][:3]


class TestS4_ClickstreamInstrumentation:
    """Software/SaaS: a large deterministic clickstream dataset (sessions, events)
    generated offline and streamed-equivalent — feedstock for analytics testing."""

    def test_large_clickstream_generation_validates(self, tmp_path):
        schema = {"schema": 1, "name": "clickstream", "fields": [
            {"name": "session_id", "type": "string", "regex": "S[0-9]{12}"},
            {"name": "event_type", "type": "string",
             "enum": ["page_view", "click", "scroll", "submit"],
             "weights": {"page_view": 0.5, "click": 0.3, "scroll": 0.15, "submit": 0.05}},
            {"name": "device", "type": "string", "enum": ["desktop", "mobile", "tablet"]},
            {"name": "dwell_ms", "type": "integer", "faker": "random_int",
             "faker_args": {"min": 0, "max": 60000}},
        ]}
        gen = run_generation(GenerateConfig(
            schema=schema, rows=10000, seed=21, export_format="csv",
            output_dir=str(tmp_path)))
        assert gen["output"]["rows_written"] == 10000
        val = run_validation(ValidateConfig(
            rows_path=gen["output"]["rows_path"], schema=schema))
        assert val["valid"] is True, val["violations"][:3]


class TestS5_GovernanceAuditCorpus:
    """Internal governance/compliance: a periodic engagement-audit corpus whose
    distributions match a real-world audit report — heavily skewed resolver
    groups, a status enum, an age-in-days numeric range, and a regex ticket id.
    Inspired by a real-world governance audit (anonymized: generic regions, no people).

    This is the highest-fidelity structured archetype: it exercises non-uniform
    weighted enums at a realistic skew (one region owns ~75% of rows) AND a
    second weighted enum, then validates the whole corpus."""

    def test_skewed_governance_corpus_generates_and_validates(self, tmp_path):
        # Region mix mirrors an audit's 39/7/4/2 split across four regions.
        schema = {"schema": 1, "name": "engagement_audit", "fields": [
            {"name": "ticket_id", "type": "string", "regex": "[0-9a-f]{8}"},
            {"name": "region", "type": "string",
             "enum": ["region_a", "region_b", "region_c", "region_d"],
             "weights": {"region_a": 0.76, "region_b": 0.14,
                         "region_c": 0.08, "region_d": 0.02}},
            {"name": "status", "type": "string",
             "enum": ["work_in_progress", "assigned", "pending"],
             "weights": {"work_in_progress": 0.6, "assigned": 0.25, "pending": 0.15}},
            {"name": "age_days", "type": "integer", "faker": "random_int",
             "faker_args": {"min": 1, "max": 120}},
            {"name": "production_access", "type": "boolean", "faker": "boolean"},
        ]}
        gen = run_generation(GenerateConfig(
            schema=schema, rows=3000, seed=13, export_format="csv",
            output_dir=str(tmp_path)))
        rows = _read_csv(gen["output"]["rows_path"])
        assert len(rows) == 3000

        # The dominant region holds its ~76% skew; the rarest stays scarce.
        region_share = {r: sum(1 for x in rows if x["region"] == r) / len(rows)
                        for r in ("region_a", "region_b", "region_c", "region_d")}
        assert region_share["region_a"] > 0.68
        assert region_share["region_d"] < 0.06
        assert region_share["region_a"] > region_share["region_b"] > region_share["region_d"]

        # age_days respects the declared bounds.
        ages = [int(r["age_days"]) for r in rows]
        assert min(ages) >= 1 and max(ages) <= 120

        val = run_validation(ValidateConfig(
            rows_path=gen["output"]["rows_path"], schema=schema))
        assert val["valid"] is True, val["violations"][:3]

    def test_multilabel_flag_field_via_regex(self, tmp_path):
        # The audit's "Key Flags" column is a comma-separated multi-label set.
        # A regex field models a constrained 1-3 flag combination deterministically.
        schema = {"schema": 1, "name": "flagged", "fields": [
            {"name": "ticket_id", "type": "string", "regex": "T-[0-9]{6}"},
            {"name": "flags", "type": "string",
             "regex": "(AGING_60|AGING_90|UNASSIGNED|MISSING_SCOPE)"},
        ]}
        gen = run_generation(GenerateConfig(
            schema=schema, rows=200, seed=8, output_dir=str(tmp_path)))
        rows = _read_csv(gen["output"]["rows_path"])
        allowed = {"AGING_60", "AGING_90", "UNASSIGNED", "MISSING_SCOPE"}
        assert all(r["flags"] in allowed for r in rows)
        val = run_validation(ValidateConfig(
            rows_path=gen["output"]["rows_path"], schema=schema))
        assert val["valid"] is True, val["violations"][:3]
