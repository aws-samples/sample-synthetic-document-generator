# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CLI contract tests for the structured-data commands (offline, free paths).

generate / test / presets / schema --from-schema touch no AWS, so they are
driven directly through the Typer CliRunner. Covers the --json envelope shape,
exit codes (notably 7 = DATA_INVALID), and the --schema XOR --preset guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pocsynth.cli import app

runner = CliRunner()


def _stdout_json(result) -> dict:
    assert result.stdout, f"empty stdout; stderr={result.stderr!r}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _write_schema(tmp_path: Path, fields, name="t") -> Path:
    p = tmp_path / "schema.json"
    p.write_text(json.dumps({"schema": 1, "name": name, "fields": fields}), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# presets
# --------------------------------------------------------------------------- #
class TestPresets:
    def test_presets_json_lists_bundled(self):
        result = runner.invoke(app, ["--json", "presets"])
        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["ok"] is True and env["command"] == "presets"
        names = {p["name"] for p in env["result"]["presets"]}
        assert {"b2b_saas", "ecommerce_orders", "healthcare_lite"} <= names


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #
class TestGenerate:
    def test_generate_from_preset_json_envelope(self, tmp_path):
        result = runner.invoke(app, [
            "--json", "generate", "--preset", "b2b_saas",
            "--rows", "10", "--seed", "1", "-o", str(tmp_path)])
        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["ok"] is True and env["command"] == "generate"
        assert env["result"]["output"]["rows_written"] == 10
        assert env["result"]["cost"] is None  # free
        assert Path(env["result"]["output"]["rows_path"]).exists()

    def test_generate_from_schema_file(self, tmp_path):
        schema = _write_schema(tmp_path, [{"name": "x", "type": "integer", "faker": "random_int"}])
        result = runner.invoke(app, [
            "--json", "generate", "--schema", str(schema),
            "--rows", "5", "--seed", "2", "-o", str(tmp_path)])
        assert result.exit_code == 0, result.stderr
        assert _stdout_json(result)["result"]["output"]["rows_written"] == 5

    def test_generate_requires_exactly_one_source(self, tmp_path):
        # Neither --schema nor --preset → SchemaError (exit 2).
        r1 = runner.invoke(app, ["--json", "generate", "--rows", "5", "-o", str(tmp_path)])
        assert r1.exit_code == 2
        assert _stdout_json(r1)["error"]["code"] == "SCHEMA_INVALID"
        # Both → also rejected.
        schema = _write_schema(tmp_path, [{"name": "x", "type": "string", "faker": "word"}])
        r2 = runner.invoke(app, [
            "--json", "generate", "--preset", "b2b_saas",
            "--schema", str(schema), "-o", str(tmp_path)])
        assert r2.exit_code == 2

    def test_generate_invalid_format_exits_2(self, tmp_path):
        schema = _write_schema(tmp_path, [{"name": "x", "type": "string", "faker": "word"}])
        result = runner.invoke(app, [
            "--json", "generate", "--schema", str(schema),
            "--format", "csv", "--locale", "not_a_locale", "-o", str(tmp_path)])
        # Invalid locale surfaces as SchemaError → exit 2, not a raw traceback.
        assert result.exit_code == 2
        assert _stdout_json(result)["error"]["code"] == "SCHEMA_INVALID"


# --------------------------------------------------------------------------- #
# test (validation) — exit code 7
# --------------------------------------------------------------------------- #
class TestValidateCommand:
    def test_valid_rows_exit_0(self, tmp_path):
        schema = _write_schema(tmp_path, [{"name": "x", "type": "integer", "faker": "random_int"}])
        runner.invoke(app, [
            "--json", "generate", "--schema", str(schema),
            "--rows", "10", "--seed", "1", "-o", str(tmp_path)])
        rows = tmp_path / "rows.csv"
        result = runner.invoke(app, [
            "--json", "test", "--rows", str(rows), "--schema", str(schema)])
        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["result"]["valid"] is True

    def test_invalid_rows_exit_7(self, tmp_path):
        schema = _write_schema(tmp_path, [{"name": "tier", "type": "string", "enum": ["A", "B"]}])
        rows = tmp_path / "rows.csv"
        rows.write_text("tier\nA\nZZ\n", encoding="utf-8")  # 'ZZ' violates the enum
        result = runner.invoke(app, [
            "--json", "test", "--rows", str(rows), "--schema", str(schema)])
        assert result.exit_code == 7
        env = _stdout_json(result)
        assert env["error"]["code"] == "DATA_INVALID"
        # The full report rides along in the error context for agents/CI.
        assert env["error"]["context"]["report"]["valid"] is False

    def test_malformed_json_rows_exit_2(self, tmp_path):
        schema = _write_schema(tmp_path, [{"name": "x", "type": "string"}])
        rows = tmp_path / "rows.json"
        rows.write_text("{not valid json", encoding="utf-8")
        result = runner.invoke(app, [
            "--json", "test", "--rows", str(rows), "--schema", str(schema)])
        assert result.exit_code == 2  # clean SchemaError, not a traceback
        assert _stdout_json(result)["error"]["code"] == "SCHEMA_INVALID"


# --------------------------------------------------------------------------- #
# schema --from-schema (lint, offline)
# --------------------------------------------------------------------------- #
class TestSchemaLint:
    def test_lint_clean_schema_no_cost(self, tmp_path):
        schema = _write_schema(tmp_path, [
            {"name": "x", "type": "string", "faker": "word", "description": "a word"}])
        result = runner.invoke(app, [
            "--json", "schema", "--from-schema", str(schema), "-o", str(tmp_path)])
        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["command"] == "schema"
        assert env["result"]["input"]["mode"] == "lint"
        assert env["result"]["cost"] is None  # offline lint is free

    def test_lint_flags_pii_enum_and_fix_strips_it(self, tmp_path):
        # A PII provider with a literal enum must be flagged; --fix strips it.
        schema = _write_schema(tmp_path, [
            {"name": "ssn", "type": "string", "faker": "ssn", "enum": ["111-11-1111"]}])
        result = runner.invoke(app, [
            "--json", "schema", "--from-schema", str(schema), "--fix", "-o", str(tmp_path)])
        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["result"]["lint"]["issues_total"] >= 1
        fixed = json.loads((tmp_path / "schema.fixed.json").read_text())
        assert "enum" not in fixed["fields"][0]

    def test_schema_requires_exactly_one_source(self):
        result = runner.invoke(app, ["--json", "schema"])
        assert result.exit_code == 2
        assert _stdout_json(result)["error"]["code"] == "SCHEMA_INVALID"


# --------------------------------------------------------------------------- #
# --stream invariant for generate
# --------------------------------------------------------------------------- #
class TestGenerateStream:
    def test_stream_emits_ndjson_then_complete(self, tmp_path):
        schema = _write_schema(tmp_path, [{"name": "x", "type": "string", "faker": "word"}])
        result = runner.invoke(app, [
            "--json", "--stream", "generate", "--schema", str(schema),
            "--rows", "5", "--seed", "1", "-o", str(tmp_path)])
        assert result.exit_code == 0, result.stderr
        lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
        parsed = [json.loads(ln) for ln in lines]
        assert all("event" in p for p in parsed)
        assert parsed[-1]["event"] == "complete"

    def test_stream_complete_envelope_equals_nonstream(self, tmp_path):
        """Gap 4: the streamed final 'complete' event must carry the same
        envelope as the plain --json run (modulo the wall-time field, which is
        timing-dependent). Both use seed=1 so the result payload is identical."""
        schema = _write_schema(tmp_path, [
            {"name": "x", "type": "string", "faker": "word"},
            {"name": "n", "type": "integer", "faker": "random_int"}])

        nonstream = runner.invoke(app, [
            "--json", "generate", "--schema", str(schema),
            "--rows", "20", "--seed", "1", "-o", str(tmp_path / "a")])
        streamed = runner.invoke(app, [
            "--json", "--stream", "generate", "--schema", str(schema),
            "--rows", "20", "--seed", "1", "-o", str(tmp_path / "b")])
        assert nonstream.exit_code == 0 and streamed.exit_code == 0

        ns_env = _stdout_json(nonstream)  # single JSON object
        complete = [json.loads(ln) for ln in streamed.stdout.strip().splitlines()
                    if ln.strip()][-1]

        # Same top-level contract fields.
        for key in ("ok", "schema", "command", "event"):
            assert ns_env[key] == complete[key], key
        assert complete["event"] == "complete"

        # Same result payload, normalizing the timing + output-dir fields.
        def _norm(env):
            r = json.loads(json.dumps(env["result"]))
            r["output"].pop("wall_time_seconds", None)
            r["output"].pop("dir", None)
            r["output"].pop("rows_path", None)
            return r
        assert _norm(ns_env) == _norm(complete)


class TestVerifyCommand:
    """F1 / ADR-0010: verify is fail-closed (exit 8) on a real-value leak."""

    def _sample(self, tmp_path):
        p = tmp_path / "sample.json"
        p.write_text(json.dumps({"schema": 1, "source": "intake.pdf", "fields": [
            {"name": "ssn", "pii": True, "value_counts": {"555-22-7788": 1}}]}))
        return p

    def test_clean_rows_pass_exit_0(self, tmp_path):
        sample = self._sample(tmp_path)
        rows = tmp_path / "rows.csv"
        rows.write_text("ssn\n111-00-0000\n")
        result = runner.invoke(app, [
            "--json", "verify", "--rows", str(rows), "--sample", str(sample),
            "-o", str(tmp_path)])
        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["command"] == "verify"
        assert env["result"]["verdict"] == "pass"
        assert (tmp_path / "attestation.json").exists()

    def test_leak_fails_closed_exit_8(self, tmp_path):
        sample = self._sample(tmp_path)
        rows = tmp_path / "rows.csv"
        rows.write_text("ssn\n555-22-7788\n")  # real value leaked
        result = runner.invoke(app, [
            "--json", "verify", "--rows", str(rows), "--sample", str(sample)])
        assert result.exit_code == 8
        env = _stdout_json(result)
        assert env["error"]["code"] == "PII_LEAK_DETECTED"
        # the attestation rides in the error context, masked
        assert env["error"]["context"]["attestation"]["verdict"] == "fail"
        assert "555-22-7788" not in result.stdout

    def test_schema_leak_detected(self, tmp_path):
        sample = self._sample(tmp_path)
        rows = tmp_path / "rows.csv"
        rows.write_text("ssn\n111-00-0000\n")  # rows clean
        schema = tmp_path / "schema.json"
        schema.write_text(json.dumps({"schema": 1, "name": "t", "fields": [
            {"name": "ssn", "type": "string", "enum": ["555-22-7788"]}]}))  # leak in schema
        result = runner.invoke(app, [
            "--json", "verify", "--rows", str(rows), "--sample", str(sample),
            "--schema", str(schema)])
        assert result.exit_code == 8


class TestRunCommand:
    """F2 / ADR-0011: one-shot `run`. Free preset path + code-enforced cost gate."""

    def test_preset_path_one_shot(self, tmp_path):
        result = runner.invoke(app, [
            "--json", "run", "--preset", "crm_contacts",
            "--rows", "30", "--seed", "1", "-o", str(tmp_path)])
        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["command"] == "run"
        r = env["result"]
        assert r["verdict"] == "not_applicable"  # synthetic by construction
        assert r["cleared_for_sharing"] is True
        assert r["output"]["rows_written"] == 30
        assert r["cost"] is None  # free path
        assert Path(r["output"]["rows_path"]).exists()

    def test_requires_exactly_one_seed_source(self, tmp_path):
        # Neither source.
        r1 = runner.invoke(app, ["--json", "run", "--rows", "5", "-o", str(tmp_path)])
        assert r1.exit_code == 2
        assert _stdout_json(r1)["error"]["code"] == "SCHEMA_INVALID"
        # Two sources.
        r2 = runner.invoke(app, [
            "--json", "run", "--preset", "b2b_saas",
            "--prompt", "x", "-o", str(tmp_path)])
        assert r2.exit_code == 2

    def test_gate_flags_accepted(self, tmp_path):
        # The safe-by-default flags parse and run on the free path (the gate's
        # threshold logic itself is unit-tested in test_run.py). --no-gate is a
        # no-op on a free preset and still succeeds.
        result = runner.invoke(app, [
            "--json", "run", "--preset", "b2b_saas", "--no-gate", "--yes",
            "--rows", "5", "--seed", "1", "-o", str(tmp_path)])
        assert result.exit_code == 0, result.stderr

    def test_document_egress_notice_omits_comprehend_when_no_pii_audit(self, tmp_path):
        # The human-mode egress notice is printed BEFORE any file/AWS work, so a
        # nonexistent path still emits it (the pipeline then fails on input).
        # Regression: the notice must name only the services that actually run —
        # --no-pii-audit skips Comprehend, so it must NOT be advertised.
        missing = str(tmp_path / "nope.pdf")
        with_audit = runner.invoke(app, ["run", "--document", missing,
                                         "-o", str(tmp_path)])
        assert "Amazon Bedrock (extraction)" in with_audit.stderr
        assert "Amazon Comprehend (PII audit)" in with_audit.stderr
        without = runner.invoke(app, ["run", "--document", missing,
                                      "--no-pii-audit", "-o", str(tmp_path)])
        assert "Amazon Bedrock (extraction)" in without.stderr
        assert "Amazon Comprehend" not in without.stderr


@pytest.mark.parametrize("cmd", [
    ["generate", "--help"], ["test", "--help"], ["verify", "--help"],
    ["run", "--help"],
    ["schema", "--help"], ["presets", "--help"], ["estimate", "--help"],
])
def test_structured_help_exits_zero(cmd):
    result = runner.invoke(app, cmd)
    assert result.exit_code == 0, result.stderr
