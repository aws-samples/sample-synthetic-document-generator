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
