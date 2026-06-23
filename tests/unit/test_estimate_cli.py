# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CLI-level tests for `pocsynth estimate`."""

from __future__ import annotations

import json
from pathlib import Path

import fitz
from typer.testing import CliRunner

from pocsynth.cli import app

runner = CliRunner()


def _make_pdf(tmp_path: Path, pages: int = 2) -> Path:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 96), f"Page {i + 1}", fontsize=18)
        page.insert_text((72, 130), "Text for estimation.", fontsize=11)
    p = tmp_path / "test.pdf"
    doc.save(p)
    doc.close()
    return p


class TestEstimateSubcommand:
    def test_help_exits_zero(self):
        r = runner.invoke(app, ["estimate", "--help"])
        assert r.exit_code == 0

    def test_success_envelope_shape(self, tmp_path):
        pdf = _make_pdf(tmp_path, pages=2)
        r = runner.invoke(app, ["--json", "estimate", str(pdf), "--model", "sonnet"])
        assert r.exit_code == 0, r.stderr
        env = json.loads(r.stdout)
        assert env["ok"] is True
        assert env["command"] == "estimate"
        assert env["event"] == "complete"
        result = env["result"]
        assert result["pages"] == 2
        assert result["bedrock"]["model"] == "sonnet"
        assert result["total_cost_usd"] > 0
        assert result["estimate"]["confidence"] == "low"

    def test_no_pii_audit_omits_comprehend(self, tmp_path):
        pdf = _make_pdf(tmp_path, pages=1)
        r = runner.invoke(app, ["--json", "estimate", str(pdf), "--no-pii-audit"])
        assert r.exit_code == 0
        env = json.loads(r.stdout)
        assert env["result"]["comprehend"] is None

    def test_missing_file_exits_3(self, tmp_path):
        r = runner.invoke(app, ["--json", "estimate", str(tmp_path / "nope.pdf")])
        assert r.exit_code == 3
        env = json.loads(r.stdout)
        assert env["error"]["code"] == "INPUT_NOT_FOUND"

    def test_pages_cap_reduces_cost(self, tmp_path):
        pdf = _make_pdf(tmp_path, pages=5)
        r_all = runner.invoke(app, ["--json", "estimate", str(pdf)])
        r_one = runner.invoke(app, ["--json", "estimate", str(pdf), "--pages", "1"])
        cost_all = json.loads(r_all.stdout)["result"]["total_cost_usd"]
        cost_one = json.loads(r_one.stdout)["result"]["total_cost_usd"]
        assert cost_one < cost_all


class TestConvertIncludesCost:
    """After our CLI change, convert's envelope should carry a `cost` block
    populated from bedrock_usage in the result."""

    def test_stubbed_convert_envelope_has_cost(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock, patch
        monkeypatch.chdir(tmp_path)
        pdf = _make_pdf(tmp_path, pages=1)

        bedrock = MagicMock()
        bedrock.converse.return_value = {
            "output": {"message": {"role": "assistant", "content": [{"text": "<p>x</p>"}]}},
            "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
            "stopReason": "end_turn",
        }

        def fake_client(name, *args, **kwargs):
            if name == "bedrock-runtime":
                return bedrock
            return MagicMock()

        fake_session = MagicMock()
        fake_session.client.side_effect = fake_client
        with patch("pocsynth.core.make_session", return_value=fake_session):
            r = runner.invoke(
                app,
                ["--json", "convert", str(pdf), "--pages", "1", "--no-pii-audit"],
            )

        assert r.exit_code == 0, r.stderr
        env = json.loads(r.stdout)
        cost = env["result"]["cost"]
        assert cost is not None
        assert cost["bedrock"]["input_tokens"] == 100
        assert cost["bedrock"]["output_tokens"] == 50
        assert cost["total_cost_usd"] > 0
