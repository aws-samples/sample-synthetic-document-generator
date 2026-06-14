# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for offline generation. No mocks, no AWS."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta
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

    def test_relative_date_fields_seed_reproducible_across_clock(self, monkeypatch):
        # Regression: Faker's relative-date providers anchor on the live clock —
        # date_time_this_* / date_of_birth read datetime.now(); date_this_* read
        # date.today() (a SEPARATE module global). A seeded run must freeze BOTH
        # or the same seed yields different absolute dates on different runs.
        # Advance both clocks between two seeded runs and require identical output.

        import faker.providers.date_time as _dt

        s = _schema([
            {"name": "dob", "type": "date", "faker": "date_of_birth"},          # now()
            {"name": "seen", "type": "datetime", "faker": "date_time_this_year"},  # now()
            {"name": "yr", "type": "date", "faker": "date_this_year"},          # today()  ← the gap
            {"name": "decade", "type": "date", "faker": "date_this_decade"},    # today()
        ])
        clock = {"t": datetime(2025, 3, 1, 9, 0, 0)}
        real_dt, real_date = _dt.datetime, _dt.dtdate

        class _AdvancingDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                clock["t"] += timedelta(seconds=37)
                return clock["t"] if tz is None else clock["t"].replace(tzinfo=tz)

        class _AdvancingDate(real_date):
            @classmethod
            def today(cls):
                clock["t"] += timedelta(days=1)   # today() drifts across runs too
                return clock["t"].date()

        monkeypatch.setattr(_dt, "datetime", _AdvancingDT)
        monkeypatch.setattr(_dt, "dtdate", _AdvancingDate)
        a = "".join(stream_rows(s, 30, export_format="csv", seed=7))
        b = "".join(stream_rows(s, 30, export_format="csv", seed=7))
        assert a == b, "seeded relative-date output drifted with the wall clock"

    def test_seeded_timestamps_have_variance(self):
        # Regression: the seeded anchor must be MID-period — a period-start anchor
        # (Jan 1 00:00) collapses date_time_this_year/month to a single instant,
        # emitting the SAME timestamp on every row. Require real spread.
        import csv
        import io
        s = _schema([
            {"name": "y", "type": "datetime", "faker": "date_time_this_year"},
            {"name": "m", "type": "datetime", "faker": "date_time_this_month"},
        ])
        rows = list(csv.DictReader(io.StringIO(
            "".join(stream_rows(s, 100, export_format="csv", seed=42)))))
        assert len({r["y"] for r in rows}) > 50, "date_time_this_year has no variance"
        assert len({r["m"] for r in rows}) > 50, "date_time_this_month has no variance"

    def test_unseeded_run_keeps_live_clock_under_concurrent_seeded(self):
        # Regression: the frozen clock is a PROCESS-GLOBAL patch. An unseeded run
        # must keep the live wall clock even while a seeded run holds the global
        # frozen at the 2025 anchor — earlier the unseeded path took a no-op
        # branch and read the seeded run's frozen 2025 dates (a silent race).
        import io
        import threading
        from datetime import datetime as _dtnow

        s = _schema([{"name": "seen", "type": "datetime", "faker": "date_time_this_year"}])
        this_year = _dtnow.now().year
        results: dict[str, str] = {}

        def seeded(barrier):
            barrier.wait()
            results["seeded"] = "".join(stream_rows(s, 3000, export_format="csv", seed=7))

        def unseeded(barrier):
            barrier.wait()
            results["unseeded"] = "".join(stream_rows(s, 3000, export_format="csv"))

        # Many trials: the race is timing-dependent, so a single pass is not enough.
        for _ in range(25):
            results.clear()
            b = threading.Barrier(2)
            t1 = threading.Thread(target=seeded, args=(b,))
            t2 = threading.Thread(target=unseeded, args=(b,))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            unseeded_rows = list(csv.DictReader(io.StringIO(results["unseeded"])))
            years = {r["seen"][:4] for r in unseeded_rows}
            # The unseeded run must reflect the real current year, never collapse
            # to the seeded anchor (2025) — and 2025 only if that IS this year.
            assert years <= {str(this_year)} or str(this_year) in years, (
                f"unseeded run leaked the frozen clock: years={years}")
            if str(this_year) != "2025":
                assert "2025" not in years or len(years) > 1, (
                    f"unseeded run got the frozen-2025 anchor: years={years}")

    def test_concurrent_seeded_runs_stay_byte_identical(self):
        # Same-mode (seeded) runs share the one installed patch and must remain
        # byte-reproducible under concurrency — the group mutex must not corrupt
        # the shared frozen global across overlapping seeded generations.
        import threading

        s = _schema([
            {"name": "d", "type": "date", "faker": "date_this_year"},
            {"name": "t", "type": "datetime", "faker": "date_time_this_month"},
        ])
        reference = "".join(stream_rows(s, 200, export_format="csv", seed=99))
        outputs: list[str] = []
        lock = threading.Lock()

        def run():
            out = "".join(stream_rows(s, 200, export_format="csv", seed=99))
            with lock:
                outputs.append(out)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(o == reference for o in outputs), "concurrent seeded runs diverged"

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

    def test_json_serializes_decimal_provider(self, tmp_path):
        # Regression: a model-chosen provider returning Decimal (e.g. pydecimal)
        # must JSON-serialize — json.dumps cannot handle Decimal natively.
        s = _schema([{"name": "amt", "type": "number", "faker": "pydecimal",
                      "faker_args": {"left_digits": 5, "right_digits": 2, "positive": True}}])
        res = run_generation(GenerateConfig(schema=s, rows=5, seed=1,
                                            export_format="json", output_dir=str(tmp_path)))
        rows = json.loads(Path(res["output"]["rows_path"]).read_text())
        assert isinstance(rows[0]["amt"], float)
        # stream_rows (the UI /download path) must also not raise.
        text = "".join(stream_rows(s, 5, export_format="json", seed=1))
        assert json.loads(text)

    def test_decimal_csv_and_json_render_identically(self):
        # Regression: a Decimal must serialize to the SAME numeric value in CSV
        # and JSON. CSV used to emit raw str(Decimal) ("123.40") while JSON
        # coerced to float (123.4) — the two formats diverged for the same seed.
        import csv
        import io
        s = _schema([{"name": "amt", "type": "number", "faker": "pydecimal",
                      "faker_args": {"left_digits": 4, "right_digits": 2, "positive": True}}])
        csv_rows = list(csv.DictReader(io.StringIO(
            "".join(stream_rows(s, 20, export_format="csv", seed=3)))))
        json_rows = json.loads("".join(stream_rows(s, 20, export_format="json", seed=3)))
        # Same seed → same draws; the numeric values must match exactly.
        for c, j in zip(csv_rows, json_rows):
            assert float(c["amt"]) == j["amt"]
            assert isinstance(j["amt"], float)
            # CSV carries the float repr, not the Decimal repr (no trailing zero).
            assert c["amt"] == repr(j["amt"])


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


class TestMalformedInputRaisesSchemaError:
    """Bad config / schema must surface as a clean SchemaError up front, not a
    TypeError / Faker exception deep inside row generation."""

    def test_faker_args_not_a_dict(self, tmp_path):
        s = _schema([{"name": "x", "type": "string", "faker": "word",
                      "faker_args": "not-a-dict"}])
        with pytest.raises(SchemaError):
            run_generation(GenerateConfig(schema=s, rows=1, output_dir=str(tmp_path)))

    def test_invalid_export_format_run_generation(self, tmp_path):
        s = _schema([{"name": "x", "type": "string", "faker": "word"}])
        with pytest.raises(SchemaError):
            run_generation(GenerateConfig(schema=s, rows=1, export_format="xml",
                                          output_dir=str(tmp_path)))

    def test_invalid_export_format_stream_rows(self):
        s = _schema([{"name": "x", "type": "string", "faker": "word"}])
        with pytest.raises(SchemaError):
            list(stream_rows(s, 1, export_format="xml"))

    def test_invalid_locale_run_generation(self, tmp_path):
        s = _schema([{"name": "x", "type": "string", "faker": "word"}])
        with pytest.raises(SchemaError):
            run_generation(GenerateConfig(schema=s, rows=1, locale="not_a_locale",
                                          output_dir=str(tmp_path)))

    def test_invalid_locale_stream_rows(self):
        s = _schema([{"name": "x", "type": "string", "faker": "word"}])
        with pytest.raises(SchemaError):
            list(stream_rows(s, 1, locale="not_a_locale"))

    def test_negative_rows_rejected(self, tmp_path):
        s = _schema([{"name": "x", "type": "string", "faker": "word"}])
        with pytest.raises(SchemaError):
            run_generation(GenerateConfig(schema=s, rows=-1, output_dir=str(tmp_path)))


class TestZeroRows:
    """rows=0 is valid: a header-only CSV / empty JSON array, no crash."""

    def test_zero_rows_csv_header_only(self, tmp_path):
        s = _schema([{"name": "a", "type": "integer", "faker": "random_int"}])
        res = run_generation(GenerateConfig(schema=s, rows=0, output_dir=str(tmp_path)))
        assert res["output"]["rows_written"] == 0
        lines = [ln for ln in Path(res["output"]["rows_path"]).read_text().splitlines() if ln.strip()]
        assert lines == ["a"]  # header only

    def test_zero_rows_json_empty_array(self):
        s = _schema([{"name": "a", "type": "integer", "faker": "random_int"}])
        text = "".join(stream_rows(s, 0, export_format="json"))
        assert json.loads(text) == []


class TestRegexGenerationEdgeCases:
    """_regexify must never crash on patterns that stdlib re accepts."""

    @pytest.mark.parametrize("pattern", [
        "[^a-zA-Z0-9_]{3}",     # negated near-full class
        "^[0-9]{3}$",           # anchored
        "(cat|dog|bird)-[0-9]{2}",  # alternation group
        r"\d{3}-\d{4}",          # escapes
        "[A-Z]{2}[0-9]{0,3}",   # zero-or-more bounded
    ])
    def test_regex_patterns_generate_and_self_validate(self, tmp_path, pattern):
        from pocsynth.validate import ValidateConfig, run_validation
        s = _schema([{"name": "code", "type": "string", "regex": pattern}])
        gen = run_generation(GenerateConfig(schema=s, rows=25, seed=7, output_dir=str(tmp_path)))
        # Generated values must validate against their own pattern.
        res = run_validation(ValidateConfig(rows_path=gen["output"]["rows_path"], schema=s))
        assert res["valid"] is True, res["violations"][:3]


class TestWeightedEnumDistribution:
    """A 90/10 weighting must skew the sample, not come out ~uniform."""

    def test_weights_skew_distribution(self, tmp_path):
        s = _schema([{"name": "tier", "type": "string", "enum": ["A", "B"],
                      "weights": {"A": 0.9, "B": 0.1}}])
        res = run_generation(GenerateConfig(schema=s, rows=1000, seed=5, output_dir=str(tmp_path)))
        rows = _read_csv(res["output"]["rows_path"])
        a_frac = sum(1 for r in rows if r["tier"] == "A") / len(rows)
        assert a_frac > 0.8, f"expected ~0.9 A, got {a_frac}"
