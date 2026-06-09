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
from pocsynth.generate import GenerateConfig, run_generation, stream_rows


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

    def test_regex_grouped_alternation(self, tmp_path):
        # Regression: grouped alternation must be resolved, not emitted literally.
        s = _schema([{"name": "code", "type": "string", "regex": "(cat|dog)-[0-9]{3}"}])
        res = run_generation(GenerateConfig(schema=s, rows=20, seed=4, output_dir=str(tmp_path)))
        for row in _read_csv(res["output"]["rows_path"]):
            assert re.fullmatch("(cat|dog)-[0-9]{3}", row["code"]), row["code"]

    def test_regex_literal_brace_does_not_crash(self, tmp_path):
        # Regression: a literal '{' / unterminated class must not raise ValueError.
        s = _schema([{"name": "a", "type": "string", "regex": "v[0-9]{1}-{beta}"},
                     {"name": "b", "type": "string", "regex": "[abc"}])
        res = run_generation(GenerateConfig(schema=s, rows=5, seed=1, output_dir=str(tmp_path)))
        rows = _read_csv(res["output"]["rows_path"])
        assert len(rows) == 5  # produced rows rather than crashing

    def test_regex_reversed_range_and_quantifier_do_not_crash(self, tmp_path):
        # Regression: reversed char-range [9-0] and reversed quantifier {5,2}
        # compile cleanly in stdlib re but used to crash _regexify.
        s = _schema([{"name": "a", "type": "string", "regex": "[9-0]{4}"},
                     {"name": "b", "type": "string", "regex": "x{5,2}"}])
        res = run_generation(GenerateConfig(schema=s, rows=8, seed=3, output_dir=str(tmp_path)))
        rows = _read_csv(res["output"]["rows_path"])
        assert len(rows) == 8
        for r in rows:
            assert r["a"].isdigit() and len(r["a"]) == 4  # [9-0] ordered to 0-9

    def test_non_data_faker_method_rejected(self, tmp_path):
        # Regression: a proxy control method (format/seed_instance) must be
        # rejected by provider validation, not silently dispatched.
        for bad in ("format", "seed_instance", "add_provider"):
            s = _schema([{"name": "x", "type": "string", "faker": bad}])
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


class TestStreamRows:
    """stream_rows yields the full dataset without materializing it (UI download)."""

    def test_csv_stream_header_plus_rows(self):
        s = _schema([{"name": "a", "type": "integer", "faker": "random_int"},
                     {"name": "b", "type": "string", "enum": ["x", "y"]}])
        text = "".join(stream_rows(s, 5000, export_format="csv", seed=1))
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert lines[0] == "a,b"
        assert len(lines) == 5001  # header + 5000

    def test_json_stream_is_valid_array(self):
        s = _schema([{"name": "a", "type": "integer", "faker": "random_int"}])
        text = "".join(stream_rows(s, 250, export_format="json", seed=2))
        rows = json.loads(text)
        assert len(rows) == 250 and isinstance(rows[0]["a"], int)

    def test_stream_is_deterministic(self):
        s = _schema([{"name": "a", "type": "string", "faker": "name"}])
        one = "".join(stream_rows(s, 50, seed=9))
        two = "".join(stream_rows(s, 50, seed=9))
        assert one == two

    def test_stream_matches_batch_generation(self, tmp_path):
        # Streamed CSV equals the batch run_generation CSV for the same seed.
        s = _schema([{"name": "a", "type": "integer", "faker": "random_int"},
                     {"name": "b", "type": "string", "enum": ["x", "y"], "weights": {"x": .6, "y": .4}}])
        streamed = "".join(stream_rows(s, 100, export_format="csv", seed=42))
        run_generation(GenerateConfig(schema=s, rows=100, seed=42, output_dir=str(tmp_path)))
        batch = (Path(tmp_path) / "rows.csv").read_text()
        assert streamed.strip() == batch.strip()
