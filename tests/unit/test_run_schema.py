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


class TestProviderSanitization:
    def test_invalid_model_provider_coerced_not_fatal(self, tmp_path):
        # Bedrock proposes a hallucinated/namespaced provider and a call
        # expression. run_schema must coerce them so generation never hard-fails
        # (exit 2 SCHEMA_INVALID) on a recoverable model quirk.
        from pocsynth.generate import (
            GenerateConfig,
            run_generation,
            valid_faker_providers,
        )

        client = _emit_schema_client([
            {"name": "title", "type": "string", "faker": "book.title"},
            {"name": "variety", "type": "string", "faker": "bothify(text='??-##')"},
            {"name": "owner", "type": "string", "faker": "name"},  # valid, untouched
        ])
        res = run_schema(SchemaConfig(prompt="a library catalog",
                                      output_dir=str(tmp_path / "o"), bedrock_client=client))
        schema = json.loads(Path(res["output"]["schema_path"]).read_text())
        by = {f["name"]: f for f in schema["fields"]}
        valid = valid_faker_providers()
        # Every emitted faker is now a real provider.
        assert all(f["faker"] in valid for f in schema["fields"] if f.get("faker"))
        assert by["owner"]["faker"] == "name"  # valid one left alone
        # The coercion is reported.
        notes = res["lint"]["notes"]
        assert any(n["issue"] == "invalid_faker_provider_coerced" for n in notes)
        # And generation actually succeeds end-to-end.
        gen = run_generation(GenerateConfig(schema=schema, rows=10, seed=1,
                                            output_dir=str(tmp_path / "g")))
        assert gen["output"]["rows_written"] == 10

    def test_stale_faker_args_dropped_when_provider_coerced(self, tmp_path):
        # Regression: the model emits an invalid provider WITH args that only
        # made sense for it (book.title takes no args). Coercing faker→word
        # while leaving faker_args in place calls word(nb_words=3) → TypeError
        # deep in generation. The stale args must be dropped with the provider.
        from pocsynth.generate import GenerateConfig, run_generation

        client = _emit_schema_client([
            {"name": "title", "type": "string", "faker": "book.title",
             "faker_args": {"nb_words": 3}},
            {"name": "price", "type": "number", "faker": "money.amount",
             "faker_args": {"left_digits": 4}},
        ])
        res = run_schema(SchemaConfig(prompt="a catalog",
                                      output_dir=str(tmp_path / "o"), bedrock_client=client))
        schema = json.loads(Path(res["output"]["schema_path"]).read_text())
        # The fallback provider carries no leftover args from the invalid one.
        assert all("faker_args" not in f for f in schema["fields"])
        # Generation runs to completion instead of raising TypeError.
        gen = run_generation(GenerateConfig(schema=schema, rows=8, seed=1,
                                            output_dir=str(tmp_path / "g")))
        assert gen["output"]["rows_written"] == 8

    def test_empty_enum_with_invalid_faker_does_not_crash_generation(self, tmp_path):
        # Regression: the model emits an empty enum:[] alongside an invalid
        # faker. Generation keys on key PRESENCE (`if "enum" in field`), so an
        # empty list left in place crashes random_element(elements=[]) with
        # "Cannot choose from an empty sequence". The sanitizer must drop the
        # empty enum so the (coerced) faker drives the field instead.
        from pocsynth.generate import GenerateConfig, run_generation

        client = _emit_schema_client([
            {"name": "status", "type": "string", "faker": "book.genre", "enum": []},
        ])
        res = run_schema(SchemaConfig(prompt="tickets",
                                      output_dir=str(tmp_path / "o"), bedrock_client=client))
        schema = json.loads(Path(res["output"]["schema_path"]).read_text())
        f = schema["fields"][0]
        assert not f.get("enum")  # empty enum dropped, not left to crash
        gen = run_generation(GenerateConfig(schema=schema, rows=6, seed=1,
                                            output_dir=str(tmp_path / "g")))
        assert gen["output"]["rows_written"] == 6


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
