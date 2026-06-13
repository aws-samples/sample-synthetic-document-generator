# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the offline safety-verification step (`verify`, ADR-0010)."""

from __future__ import annotations

import json

import pytest

from pocsynth.verify import VerifyConfig, _mask, _real_pii_values, run_verify


def _sample(tmp_path, obj) -> str:
    p = tmp_path / "sample.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def _rows(tmp_path, text, name="rows.csv") -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


DISCOVERY_SAMPLE = {
    "schema": 1, "source": "intake.pdf",
    "fields": [
        {"name": "ssn", "pii": True, "value_counts": {"555-22-7788": 1, "555-22-9001": 1}},
        {"name": "state", "pii": False, "value_counts": {"CA": 3, "NY": 1}},
    ],
}


class TestRealValueExtraction:
    def test_discovery_collects_only_flagged_pii(self, tmp_path):
        vals = _real_pii_values(DISCOVERY_SAMPLE, min_len=4)
        assert vals == {"555-22-7788", "555-22-9001"}
        # non-PII state codes are NOT candidates (allowed to survive as enums)
        assert "CA" not in vals

    def test_short_values_dropped(self):
        s = {"fields": [{"name": "x", "pii": True, "value_counts": {"AB": 1, "ABCDE": 1}}]}
        assert _real_pii_values(s, min_len=4) == {"ABCDE"}

    def test_conform_sample_treats_all_record_values_as_real(self):
        s = {"records": [{"name": "Alice Hernandez", "code": "X"}]}
        vals = _real_pii_values(s, min_len=4)
        assert "Alice Hernandez" in vals
        assert "X" not in vals  # too short


class TestVerdicts:
    def test_clean_rows_pass(self, tmp_path):
        sp = _sample(tmp_path, DISCOVERY_SAMPLE)
        rp = _rows(tmp_path, "ssn,state\n111-00-0000,CA\n222-00-0000,NY\n")
        res = run_verify(VerifyConfig(rows_path=rp, sample_path=sp))
        assert res["verdict"] == "pass"
        assert res["attestation"]["leaks"] == []

    def test_real_value_in_rows_fails(self, tmp_path):
        sp = _sample(tmp_path, DISCOVERY_SAMPLE)
        rp = _rows(tmp_path, "ssn,state\n555-22-7788,CA\n")
        res = run_verify(VerifyConfig(rows_path=rp, sample_path=sp))
        assert res["verdict"] == "fail"
        assert res["attestation"]["leaks"][0]["where"] == ["rows"]

    def test_real_value_in_schema_enum_fails(self, tmp_path):
        # Closes the fragment hole: a real value baked into a schema enum is a
        # leak even when the rows themselves are clean.
        sp = _sample(tmp_path, DISCOVERY_SAMPLE)
        rp = _rows(tmp_path, "ssn,state\n111-00-0000,CA\n")
        schema = {"schema": 1, "name": "t",
                  "fields": [{"name": "ssn", "type": "string", "enum": ["555-22-7788"]}]}
        res = run_verify(VerifyConfig(rows_path=rp, sample_path=sp, schema=schema))
        assert res["verdict"] == "fail"
        assert "schema" in res["attestation"]["leaks"][0]["where"]

    def test_real_value_in_regex_pattern_fails(self, tmp_path):
        sp = _sample(tmp_path, DISCOVERY_SAMPLE)
        rp = _rows(tmp_path, "ssn\n111-00-0000\n")
        schema = {"schema": 1, "name": "t",
                  "fields": [{"name": "ssn", "type": "string", "regex": "555-22-7788"}]}
        res = run_verify(VerifyConfig(rows_path=rp, sample_path=sp, schema=schema))
        assert res["verdict"] == "fail"

    def test_public_sample_is_not_applicable(self, tmp_path):
        pub = {"schema": 1, "source": "cat.pdf",
               "fields": [{"name": "sku", "pii": False, "value_counts": {"SKU-100": 1}}]}
        sp = _sample(tmp_path, pub)
        rp = _rows(tmp_path, "sku\nSKU-999\n")
        res = run_verify(VerifyConfig(rows_path=rp, sample_path=sp))
        assert res["verdict"] == "not_applicable"


class TestAttestation:
    def test_attestation_written_with_hashes_and_version(self, tmp_path):
        sp = _sample(tmp_path, DISCOVERY_SAMPLE)
        rp = _rows(tmp_path, "ssn\n111-00-0000\n")
        res = run_verify(VerifyConfig(rows_path=rp, sample_path=sp, output_dir=str(tmp_path)))
        att = res["attestation"]
        assert att["verdict"] == "pass"
        assert att["rows_sha256"] and len(att["rows_sha256"]) == 64
        assert att["source_hash"] and att["tool_version"]
        written = json.loads((tmp_path / "attestation.json").read_text())
        assert written["verdict"] == "pass"

    def test_attestation_masks_leaked_value(self, tmp_path):
        # The attestation must NOT carry the full real value (would re-leak it).
        sp = _sample(tmp_path, DISCOVERY_SAMPLE)
        rp = _rows(tmp_path, "ssn\n555-22-7788\n")
        res = run_verify(VerifyConfig(rows_path=rp, sample_path=sp, output_dir=str(tmp_path)))
        blob = (tmp_path / "attestation.json").read_text()
        assert "555-22-7788" not in blob
        assert res["attestation"]["leaks"][0]["value_preview"] == _mask("555-22-7788")

    def test_missing_sample_raises(self, tmp_path):
        from pocsynth.errors import SchemaError
        rp = _rows(tmp_path, "x\n1\n")
        with pytest.raises(SchemaError):
            run_verify(VerifyConfig(rows_path=rp, sample_path=str(tmp_path / "nope.json")))


class TestConformPipeline:
    def test_conform_real_record_value_leak_detected(self, tmp_path):
        sample = {"schema": 1, "source": "claims.pdf",
                  "records": [{"claimant": "Harold Webb", "amount": "4200.00"}]}
        sp = _sample(tmp_path, sample)
        rp = _rows(tmp_path, "claimant,amount\nHarold Webb,99.00\n")
        res = run_verify(VerifyConfig(rows_path=rp, sample_path=sp))
        assert res["verdict"] == "fail"
