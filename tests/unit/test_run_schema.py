# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for schemagen.run_schema — lint (offline), infer + from-prompt (mocked)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from pocsynth.schemagen import SchemaConfig, run_schema


def _emit_schema_client(fields: list[dict]) -> MagicMock:
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"name": "emit_schema",
                         "input": {"schema": 1, "name": "demo", "fields": fields}}}
        ]}},
        "usage": {"inputTokens": 100, "outputTokens": 40},
        "stopReason": "tool_use",
    }
    return client


class TestLintMode:
    def test_lint_offline_no_client(self, tmp_path):
        schema = {"schema": 1, "name": "t", "fields": [
            {"name": "ssn", "type": "string", "faker": "ssn", "enum": ["1"]}]}
        p = tmp_path / "s.json"
        p.write_text(json.dumps(schema))
        res = run_schema(SchemaConfig(in_schema_path=str(p), output_dir=str(tmp_path / "o")))
        assert res["input"]["mode"] == "lint"
        assert res["lint"]["issues_total"] >= 1
        assert Path(res["output"]["doc_path"]).exists()

    def test_fix_applies_and_lists(self, tmp_path):
        schema = {"schema": 1, "name": "t", "fields": [
            {"name": "ssn", "type": "string", "faker": "ssn", "enum": ["1"]}]}
        p = tmp_path / "s.json"
        p.write_text(json.dumps(schema))
        res = run_schema(SchemaConfig(in_schema_path=str(p), fix=True, output_dir=str(tmp_path / "o")))
        assert res["lint"]["applied"]
        fixed = json.loads(Path(res["output"]["fixed_schema_path"]).read_text())
        assert "enum" not in fixed["fields"][0]


class TestInferMode:
    def test_infer_weights_from_counts(self, tmp_path):
        sample = {"schema": 1, "source": "x.pdf", "fields": [
            {"name": "state", "type_hint": "string", "value_counts": {"CA": 7, "NY": 3}}]}
        sp = tmp_path / "sample.json"
        sp.write_text(json.dumps(sample))
        client = _emit_schema_client([
            {"name": "state", "type": "string", "enum": ["CA", "NY"],
             "weights": {"CA": 0.5, "NY": 0.5}}])
        res = run_schema(SchemaConfig(sample_path=str(sp), distribution="infer",
                                      output_dir=str(tmp_path / "o"), bedrock_client=client))
        assert res["input"]["mode"] == "infer"
        schema = json.loads(Path(res["output"]["schema_path"]).read_text())
        # infer overrides the model's weights with exact counts -> 0.7/0.3
        st = schema["fields"][0]
        assert st["weights"]["CA"] == 0.7
        assert res["distribution"]["per_field_source"]["state"] == "infer"

    def test_pii_guard_strips_enum(self, tmp_path):
        sample = {"schema": 1, "source": "x.pdf", "fields": [
            {"name": "ssn", "type_hint": "string", "pii": True,
             "value_counts": {"111-11-1111": 1, "222-22-2222": 1}}]}
        sp = tmp_path / "sample.json"
        sp.write_text(json.dumps(sample))
        client = _emit_schema_client([
            {"name": "ssn", "type": "string", "faker": "ssn",
             "enum": ["111-11-1111"], "weights": {"111-11-1111": 1.0}}])
        res = run_schema(SchemaConfig(sample_path=str(sp), output_dir=str(tmp_path / "o"),
                                      bedrock_client=client))
        schema = json.loads(Path(res["output"]["schema_path"]).read_text())
        f = schema["fields"][0]
        assert "enum" not in f and "weights" not in f
        assert f.get("faker")


class TestFromPromptMode:
    def test_from_prompt_no_counts_downgrades_infer(self, tmp_path):
        client = _emit_schema_client([
            {"name": "tier", "type": "string", "enum": ["A", "B"],
             "weights": {"A": 0.6, "B": 0.4}}])
        res = run_schema(SchemaConfig(prompt="widgets with a tier", distribution="infer",
                                      output_dir=str(tmp_path / "o"), bedrock_client=client))
        assert res["input"]["mode"] == "from_prompt"
        assert res["distribution"]["per_field_source"]["tier"] in {"synthetic", "uniform"}
        assert res["distribution"]["requested"] == "infer"
