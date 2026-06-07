# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for offline validation (`test`) + the generate->test round-trip."""

from __future__ import annotations

import csv

import pytest

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
