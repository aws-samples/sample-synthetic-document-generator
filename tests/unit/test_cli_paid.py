# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CLI contract tests for the PAID Bedrock/Comprehend commands.

extract / schema --from-sample / pii-audit build their AWS clients from a
shared session (`make_session(...).client(name)`). These tests patch
`make_session` so the commands run end-to-end through the Typer CliRunner with
NO network — exercising the --json envelope, the cost-wiring block, and exit
codes that were previously only covered by the in-process scenario tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import fitz
from typer.testing import CliRunner

from pocsynth.cli import app

runner = CliRunner()


def _stdout_json(result) -> dict:
    assert result.stdout, f"empty stdout; stderr={result.stderr!r}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _make_pdf(tmp_path: Path, lines, name="doc.pdf") -> Path:
    doc = fitz.open()
    page = doc.new_page()
    y = 96
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 22
    p = tmp_path / name
    doc.save(p)
    doc.close()
    return p


def _bedrock_extract_client(records, *, mode="conform"):
    if mode == "conform":
        payload = {"records": records}
        tool = "extract_records"
    else:
        counts: dict[str, dict] = {}
        for rec in records:
            for k, v in rec.items():
                counts.setdefault(k, {})[str(v)] = counts.get(k, {}).get(str(v), 0) + 1
        payload = {"fields": [{"name": k, "type_hint": "string", "value_counts": vc}
                              for k, vc in counts.items()]}
        tool = "observe_fields"
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"role": "assistant",
                               "content": [{"toolUse": {"name": tool, "input": payload}}]}},
        "usage": {"inputTokens": 120, "outputTokens": 40, "totalTokens": 160},
        "stopReason": "tool_use",
    }
    return client


def _bedrock_schema_client(fields):
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"name": "emit_schema",
                         "input": {"schema": 1, "name": "demo", "fields": fields}}}]}},
        "usage": {"inputTokens": 200, "outputTokens": 90, "totalTokens": 290},
        "stopReason": "tool_use",
    }
    return client


def _comprehend_client(*, flag_text=None):
    """Comprehend mock returning a complete PII entity when text contains a marker."""
    client = MagicMock()

    def _detect(Text="", **_kw):
        if flag_text and flag_text in Text:
            return {"Entities": [{"Type": "NAME", "Score": 0.99,
                                  "BeginOffset": 0, "EndOffset": 3}]}
        return {"Entities": []}

    client.detect_pii_entities.side_effect = _detect
    return client


def _fake_session(clients: dict):
    """A session whose .client(name) returns the matching stub."""
    sess = MagicMock()
    sess.client.side_effect = lambda name, *a, **k: clients[name]
    return sess


# --------------------------------------------------------------------------- #
# extract
# --------------------------------------------------------------------------- #
class TestExtractCommand:
    def test_extract_conform_json_envelope_and_cost(self, tmp_path):
        pdf = _make_pdf(tmp_path, ["Claimant: Pat Lee", "Policy: POL-1  Amount: 99.50"])
        schema = tmp_path / "schema.json"
        schema.write_text(json.dumps({"schema": 1, "name": "c", "fields": [
            {"name": "claimant", "type": "string"},
            {"name": "policy", "type": "string"},
            {"name": "amount", "type": "number"},
        ]}))
        bedrock = _bedrock_extract_client(
            [{"claimant": "Pat Lee", "policy": "POL-1", "amount": "99.50"}], mode="conform")
        comprehend = _comprehend_client(flag_text="Pat Lee")
        session = _fake_session({"bedrock-runtime": bedrock, "comprehend": comprehend})

        with patch("pocsynth.extract.make_session", return_value=session):
            result = runner.invoke(app, [
                "--json", "extract", str(pdf), "--schema", str(schema),
                "-o", str(tmp_path / "out")])

        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["ok"] is True and env["command"] == "extract"
        out = env["result"]["output"]
        assert out["records_extracted"] == 1
        assert out["pages_processed"] == 1
        assert out["bedrock_usage"]["input_tokens"] == 120
        # Cost block wired from the post-run token usage.
        assert env["result"]["cost"]["total_cost_usd"] >= 0
        # PII audit flagged the claimant field.
        assert "claimant" in env["result"]["pii_audit"]["pii_fields"]

    def test_extract_discovery_no_pii_audit(self, tmp_path):
        pdf = _make_pdf(tmp_path, ["Category: Widget  Region: US"])
        bedrock = _bedrock_extract_client(
            [{"category": "Widget", "region": "US"}], mode="discovery")
        session = _fake_session({"bedrock-runtime": bedrock})

        with patch("pocsynth.extract.make_session", return_value=session):
            result = runner.invoke(app, [
                "--json", "extract", str(pdf), "--no-pii-audit",
                "-o", str(tmp_path / "out")])

        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["result"]["input"]["mode"] == "discovery"
        assert env["result"]["pii_audit"]["enabled"] is False


# --------------------------------------------------------------------------- #
# schema --from-sample (infer, paid)
# --------------------------------------------------------------------------- #
class TestSchemaInferCommand:
    def test_schema_from_sample_json_envelope_and_cost(self, tmp_path):
        sample = tmp_path / "sample.json"
        sample.write_text(json.dumps({"schema": 1, "source": "x.pdf", "fields": [
            {"name": "region", "type_hint": "string",
             "value_counts": {"NA": 39, "EMEA": 7, "APJ": 2}},
        ]}))
        bedrock = _bedrock_schema_client([
            {"name": "region", "type": "string", "enum": ["NA", "EMEA", "APJ"]}])
        session = _fake_session({"bedrock-runtime": bedrock})

        with patch("pocsynth.schemagen.make_session", return_value=session):
            result = runner.invoke(app, [
                "--json", "schema", "--from-sample", str(sample),
                "--distribution", "infer", "-o", str(tmp_path / "out")])

        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["result"]["input"]["mode"] == "infer"
        # Paid path -> cost block present.
        assert env["result"]["cost"]["total_cost_usd"] >= 0
        schema = json.loads(Path(env["result"]["output"]["schema_path"]).read_text())
        # infer distribution applied the observed frequencies as weights.
        region = next(f for f in schema["fields"] if f["name"] == "region")
        assert region["weights_source"] == "infer"
        assert region["weights"]["NA"] > region["weights"]["EMEA"]


# --------------------------------------------------------------------------- #
# pii-audit
# --------------------------------------------------------------------------- #
class TestPiiAuditCommand:
    def test_pii_audit_json_envelope(self, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("Contact: Jordan Vance at 555-00-1234", encoding="utf-8")
        comprehend = _comprehend_client(flag_text="Jordan Vance")
        session = _fake_session({"comprehend": comprehend})

        with patch("pocsynth.cli.make_session", return_value=session):
            result = runner.invoke(app, ["--json", "pii-audit", str(f)])

        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["command"] == "pii-audit"
        assert env["result"]["pii_audit"]["enabled"] is True
        assert env["result"]["pii_audit"]["entities_found"] >= 1


# --------------------------------------------------------------------------- #
# estimate --for extract|schema (offline)
# --------------------------------------------------------------------------- #
class TestEstimatePaidTargets:
    def test_estimate_for_extract(self, tmp_path):
        pdf = _make_pdf(tmp_path, ["Dense content " * 30])
        result = runner.invoke(app, ["--json", "estimate", str(pdf), "--for", "extract"])
        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["result"]["target"] == "extract"
        assert env["result"]["total_cost_usd"] >= 0

    def test_estimate_for_schema_no_comprehend(self, tmp_path):
        sample = tmp_path / "sample.json"
        sample.write_text('{"schema":1,"fields":[{"name":"a","value_counts":{"x":1}}]}')
        result = runner.invoke(app, ["--json", "estimate", str(sample), "--for", "schema"])
        assert result.exit_code == 0, result.stderr
        env = _stdout_json(result)
        assert env["result"]["target"] == "schema"
        assert env["result"]["comprehend"] is None

    def test_estimate_missing_file_exit_3(self):
        result = runner.invoke(app, ["--json", "estimate", "/no/such.pdf", "--for", "extract"])
        assert result.exit_code == 3
        assert _stdout_json(result)["error"]["code"] in ("INPUT_NOT_FOUND", "INPUT_ERROR")
