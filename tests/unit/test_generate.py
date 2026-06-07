# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for offline generation. No mocks, no AWS."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

from pocsynth.errors import SchemaError
from pocsynth.generate import GenerateConfig, run_generation


def _schema(fields, name="t"):
    return {"schema": 1, "name": name, "fields": fields}


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class TestDeterminism:
    def test_same_seed_byte_identical(self, tmp_path):
        s = _schema([{"name": "x", "type": "string", "faker": "name"}])
        a, b = tmp_path / "a", tmp_path / "b"
        run_generation(GenerateConfig(schema=s, rows=20, seed=42, output_dir=str(a)))
        run_generation(GenerateConfig(schema=s, rows=20, seed=42, output_dir=str(b)))
        assert (a / "rows.csv").read_bytes() == (b / "rows.csv").read_bytes()

    def test_row_count_and_headers(self, tmp_path):
        s = _schema([{"name": "x", "type": "integer", "faker": "random_int"},
                     {"name": "y", "type": "string", "faker": "word"}])
        res = run_generation(GenerateConfig(schema=s, rows=15, seed=1, output_dir=str(tmp_path)))
        rows = _read_csv(res["output"]["rows_path"])
        assert len(rows) == 15
        assert set(rows[0].keys()) == {"x", "y"}
        assert res["cost"] is None


class TestConstraints:
    def test_regex(self, tmp_path):
        s = _schema([{"name": "mrn", "type": "string", "regex": "MRN-[0-9]{6}"}])
        res = run_generation(GenerateConfig(schema=s, rows=30, seed=7, output_dir=str(tmp_path)))
        for row in _read_csv(res["output"]["rows_path"]):
            assert re.fullmatch("MRN-[0-9]{6}", row["mrn"])

    def test_enum_membership(self, tmp_path):
        s = _schema([{"name": "st", "type": "string", "enum": ["CA", "NY", "TX"]}])
        res = run_generation(GenerateConfig(schema=s, rows=50, seed=3, output_dir=str(tmp_path)))
        for row in _read_csv(res["output"]["rows_path"]):
            assert row["st"] in {"CA", "NY", "TX"}

    def test_weighted_enum_tracks_distribution(self, tmp_path):
        s = _schema([{"name": "st", "type": "string", "enum": ["CA", "NY"],
                      "weights": {"CA": 0.8, "NY": 0.2}}])
        res = run_generation(GenerateConfig(schema=s, rows=2000, seed=11, output_dir=str(tmp_path)))
        rows = _read_csv(res["output"]["rows_path"])
        ca = sum(1 for r in rows if r["st"] == "CA") / len(rows)
        assert 0.72 < ca < 0.88

    def test_unknown_provider_fails_fast(self, tmp_path):
        s = _schema([{"name": "x", "type": "string", "faker": "definitely_not_real"}])
        with pytest.raises(SchemaError):
            run_generation(GenerateConfig(schema=s, rows=1, output_dir=str(tmp_path)))


class TestJson:
    def test_json_native_scalars(self, tmp_path):
        s = _schema([{"name": "n", "type": "integer", "faker": "random_int"},
                     {"name": "ok", "type": "boolean", "faker": "boolean"}])
        res = run_generation(GenerateConfig(schema=s, rows=5, seed=2,
                                            export_format="json", output_dir=str(tmp_path)))
        rows = json.loads(Path(res["output"]["rows_path"]).read_text())
        assert isinstance(rows[0]["n"], int)
        assert isinstance(rows[0]["ok"], bool)
