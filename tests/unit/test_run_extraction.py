# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for extract.run_extraction — mocked Bedrock/Comprehend, no AWS."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import fitz
import pytest

from pocsynth.errors import PartialError
from pocsynth.extract import ExtractConfig, run_extraction


def _make_pdf(tmp_path: Path, pages: int = 2) -> Path:
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page()
        p.insert_text((72, 96), f"Name: Person {i}  State: CA  Plan: Gold", fontsize=11)
    path = tmp_path / "in.pdf"
    doc.save(path)
    doc.close()
    return path


def _bedrock_tooluse(tool_name: str, payload: dict) -> MagicMock:
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"name": tool_name, "input": payload}}
        ]}},
        "usage": {"inputTokens": 50, "outputTokens": 20, "totalTokens": 70},
        "stopReason": "tool_use",
    }
    return client


def _bedrock_no_tool() -> MagicMock:
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [{"text": "nope"}]}},
        "usage": {"inputTokens": 10, "outputTokens": 2},
        "stopReason": "end_turn",
    }
    return client


def _comprehend(types: list[str]) -> MagicMock:
    """Return a comprehend mock that reports complete PII entities of the given
    types (offsets within the scanned text), for both the audit CSV scan and
    the per-field PII probe."""
    entities = [
        {"Type": t, "Score": 0.99, "BeginOffset": 0, "EndOffset": 3}
        for t in types
    ]
    client = MagicMock()
    client.detect_pii_entities.return_value = {"Entities": entities}
    return client


class TestDiscovery:
    def test_merges_fields_and_flags_pii(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pdf = _make_pdf(tmp_path, pages=2)
        bedrock = _bedrock_tooluse("observe_fields", {"fields": [
            {"name": "name", "type_hint": "string", "value_counts": {"Person 0": 1}},
            {"name": "state", "type_hint": "string", "value_counts": {"CA": 1}},
        ]})
        comp = _comprehend(["NAME"])  # flags every scanned field
        res = run_extraction(ExtractConfig(
            pdf_url=str(pdf), schema=None, output_dir=str(tmp_path / "o"),
            bedrock_client=bedrock, comprehend_client=comp,
        ))
        assert res["input"]["mode"] == "discovery"
        assert res["output"]["pages_processed"] == 2
        assert res["pii_audit"]["enabled"] is True
        sample = json.loads(Path(res["output"]["sample_path"]).read_text())
        # value_counts summed across the 2 pages
        names = {f["name"]: f for f in sample["fields"]}
        assert names["state"]["value_counts"]["CA"] == 2
        assert names["name"]["pii"] is True

    def test_token_accounting(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pdf = _make_pdf(tmp_path, pages=2)
        bedrock = _bedrock_tooluse("observe_fields", {"fields": [
            {"name": "x", "type_hint": "string", "value_counts": {"a": 1}}]})
        res = run_extraction(ExtractConfig(
            pdf_url=str(pdf), output_dir=str(tmp_path / "o"),
            bedrock_client=bedrock, pii_audit=False,
        ))
        assert res["output"]["bedrock_usage"]["input_tokens"] == 100  # 50 * 2 pages


class TestConform:
    def test_records_flat_appended(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pdf = _make_pdf(tmp_path, pages=2)
        schema = {"schema": 1, "name": "t", "fields": [
            {"name": "name", "type": "string"}, {"name": "state", "type": "string"}]}
        bedrock = _bedrock_tooluse("extract_records", {"records": [
            {"name": "P", "state": "CA"}]})
        res = run_extraction(ExtractConfig(
            pdf_url=str(pdf), schema=schema, output_dir=str(tmp_path / "o"),
            bedrock_client=bedrock, pii_audit=False,
        ))
        assert res["input"]["mode"] == "conform"
        assert res["output"]["records_extracted"] == 2  # one per page

    def test_csv_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pdf = _make_pdf(tmp_path, pages=1)
        schema = {"schema": 1, "name": "t", "fields": [{"name": "name", "type": "string"}]}
        bedrock = _bedrock_tooluse("extract_records", {"records": [{"name": "P"}]})
        res = run_extraction(ExtractConfig(
            pdf_url=str(pdf), schema=schema, export_format="csv",
            output_dir=str(tmp_path / "o"), bedrock_client=bedrock, pii_audit=False,
        ))
        assert res["output"]["sample_path"].endswith(".csv")


class TestFailures:
    def test_no_tooluse_all_pages_partial_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pdf = _make_pdf(tmp_path, pages=1)
        with pytest.raises(PartialError):
            run_extraction(ExtractConfig(
                pdf_url=str(pdf), output_dir=str(tmp_path / "o"),
                bedrock_client=_bedrock_no_tool(), pii_audit=False,
            ))


def _bedrock_fail_on_pages(fail_pages: set[int], payload: dict,
                           input_tokens: int = 50, output_tokens: int = 20) -> MagicMock:
    """A converse mock that raises on the given 1-based page numbers (via a
    ThrottlingException ClientError) and otherwise returns a successful toolUse."""
    from botocore.exceptions import ClientError
    client = MagicMock()
    state = {"n": 0}

    def _converse(**_kw):
        state["n"] += 1
        if state["n"] in fail_pages:
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "Converse",
            )
        return {
            "output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"name": "extract_records", "input": payload}}]}},
            "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
            "stopReason": "tool_use",
        }

    client.converse.side_effect = _converse
    return client


class TestPartialFailureReconciliation:
    """Gap 1: a mixed success/failure multi-page run must keep the envelope
    counters and token accounting internally consistent."""

    def test_mixed_failure_counters_and_token_accounting(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pdf = _make_pdf(tmp_path, pages=3)
        schema = {"schema": 1, "name": "t", "fields": [{"name": "name", "type": "string"}]}
        # Page 2 of 3 fails at converse; pages 1 and 3 succeed.
        bedrock = _bedrock_fail_on_pages(
            {2}, {"records": [{"name": "P"}]}, input_tokens=50, output_tokens=20)
        res = run_extraction(ExtractConfig(
            pdf_url=str(pdf), schema=schema, output_dir=str(tmp_path / "o"),
            bedrock_client=bedrock, pii_audit=False,
        ))
        out = res["output"]
        # Reconciliation: processed + failed == attempted.
        assert out["pages_attempted"] == 3
        assert out["pages_processed"] == 2
        assert len(res["page_failures"]) == 1
        assert out["pages_processed"] + len(res["page_failures"]) == out["pages_attempted"]
        # The failure is recorded with its page number and a translated code.
        assert res["page_failures"][0]["page"] == 2
        # Tokens are summed ONLY over the 2 successful pages (50+20 each).
        assert out["bedrock_usage"]["input_tokens"] == 100
        assert out["bedrock_usage"]["output_tokens"] == 40
        # 2 successful conform records survived.
        assert out["records_extracted"] == 2
