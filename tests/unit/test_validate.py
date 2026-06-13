# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for offline validation (`test`) + the generate->test round-trip."""

from __future__ import annotations

import csv

import pytest

from pocsynth.errors import SchemaError
from pocsynth.generate import GenerateConfig, run_generation
from pocsynth.validate import ValidateConfig, run_validation


def _schema(fields, name="t"):
    return {"schema": 1, "name": name, "fields": fields}


def _write_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow(r)


class TestValidation:
    def test_conforming_rows_valid(self, tmp_path):
        s = _schema([{"name": "age", "type": "integer"},
                     {"name": "st", "type": "string", "enum": ["CA", "NY"]}])
        p = tmp_path / "rows.csv"
        _write_csv(p, ["age", "st"], [{"age": "30", "st": "CA"}, {"age": "41", "st": "NY"}])
        res = run_validation(ValidateConfig(rows_path=str(p), schema=s))
        assert res["valid"] is True
        assert res["rows_checked"] == 2

    def test_type_violation_caught(self, tmp_path):
        s = _schema([{"name": "age", "type": "integer"}])
        p = tmp_path / "rows.csv"
        _write_csv(p, ["age"], [{"age": "thirty"}])
        res = run_validation(ValidateConfig(rows_path=str(p), schema=s))
        assert res["valid"] is False
        v = res["violations"][0]
        assert v["row"] == 0 and v["field"] == "age" and v["rule"] == "type"

    def test_enum_and_regex_violations(self, tmp_path):
        s = _schema([{"name": "st", "type": "string", "enum": ["CA"]},
                     {"name": "mrn", "type": "string", "regex": "MRN-[0-9]{6}"}])
        p = tmp_path / "rows.csv"
        _write_csv(p, ["st", "mrn"], [{"st": "ZZ", "mrn": "BAD"}])
        res = run_validation(ValidateConfig(rows_path=str(p), schema=s))
        rules = {v["rule"] for v in res["violations"]}
        assert rules == {"enum", "regex"}

    def test_null_is_allowed(self, tmp_path):
        s = _schema([{"name": "age", "type": "integer"}])
        p = tmp_path / "rows.csv"
        _write_csv(p, ["age"], [{"age": ""}])
        assert run_validation(ValidateConfig(rows_path=str(p), schema=s))["valid"] is True

    def test_uncompilable_regex_reported_not_raised(self, tmp_path):
        # A schema regex that doesn't compile must be a field-level violation,
        # not a crash, and must be reported once (not per row).
        s = _schema([{"name": "code", "type": "string", "regex": "([unclosed"}])
        p = tmp_path / "rows.csv"
        _write_csv(p, ["code"], [{"code": "x"}, {"code": "y"}])
        res = run_validation(ValidateConfig(rows_path=str(p), schema=s))
        assert res["valid"] is False
        regex_v = [v for v in res["violations"] if v["rule"] == "regex"]
        assert len(regex_v) == 1  # reported once, not per row

    def test_integer_enum_membership_via_coercion(self, tmp_path):
        # Regression: enum members must be compared after type coercion, so a
        # CSV "2" validates against an integer enum [1,2,3] (the model emits
        # string enum members per the toolspec).
        s = _schema([{"name": "tier", "type": "integer", "enum": ["1", "2", "3"]}])
        p = tmp_path / "rows.csv"
        _write_csv(p, ["tier"], [{"tier": "2"}, {"tier": "3"}])
        res = run_validation(ValidateConfig(rows_path=str(p), schema=s))
        assert res["valid"] is True, res["violations"]
        # And an out-of-set integer is still caught.
        _write_csv(p, ["tier"], [{"tier": "9"}])
        assert run_validation(ValidateConfig(rows_path=str(p), schema=s))["valid"] is False


class TestRoundTrip:
    """The pipeline keystone: generate -> test is always valid, every type, both formats."""

    @pytest.mark.parametrize("fmt", ["csv", "json"])
    def test_all_types_round_trip(self, tmp_path, fmt):
        s = _schema([
            {"name": "s", "type": "string", "faker": "word"},
            {"name": "i", "type": "integer", "faker": "random_int"},
            {"name": "n", "type": "number", "faker": "pyfloat"},
            {"name": "b", "type": "boolean", "faker": "boolean"},
            {"name": "d", "type": "date", "faker": "date_object"},
            {"name": "dt", "type": "datetime", "faker": "date_time"},
            {"name": "e", "type": "string", "enum": ["x", "y"], "weights": {"x": 0.7, "y": 0.3}},
            {"name": "r", "type": "string", "regex": "ID-[0-9]{4}"},
        ])
        gen = run_generation(GenerateConfig(schema=s, rows=100, seed=99,
                                            export_format=fmt, output_dir=str(tmp_path)))
        res = run_validation(ValidateConfig(rows_path=gen["output"]["rows_path"], schema=s))
        assert res["valid"] is True, res["violations"][:3]
        assert res["rows_checked"] == 100


class TestMalformedRowsInput:
    """Malformed rows files must raise a clean SchemaError, never a bare
    JSONDecodeError / AttributeError that leaks the internal failure."""

    def test_malformed_json_raises_schema_error(self, tmp_path):
        p = tmp_path / "rows.json"
        p.write_text("{ this is not valid json", encoding="utf-8")
        s = _schema([{"name": "x", "type": "string"}])
        with pytest.raises(SchemaError):
            run_validation(ValidateConfig(rows_path=str(p), schema=s))

    def test_json_not_a_list_raises_schema_error(self, tmp_path):
        p = tmp_path / "rows.json"
        p.write_text('{"x": 1}', encoding="utf-8")  # an object, not a list
        s = _schema([{"name": "x", "type": "integer"}])
        with pytest.raises(SchemaError):
            run_validation(ValidateConfig(rows_path=str(p), schema=s))

    def test_json_list_of_scalars_raises_schema_error(self, tmp_path):
        # Regression: row.get(name) would AttributeError on an int element.
        p = tmp_path / "rows.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        s = _schema([{"name": "x", "type": "integer"}])
        with pytest.raises(SchemaError):
            run_validation(ValidateConfig(rows_path=str(p), schema=s))

    def test_missing_rows_file_raises_schema_error(self, tmp_path):
        s = _schema([{"name": "x", "type": "string"}])
        with pytest.raises(SchemaError):
            run_validation(ValidateConfig(rows_path=str(tmp_path / "nope.csv"), schema=s))

    def test_csv_missing_schema_column_treated_as_null(self, tmp_path):
        # A CSV whose header omits a schema field: the column reads as null,
        # which is permitted (v1 fields are nullable). Documents the behavior.
        s = _schema([{"name": "a", "type": "integer"}, {"name": "b", "type": "string"}])
        p = tmp_path / "rows.csv"
        _write_csv(p, ["a"], [{"a": "1"}, {"a": "2"}])  # no 'b' column
        res = run_validation(ValidateConfig(rows_path=str(p), schema=s))
        assert res["valid"] is True
        assert res["rows_checked"] == 2

    def test_csv_extra_column_ignored(self, tmp_path):
        s = _schema([{"name": "a", "type": "integer"}])
        p = tmp_path / "rows.csv"
        _write_csv(p, ["a", "extra"], [{"a": "1", "extra": "ignored"}])
        res = run_validation(ValidateConfig(rows_path=str(p), schema=s))
        assert res["valid"] is True
