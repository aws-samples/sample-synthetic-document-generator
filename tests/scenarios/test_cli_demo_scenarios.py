# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SA demo-data scenarios, driven through the CLI / core functions.

Three realistic Solutions-Architect workflows (see conftest docstring):
  S1 customer-run, customer-data-seeded  (PII must not leak)
  S2 SA-run, public-data-seeded
  S3 SA-run, prompt-seeded (no document)

Every paid stage uses an injected stub client; the free stages (generate/test)
touch no AWS at all. Each scenario ends by asserting the keystone invariant:
generated rows VALIDATE against the schema they were generated from.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from .conftest import (
    CUSTOMER_PII_VALUES,
    PIPELINE_AVAILABLE,
    PIPELINE_SKIP_REASON,
    bedrock_extract_stub,
    bedrock_schema_stub,
    comprehend_stub,
)

pytestmark = pytest.mark.skipif(not PIPELINE_AVAILABLE, reason=PIPELINE_SKIP_REASON)

if PIPELINE_AVAILABLE:
    from pocsynth import presets
    from pocsynth.extract import ExtractConfig, run_extraction
    from pocsynth.generate import GenerateConfig, run_generation
    from pocsynth.schemagen import SchemaConfig, run_schema
    from pocsynth.validate import ValidateConfig, run_validation


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------- #
# Scenario 1 — Customer runs it on their own PII-bearing document
# --------------------------------------------------------------------------- #
class TestScenario1CustomerDataSeeded:
    """The customer runs extract -> schema -> generate -> test on a real intake
    form. The synthetic dataset they share back must contain NONE of the real
    PII values, while non-PII categoricals (state, plan) keep realistic
    distributions."""

    def test_pii_audited_and_never_leaks_into_synthetic_output(
        self, customer_pdf, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "out"

        # 1) EXTRACT (paid, customer's own account) — discovery mode, with the
        #    PII audit ON by default. Comprehend flags name/ssn/email/mrn fields.
        bedrock = bedrock_extract_stub(
            records=[
                {"full_name": "Alice Hernandez", "ssn": "555-22-7788",
                 "email": "alice.hernandez@acme.example", "mrn": "MRN-009132",
                 "state": "CA", "plan": "Gold"},
            ]
        )
        comprehend = comprehend_stub(
            entities_per_call=[[{"Type": "NAME"}, {"Type": "SSN"},
                                {"Type": "EMAIL"}, {"Type": "OTHER"}]]
        )
        ext = run_extraction(
            ExtractConfig(
                pdf_url=str(customer_pdf), schema=None, pii_audit=True,
                output_dir=str(out), bedrock_client=bedrock,
                comprehend_client=comprehend,
            )
        )
        assert ext["pii_audit"]["enabled"] is True
        assert ext["pii_audit"]["entities_found"] >= 1
        # The fields carrying real PII are flagged for the schema step.
        assert {"full_name", "ssn", "email", "mrn"} <= set(ext["pii_audit"]["pii_fields"])

        # 2) SCHEMA --infer (paid) — PII guard strips real-value enums.
        # The model over-eagerly enumerated the real SSN values it saw — the
        # PII guard must strip that enum and surface a suppression note.
        schema_bedrock = bedrock_schema_stub(
            schema_fields=[
                {"name": "full_name", "type": "string", "faker": "name"},
                {"name": "ssn", "type": "string", "faker": "ssn",
                 "enum": ["555-22-7788", "555-22-9001"], "weights": {"555-22-7788": 0.5, "555-22-9001": 0.5}},
                {"name": "email", "type": "string", "faker": "email"},
                {"name": "mrn", "type": "string", "regex": "MRN-[0-9]{6}"},
                {"name": "state", "type": "string", "enum": ["CA", "NY"]},
                {"name": "plan", "type": "string", "enum": ["Gold", "Silver"]},
            ]
        )
        sch = run_schema(
            SchemaConfig(
                sample_path=ext["output"]["sample_path"], distribution="auto",
                output_dir=str(out), bedrock_client=schema_bedrock,
            )
        )
        schema = json.loads(Path(sch["output"]["schema_path"]).read_text())
        by_name = {f["name"]: f for f in schema["fields"]}
        # PII fields must NOT carry real-value enums; non-PII may.
        for pii_field in ("full_name", "ssn", "email", "mrn"):
            assert "enum" not in by_name[pii_field], pii_field
            assert by_name[pii_field].get("faker") or by_name[pii_field].get("regex")
        assert set(by_name["state"]["enum"]) <= {"CA", "NY"}
        # The suppression is surfaced, not silent.
        assert any("pii" in n.get("issue", "").lower() or "PII" in n.get("recommendation", "")
                   for n in sch.get("lint", {}).get("notes", sch.get("lint_notes", [])))

        # 3) GENERATE (free, offline) 200 rows, deterministic.
        gen = run_generation(
            GenerateConfig(schema=schema, rows=200, export_format="csv",
                           seed=42, output_dir=str(out))
        )
        rows_path = Path(gen["output"]["rows_path"])
        assert gen["cost"] is None  # free
        blob = rows_path.read_text(encoding="utf-8")

        # THE GUARANTEE: no real customer value appears anywhere in the output.
        for real in CUSTOMER_PII_VALUES:
            assert real not in blob, f"real PII leaked: {real!r}"
        # ...nor in the schema artifact that gets shared.
        assert all(real not in json.dumps(schema) for real in CUSTOMER_PII_VALUES)

        # 4) TEST (free) — generated rows validate against their schema.
        val = run_validation(ValidateConfig(rows_path=str(rows_path), schema=schema))
        assert val["valid"] is True, val["violations"][:3]
        assert val["rows_checked"] == 200

    def test_determinism_same_seed_byte_identical(self, customer_pdf, tmp_path):
        """Customer can reproduce the exact dataset for an auditable demo."""
        schema = {
            "schema": 1, "name": "demo",
            "fields": [
                {"name": "state", "type": "string", "enum": ["CA", "NY"],
                 "weights": {"CA": 0.7, "NY": 0.3}},
                {"name": "mrn", "type": "string", "regex": "MRN-[0-9]{6}"},
            ],
        }
        a = tmp_path / "a"
        b = tmp_path / "b"
        run_generation(GenerateConfig(schema=schema, rows=50, seed=7, output_dir=str(a)))
        run_generation(GenerateConfig(schema=schema, rows=50, seed=7, output_dir=str(b)))
        assert (a / "rows.csv").read_bytes() == (b / "rows.csv").read_bytes()


# --------------------------------------------------------------------------- #
# Scenario 2 — SA seeds from PUBLIC data
# --------------------------------------------------------------------------- #
class TestScenario2PublicDataSeeded:
    """The SA seeds from a public, non-sensitive catalog. No customer PII is
    ever involved; the SA builds a believable demo dataset and validates it."""

    def test_public_seed_extract_to_validated_dataset(self, public_pdf, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "out"

        bedrock = bedrock_extract_stub(
            records=[
                {"sku": "SKU-100", "category": "Widget", "price": "19.99", "region": "US"},
                {"sku": "SKU-101", "category": "Gadget", "price": "49.50", "region": "EU"},
            ]
        )
        # Public data: Comprehend finds nothing.
        comprehend = comprehend_stub(entities_per_call=[[]])
        ext = run_extraction(
            ExtractConfig(pdf_url=str(public_pdf), schema=None, pii_audit=True,
                          output_dir=str(out), bedrock_client=bedrock,
                          comprehend_client=comprehend)
        )
        assert ext["pii_audit"]["entities_found"] == 0
        assert ext["pii_audit"]["pii_fields"] == []

        schema_bedrock = bedrock_schema_stub(
            schema_fields=[
                {"name": "sku", "type": "string", "regex": "SKU-[0-9]{3}"},
                {"name": "category", "type": "string", "enum": ["Widget", "Gadget"],
                 "weights": {"Widget": 0.66, "Gadget": 0.34}},
                {"name": "price", "type": "number", "faker": "pyfloat",
                 "faker_args": {"min_value": 5, "max_value": 2000, "right_digits": 2}},
                {"name": "region", "type": "string", "enum": ["US", "EU"]},
            ]
        )
        sch = run_schema(
            SchemaConfig(sample_path=ext["output"]["sample_path"], distribution="infer",
                         output_dir=str(out), bedrock_client=schema_bedrock)
        )
        schema = json.loads(Path(sch["output"]["schema_path"]).read_text())

        gen = run_generation(
            GenerateConfig(schema=schema, rows=500, export_format="json",
                           seed=1, output_dir=str(out))
        )
        rows = json.loads(Path(gen["output"]["rows_path"]).read_text())
        assert len(rows) == 500
        # Category honors the inferred (non-uniform) distribution within tolerance.
        widgets = sum(1 for r in rows if r["category"] == "Widget")
        assert 0.55 < widgets / 500 < 0.77

        val = run_validation(ValidateConfig(rows_path=gen["output"]["rows_path"], schema=schema))
        assert val["valid"] is True, val["violations"][:3]

    def test_preset_path_is_fully_offline(self, tmp_path):
        """The fastest SA demo path: a bundled preset, zero AWS, instant."""
        names = [p["name"] for p in presets.list_presets()]
        assert names, "at least one preset must ship"
        schema = presets.load_preset(names[0])
        gen = run_generation(
            GenerateConfig(schema=schema, rows=100, seed=3, output_dir=str(tmp_path))
        )
        assert gen["cost"] is None
        val = run_validation(
            ValidateConfig(rows_path=gen["output"]["rows_path"], schema=schema)
        )
        assert val["valid"] is True


# --------------------------------------------------------------------------- #
# Scenario 3 — SA describes the business in natural language (no document)
# --------------------------------------------------------------------------- #
class TestScenario3PromptSeeded:
    """No document at all: the SA types a business description; schema is
    inferred from the prompt (ADR-0008), then generated + validated for free."""

    def test_prompt_to_validated_dataset(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "out"

        schema_bedrock = bedrock_schema_stub(
            schema_fields=[
                {"name": "account_name", "type": "string", "faker": "company"},
                {"name": "plan", "type": "string", "enum": ["Starter", "Pro", "Enterprise"],
                 "weights": {"Starter": 0.5, "Pro": 0.3, "Enterprise": 0.2}},
                {"name": "mrr", "type": "number", "faker": "pyfloat",
                 "faker_args": {"min_value": 50, "max_value": 50000, "right_digits": 2}},
                {"name": "signup_date", "type": "date", "faker": "date_this_year"},
            ]
        )
        sch = run_schema(
            SchemaConfig(
                prompt="A B2B SaaS company's customer accounts with plan tier and MRR",
                distribution="synthetic", output_dir=str(out),
                bedrock_client=schema_bedrock,
            )
        )
        assert sch["input"]["mode"] == "from_prompt"
        schema = json.loads(Path(sch["output"]["schema_path"]).read_text())

        gen = run_generation(
            GenerateConfig(schema=schema, rows=300, export_format="csv",
                           seed=11, output_dir=str(out))
        )
        # signup_date serialized as ISO-8601 (ADR-0006) and validates as `date`.
        val = run_validation(ValidateConfig(rows_path=gen["output"]["rows_path"], schema=schema))
        assert val["valid"] is True, val["violations"][:3]

    def test_from_prompt_distribution_cannot_be_infer(self, tmp_path):
        """No document => no value_counts => distribution=infer must downgrade to
        synthetic, recorded transparently (ADR-0004/0008)."""
        schema_bedrock = bedrock_schema_stub(
            schema_fields=[{"name": "tier", "type": "string",
                            "enum": ["A", "B"], "weights": {"A": 0.6, "B": 0.4}}]
        )
        sch = run_schema(
            SchemaConfig(prompt="widgets with a tier", distribution="infer",
                         output_dir=str(tmp_path), bedrock_client=schema_bedrock)
        )
        per_field = sch["distribution"]["per_field_source"]
        assert per_field["tier"] in {"synthetic", "uniform"}
        assert sch["distribution"]["requested"] == "infer"
