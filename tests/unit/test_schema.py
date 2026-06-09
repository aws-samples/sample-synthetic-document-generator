# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the shared schema model: validation, typing, lint, fix, docs."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from pocsynth.errors import SchemaError
from pocsynth.schema import (
    FIELD_TYPES,
    _validate_schema_shape,
    apply_fixes,
    coerce_and_check,
    discovery_toolspec,
    document_schema,
    field_names,
    lint_schema,
    load_schema,
    merge_observations,
    schema_infer_toolspec,
    schema_to_toolspec,
    serialize,
    weights_from_counts,
)


def _schema(fields):
    return {"schema": 1, "name": "t", "fields": fields}


# ---------- validation ----------
class TestValidate:
    def test_valid_schema_ok(self):
        _validate_schema_shape(_schema([{"name": "a", "type": "string"}]))

    @pytest.mark.parametrize("bad", [
        {"schema": 2, "fields": [{"name": "a", "type": "string"}]},
        {"schema": 1, "fields": []},
        {"schema": 1, "fields": [{"type": "string"}]},
        {"schema": 1, "fields": [{"name": "a", "type": "blob"}]},
        {"schema": 1, "fields": [{"name": "a", "type": "string"}, {"name": "a", "type": "string"}]},
    ])
    def test_invalid_raises(self, bad):
        with pytest.raises(SchemaError):
            _validate_schema_shape(bad)

    def test_load_schema_roundtrip(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps(_schema([{"name": "a", "type": "integer"}])))
        assert field_names(load_schema(p)) == ["a"]

    def test_load_missing(self, tmp_path):
        with pytest.raises(SchemaError):
            load_schema(tmp_path / "nope.json")


# ---------- typing: serialize ----------
class TestSerialize:
    def test_date_iso_both_formats(self):
        d = date(1990, 5, 1)
        assert serialize(d, "date", "csv") == "1990-05-01"
        assert serialize(d, "date", "json") == "1990-05-01"

    def test_datetime_iso(self):
        dt = datetime(1990, 5, 1, 9, 30)
        assert serialize(dt, "datetime", "json") == "1990-05-01T09:30:00"

    def test_csv_stringifies(self):
        assert serialize(42, "integer", "csv") == "42"
        assert serialize(True, "boolean", "csv") == "true"

    def test_json_keeps_native(self):
        assert serialize(42, "integer", "json") == 42
        assert serialize(True, "boolean", "json") is True

    def test_none(self):
        assert serialize(None, "string", "csv") == ""
        assert serialize(None, "string", "json") is None


# ---------- typing: coerce_and_check ----------
class TestCoerce:
    def test_empty_is_null(self):
        assert coerce_and_check("", "integer") == (True, None)
        assert coerce_and_check(None, "date") == (True, None)

    def test_csv_str_and_json_int_agree(self):
        assert coerce_and_check("42", "integer") == (True, 42)
        assert coerce_and_check(42, "integer") == (True, 42)

    def test_integer_rejects_fractional(self):
        ok, _ = coerce_and_check("4.5", "integer")
        assert ok is False

    def test_number(self):
        assert coerce_and_check("3.14", "number")[0] is True
        assert coerce_and_check("x", "number")[0] is False

    def test_boolean(self):
        assert coerce_and_check("true", "boolean") == (True, True)
        assert coerce_and_check("0", "boolean") == (True, False)
        assert coerce_and_check("maybe", "boolean")[0] is False

    def test_date(self):
        assert coerce_and_check("1990-05-01", "date")[0] is True
        assert coerce_and_check("05/01/1990", "date")[0] is False

    def test_datetime_z(self):
        assert coerce_and_check("1990-05-01T09:30:00Z", "datetime")[0] is True

    def test_string_accepts_anything(self):
        assert coerce_and_check("anything", "string") == (True, "anything")


# ---------- distributions ----------
class TestWeights:
    def test_normalize(self):
        w = weights_from_counts({"CA": 7, "NY": 2, "TX": 1})
        assert abs(sum(w.values()) - 1.0) < 1e-6
        assert w["CA"] == 0.7

    def test_zero_total_uniform(self):
        w = weights_from_counts({"a": 0, "b": 0})
        assert w["a"] == w["b"] == 0.5

    def test_merge_observations_sums_counts(self):
        merged = merge_observations([
            {"fields": [{"name": "state", "type_hint": "string", "value_counts": {"CA": 3, "NY": 1}}]},
            {"fields": [{"name": "state", "type_hint": "string", "value_counts": {"CA": 4, "TX": 1}}]},
        ])
        vc = {f["name"]: f["value_counts"] for f in merged}["state"]
        assert vc == {"CA": 7, "NY": 1, "TX": 1}

    def test_merge_caps_distinct(self):
        page = {"fields": [{"name": "f", "type_hint": "string",
                            "value_counts": {str(i): 1 for i in range(50)}}]}
        merged = merge_observations([page], cap=20)
        assert len(merged[0]["value_counts"]) == 20

    def test_merge_tolerates_malformed_value_counts(self):
        # Model-controlled value_counts may be a list, a non-numeric count, or
        # absent — merge must degrade, not raise.
        merged = merge_observations([
            {"fields": [
                {"name": "a", "value_counts": {"CA": "many", "NY": 2}},  # non-numeric
                {"name": "b", "value_counts": ["x", "y", "x"]},          # bare list
                {"name": "c", "value_counts": "garbage"},                # wrong type
            ]},
        ])
        by = {f["name"]: f["value_counts"] for f in merged}
        assert by["a"] == {"CA": 1, "NY": 2}      # non-numeric → 1
        assert by["b"] == {"x": 2, "y": 1}        # list → count 1 each, summed
        assert by["c"] == {}                       # unusable → empty, no crash


# ---------- lint + fix + doc ----------
class TestLint:
    def test_unknown_provider(self):
        issues = lint_schema(_schema([{"name": "a", "type": "string", "faker": "not_a_provider"}]))
        assert any(i["issue"] == "unknown_faker_provider" for i in issues)

    def test_proxy_control_methods_flagged_as_unknown(self):
        # format / seed_instance are callable on the Faker proxy but are not
        # data providers; lint must flag them so they never reach generation.
        for bad in ("format", "seed_instance"):
            issues = lint_schema(_schema([{"name": "a", "type": "string", "faker": bad}]))
            assert any(i["issue"] == "unknown_faker_provider" for i in issues), bad

    def test_bad_regex(self):
        issues = lint_schema(_schema([{"name": "a", "type": "string", "regex": "([unclosed"}]))
        assert any(i["issue"] == "bad_regex" for i in issues)

    def test_pii_enum_flagged_and_fixed(self):
        s = _schema([{"name": "ssn", "type": "string", "faker": "ssn",
                      "enum": ["111-11-1111"], "weights": {"111-11-1111": 1.0}}])
        issues = lint_schema(s)
        pii = [i for i in issues if i["issue"] == "pii_enum"]
        assert pii and pii[0]["autofixable"]
        fixed, changes = apply_fixes(s, issues)
        f = fixed["fields"][0]
        assert "enum" not in f and "weights" not in f
        all_removed = {k for c in changes for k in c["removed"]}
        assert {"enum", "weights"} <= all_removed

    def test_document_schema_markdown(self):
        md = document_schema(_schema([{"name": "a", "type": "integer", "description": "x"}]))
        assert "| field |" in md and "| a |" in md


# ---------- toolspecs ----------
class TestToolspecs:
    def test_conform_records_array(self):
        spec = schema_to_toolspec(_schema([{"name": "a", "type": "integer"},
                                           {"name": "b", "type": "string", "enum": ["x"]}]))
        js = spec["tools"][0]["toolSpec"]["inputSchema"]["json"]
        item = js["properties"]["records"]["items"]
        assert item["properties"]["a"]["type"] == "integer"
        assert item["properties"]["b"]["enum"] == ["x"]
        assert spec["toolChoice"]["tool"]["name"] == "extract_records"

    def test_discovery_fields_value_counts(self):
        spec = discovery_toolspec()
        js = spec["tools"][0]["toolSpec"]["inputSchema"]["json"]
        assert "value_counts" in js["properties"]["fields"]["items"]["properties"]

    def test_infer_emit_schema(self):
        spec = schema_infer_toolspec()
        assert spec["tools"][0]["toolSpec"]["name"] == "emit_schema"
        item = spec["tools"][0]["toolSpec"]["inputSchema"]["json"]["properties"]["fields"]["items"]
        assert set(item["properties"]["type"]["enum"]) == set(FIELD_TYPES)
