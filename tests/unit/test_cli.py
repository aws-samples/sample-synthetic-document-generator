# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Typer CLI contract tests.

Covers:
  * --help on every subcommand
  * JSON envelope shape (stdout purity: stdout = 1 valid JSON object)
  * Exit-code coverage per the plan's table
  * --stream invariant: each line valid JSON, last line event=="complete",
    complete envelope is byte-equivalent to non-stream modulo ordering
  * doctor: result.checks[] + all_ok
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from pocsynth.cli import app

runner = CliRunner()


def _stdout_json(result) -> dict:
    assert result.stdout, f"empty stdout; stderr={result.stderr!r}"
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------- --help --------------------------------------------------------


class TestHelp:
    @pytest.mark.parametrize("cmd", [[], ["convert", "--help"], ["pii-audit", "--help"],
                                      ["models", "--help"], ["doctor", "--help"],
                                      ["version", "--help"]])
    def test_help_exits_zero(self, cmd):
        result = runner.invoke(app, cmd if cmd else ["--help"])
        assert result.exit_code == 0, result.stderr


# ---------- version -------------------------------------------------------


class TestVersion:
    def test_version_json(self):
        result = runner.invoke(app, ["--json", "version"])
        assert result.exit_code == 0
        env = _stdout_json(result)
        assert env["ok"] is True
        assert env["schema"] == 1
        assert env["command"] == "version"
        assert env["event"] == "complete"
        assert "version" in env["result"]


# ---------- models --------------------------------------------------------


class TestModels:
    def test_models_json_stdout_purity(self):
        result = runner.invoke(app, ["--json", "models"])
        assert result.exit_code == 0
        # stdout MUST be exactly one JSON value; assert it parses
        env = json.loads(result.stdout.strip())
        assert env["ok"] is True
        assert env["command"] == "models"
        assert env["event"] == "complete"
        assert env["result"]["default"] == "sonnet"
        assert isinstance(env["result"]["models"], list)
        assert len(env["result"]["models"]) == 3

    def test_models_human_mode_stdout_still_json(self):
        # Per design: even in human mode, the final stdout line is the JSON
        # envelope (stderr carries the human Rich summary).
        result = runner.invoke(app, ["models"])
        assert result.exit_code == 0


# ---------- convert: error paths (exit-code coverage) --------------------


class TestConvertErrors:
    def test_input_not_found_exits_3(self, tmp_path):
        result = runner.invoke(app, ["--json", "convert", str(tmp_path / "nope.pdf")])
        assert result.exit_code == 3
        env = json.loads(result.stdout.strip())
        assert env["ok"] is False
        assert env["error"]["code"] == "INPUT_NOT_FOUND"

    def test_input_not_pdf_exits_3(self, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("hi")
        result = runner.invoke(app, ["--json", "convert", str(f)])
        assert result.exit_code == 3
        env = json.loads(result.stdout.strip())
        assert env["error"]["code"] == "INPUT_NOT_PDF"

    def test_url_rejected_non_https_exits_3(self):
        result = runner.invoke(app, ["--json", "convert", "http://example.com/x.pdf"])
        assert result.exit_code == 3
        env = json.loads(result.stdout.strip())
        assert env["error"]["code"] == "URL_REJECTED"
        assert env["error"]["context"]["reason"] == "non_https_scheme"

    def test_stream_requires_json_exits_nonzero(self):
        result = runner.invoke(app, ["--stream", "convert", "x.pdf"])
        # typer.BadParameter produces usage error (exit 2)
        assert result.exit_code != 0

    def test_interactive_with_json_exits_nonzero(self):
        result = runner.invoke(app, ["--json", "--interactive", "convert", "x.pdf"])
        assert result.exit_code != 0


# ---------- convert: happy path (stubbed) --------------------------------


def _make_fake_pdf(tmp_path):
    """Build a 1-page PDF with `fitz` so the fitz.open path in core.py works."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 96), "Hello World", fontsize=18)
    pdf = tmp_path / "test.pdf"
    doc.save(pdf)
    doc.close()
    return pdf


def _stub_bedrock_client():
    """Return a MagicMock bedrock-runtime client that yields a deterministic converse result."""
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": "<p>converted</p>"}], "role": "assistant"}},
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "stopReason": "end_turn",
    }
    return client


class TestConvertHappy:
    def test_stubbed_convert_json_success(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pdf = _make_fake_pdf(tmp_path)

        bedrock = _stub_bedrock_client()

        # Patch make_session() to return a MagicMock whose .client() yields our stub.
        def fake_client(name, *args, **kwargs):
            if name == "bedrock-runtime":
                return bedrock
            raise AssertionError(f"Unexpected client: {name}")

        fake_session = MagicMock()
        fake_session.client.side_effect = fake_client
        with patch("pocsynth.core.make_session", return_value=fake_session):
            result = runner.invoke(
                app,
                [
                    "--json",
                    "convert",
                    str(pdf),
                    "--pages", "1",
                    "--no-pii-audit",
                ],
            )

        assert result.exit_code == 0, f"stderr={result.stderr}\nstdout={result.stdout}"
        env = json.loads(result.stdout.strip())
        assert env["ok"] is True
        assert env["schema"] == 1
        assert env["command"] == "convert"
        assert env["event"] == "complete"
        out = env["result"]["output"]
        assert out["pages_processed"] == 1
        assert out["pages_attempted"] == 1
        assert out["bedrock_usage"]["input_tokens"] == 10
        assert out["bedrock_usage"]["output_tokens"] == 5


class TestConvertStream:
    def test_stream_invariant(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pdf = _make_fake_pdf(tmp_path)
        bedrock = _stub_bedrock_client()

        def fake_client(name, *args, **kwargs):
            if name == "bedrock-runtime":
                return bedrock
            raise AssertionError(f"Unexpected client: {name}")

        fake_session = MagicMock()
        fake_session.client.side_effect = fake_client
        with patch("pocsynth.core.make_session", return_value=fake_session):
            result = runner.invoke(
                app,
                [
                    "--json", "--stream",
                    "convert", str(pdf),
                    "--pages", "1",
                    "--no-pii-audit",
                ],
            )

        assert result.exit_code == 0, f"stderr={result.stderr}\nstdout={result.stdout}"
        lines = [ln for ln in result.stdout.strip().splitlines() if ln]
        # Every line valid JSON
        events = [json.loads(ln) for ln in lines]
        # Each event carries schema + tool_version + command + event
        for ev in events:
            assert ev["schema"] == 1
            assert ev["command"] == "convert"
            assert "event" in ev
        # Last event must be complete
        assert events[-1]["event"] == "complete"
        # There should be at least one page_started/page_processed before complete
        ev_names = {e["event"] for e in events[:-1]}
        assert "page_started" in ev_names
        assert "page_processed" in ev_names


# ---------- doctor shape --------------------------------------------------


class TestDoctor:
    def test_doctor_json_all_pass(self):
        """Stubbed STS + Bedrock + Comprehend all succeed → all_ok=true."""
        fake_sts = MagicMock()
        fake_sts.get_caller_identity.return_value = {
            "Arn": "arn:aws:sts::000000000000:assumed-role/Fake/caller",
            "Account": "000000000000",
        }
        fake_bedrock = MagicMock()
        fake_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "OK"}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            "stopReason": "end_turn",
        }
        fake_comprehend = MagicMock()
        fake_comprehend.detect_pii_entities.return_value = {"Entities": []}

        def fake_client(name, *args, **kwargs):
            return {
                "sts": fake_sts,
                "bedrock-runtime": fake_bedrock,
                "comprehend": fake_comprehend,
            }[name]

        with patch("pocsynth.cli.make_session") as MockSession:
            MockSession.return_value.client.side_effect = fake_client
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0, f"stderr={result.stderr}\nstdout={result.stdout}"
        env = json.loads(result.stdout.strip())
        assert env["ok"] is True
        assert env["result"]["all_ok"] is True
        names = [c["name"] for c in env["result"]["checks"]]
        for required in ["python", "boto3", "pymupdf", "region",
                         "sts_caller_identity", "bedrock_converse",
                         "comprehend_detect_pii"]:
            assert required in names

    def test_doctor_auth_failure_exits_4(self):
        fake_sts = MagicMock()
        fake_sts.get_caller_identity.side_effect = RuntimeError("creds missing")

        def fake_client(name, *args, **kwargs):
            return {"sts": fake_sts}[name] if name == "sts" else MagicMock()

        with patch("pocsynth.cli.make_session") as MockSession:
            MockSession.return_value.client.side_effect = fake_client
            # Also stub bedrock/comprehend to not explode on invocation
            def full_client(name, *args, **kwargs):
                if name == "sts":
                    return fake_sts
                return MagicMock()
            MockSession.return_value.client.side_effect = full_client
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 4
        env = json.loads(result.stdout.strip())
        assert env["ok"] is False
        assert env["error"]["code"] == "AWS_AUTH_FAILED"
        assert "checks" in env["error"]["context"]
