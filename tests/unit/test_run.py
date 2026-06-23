# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The one-shot `run` verb: seed routing + safe-by-default gates (ADR-0011).

The free preset path is driven directly. The paid prompt/document paths patch
`make_session` so the chained extract / schema-infer calls run end-to-end with
NO network — exercising the cost gate, the fail-closed verify, and the
not-applicable verdict on synthetic seeds.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import fitz
import pytest

from pocsynth.errors import CostGateError, LeakDetectedError, SchemaError
from pocsynth.run import RunConfig, run_pipeline


# --------------------------------------------------------------------------- #
# stubs (mirrors test_cli_paid.py)
# --------------------------------------------------------------------------- #
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


def _bedrock_extract_client(records, *, mode="discovery"):
    counts: dict[str, dict] = {}
    for rec in records:
        for k, v in rec.items():
            counts.setdefault(k, {})[str(v)] = counts.get(k, {}).get(str(v), 0) + 1
    payload = {"fields": [{"name": k, "type_hint": "string", "value_counts": vc}
                          for k, vc in counts.items()]}
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"role": "assistant",
                               "content": [{"toolUse": {"name": "observe_fields", "input": payload}}]}},
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
    client = MagicMock()

    def _detect(Text="", **_kw):
        if flag_text and flag_text in Text:
            # Whole-value NAME span over the flagged text.
            return {"Entities": [{"Type": "NAME", "Score": 0.99,
                                  "BeginOffset": Text.index(flag_text),
                                  "EndOffset": Text.index(flag_text) + len(flag_text)}]}
        return {"Entities": []}

    client.detect_pii_entities.side_effect = _detect
    return client


def _fake_session(clients: dict):
    sess = MagicMock()
    sess.client.side_effect = lambda name, *a, **k: clients[name]
    return sess


# --------------------------------------------------------------------------- #
# seed-source routing
# --------------------------------------------------------------------------- #
class TestSeedRouting:
    def test_requires_exactly_one_seed_source(self):
        with pytest.raises(SchemaError):
            RunConfig().seed_source()
        with pytest.raises(SchemaError):
            RunConfig(preset="b2b_saas", prompt="x").seed_source()

    def test_preset_is_free_others_paid(self):
        assert RunConfig(preset="b2b_saas").is_paid() is False
        assert RunConfig(prompt="a desc").is_paid() is True
        assert RunConfig(document="x.pdf").is_paid() is True


# --------------------------------------------------------------------------- #
# free preset path — no AWS, synthetic by construction
# --------------------------------------------------------------------------- #
class TestPresetPath:
    def test_preset_generates_and_is_not_applicable(self, tmp_path):
        res = run_pipeline(RunConfig(
            preset="crm_contacts", rows=25, seed=1, output_dir=str(tmp_path)))
        assert res["verdict"] == "not_applicable"
        assert res["cleared_for_sharing"] is True
        assert res["output"]["rows_written"] == 25
        assert res["cost"] is None  # free path reports no cost block
        assert Path(res["output"]["rows_path"]).exists()
        # No paid stack touched: no sample, no attestation.
        assert res["output"]["sample_path"] is None
        assert res["output"]["attestation_path"] is None

    def test_preset_steps_are_load_then_generate(self, tmp_path):
        res = run_pipeline(RunConfig(preset="b2b_saas", rows=10, seed=2, output_dir=str(tmp_path)))
        names = [s["step"] for s in res["steps"]]
        assert names == ["load_preset", "generate"]


# --------------------------------------------------------------------------- #
# cost gate (ADR-0011) — enforced BEFORE any spend
# --------------------------------------------------------------------------- #
class TestCostGate:
    def test_prompt_under_threshold_proceeds(self, tmp_path):
        # A short prompt is pennies → under the $0.10 gate → no --yes needed.
        bedrock = _bedrock_schema_client([
            {"name": "tier", "type": "string", "enum": ["A", "B", "C"]},
            {"name": "n", "type": "integer", "faker": "random_int"}])
        session = _fake_session({"bedrock-runtime": bedrock})
        with patch("pocsynth.schemagen.make_session", return_value=session):
            res = run_pipeline(RunConfig(
                prompt="a small customer table with a tier and a number",
                rows=15, seed=1, output_dir=str(tmp_path)))
        assert res["verdict"] == "not_applicable"  # prompt seed = synthetic
        assert res["cleared_for_sharing"] is True
        assert res["output"]["rows_written"] == 15
        assert res["cost"]["total_cost_usd"] >= 0

    def test_gate_blocks_when_over_threshold_without_yes(self, tmp_path):
        # Force the gate by setting the threshold to 0 (any spend trips it).
        with pytest.raises(CostGateError) as ei:
            run_pipeline(RunConfig(
                prompt="anything", rows=5, cost_threshold=0.0, output_dir=str(tmp_path)))
        assert ei.value.code == "COST_GATE_BLOCKED"
        assert ei.value.exit_code == 2
        assert "estimate" in ei.value.context

    def test_yes_bypasses_the_gate(self, tmp_path):
        bedrock = _bedrock_schema_client([{"name": "x", "type": "string", "faker": "word"}])
        session = _fake_session({"bedrock-runtime": bedrock})
        with patch("pocsynth.schemagen.make_session", return_value=session):
            res = run_pipeline(RunConfig(
                prompt="anything", rows=5, cost_threshold=0.0, assume_yes=True,
                output_dir=str(tmp_path)))
        assert res["output"]["rows_written"] == 5

    def test_no_gate_disables_the_gate(self, tmp_path):
        bedrock = _bedrock_schema_client([{"name": "x", "type": "string", "faker": "word"}])
        session = _fake_session({"bedrock-runtime": bedrock})
        with patch("pocsynth.schemagen.make_session", return_value=session):
            res = run_pipeline(RunConfig(
                prompt="anything", rows=5, cost_threshold=0.0, gate=False,
                output_dir=str(tmp_path)))
        assert res["output"]["rows_written"] == 5


# --------------------------------------------------------------------------- #
# document path — extract → schema → generate → verify (fail-closed)
# --------------------------------------------------------------------------- #
class TestDocumentPath:
    def test_clean_document_passes_and_clears(self, tmp_path):
        # Comprehend flags the claimant (so verify is *applicable* — a real PII
        # value exists), but the PII guard binds it to faker.name and discards
        # the real value, so it never reaches the rows → verify PASSES.
        pdf = _make_pdf(tmp_path, ["Claimant: Pat Lee  Region: NA"])
        bedrock_extract = _bedrock_extract_client([{"claimant": "Pat Lee", "region": "NA"}])
        bedrock_schema = _bedrock_schema_client([
            {"name": "claimant", "type": "string", "faker": "name"},
            {"name": "region", "type": "string", "enum": ["NA", "EMEA"]}])
        comprehend = _comprehend_client(flag_text="Pat Lee")

        sessions = iter([
            _fake_session({"bedrock-runtime": bedrock_extract, "comprehend": comprehend}),
            _fake_session({"bedrock-runtime": bedrock_schema}),
        ])
        with patch("pocsynth.extract.make_session", lambda *a, **k: next(sessions)), \
             patch("pocsynth.schemagen.make_session", lambda *a, **k: next(sessions)):
            res = run_pipeline(RunConfig(
                document=str(pdf), rows=20, seed=1, assume_yes=True,
                output_dir=str(tmp_path)))

        assert res["verdict"] == "pass"
        assert res["cleared_for_sharing"] is True
        assert res["output"]["attestation_path"] is not None
        names = [s["step"] for s in res["steps"]]
        assert names == ["extract", "schema_infer", "generate"]

    def test_leak_fails_closed(self, tmp_path):
        # The fragment hole verify uniquely closes: the PII guard strips the real
        # value from the field's ENUM, but the model also echoed it into the field
        # DESCRIPTION — which the guard does not touch. verify scans the Schema
        # artifact, finds it, and fails closed (exit 8).
        pdf = _make_pdf(tmp_path, ["Claimant: Harold Webb"])
        bedrock_extract = _bedrock_extract_client([{"claimant": "Harold Webb"}])
        bedrock_schema = _bedrock_schema_client([
            {"name": "claimant", "type": "string", "faker": "name",
             "description": "claimant full name, e.g. Harold Webb"}])
        comprehend = _comprehend_client(flag_text="Harold Webb")

        sessions = iter([
            _fake_session({"bedrock-runtime": bedrock_extract, "comprehend": comprehend}),
            _fake_session({"bedrock-runtime": bedrock_schema}),
        ])
        with patch("pocsynth.extract.make_session", lambda *a, **k: next(sessions)), \
             patch("pocsynth.schemagen.make_session", lambda *a, **k: next(sessions)):
            with pytest.raises(LeakDetectedError) as ei:
                run_pipeline(RunConfig(
                    document=str(pdf), rows=10, seed=1, assume_yes=True,
                    output_dir=str(tmp_path)))

        assert ei.value.exit_code == 8
        att = ei.value.context["attestation"]
        assert att["verdict"] == "fail"
        assert "schema" in att["leaks"][0]["where"]
        # The dataset is still written for inspection even though it failed.
        assert Path(ei.value.context["run"]["output"]["rows_path"]).exists()

    def test_share_anyway_overrides_failed_verify(self, tmp_path):
        pdf = _make_pdf(tmp_path, ["Claimant: Harold Webb"])
        bedrock_extract = _bedrock_extract_client([{"claimant": "Harold Webb"}])
        bedrock_schema = _bedrock_schema_client([
            {"name": "claimant", "type": "string", "faker": "name",
             "description": "claimant full name, e.g. Harold Webb"}])
        comprehend = _comprehend_client(flag_text="Harold Webb")

        sessions = iter([
            _fake_session({"bedrock-runtime": bedrock_extract, "comprehend": comprehend}),
            _fake_session({"bedrock-runtime": bedrock_schema}),
        ])
        with patch("pocsynth.extract.make_session", lambda *a, **k: next(sessions)), \
             patch("pocsynth.schemagen.make_session", lambda *a, **k: next(sessions)):
            res = run_pipeline(RunConfig(
                document=str(pdf), rows=10, seed=1, assume_yes=True,
                share_anyway=True, output_dir=str(tmp_path)))

        # Override is honored but recorded; the verdict still says fail.
        assert res["verdict"] == "fail"
        assert res["cleared_for_sharing"] is False
        assert res["override_acknowledged"] is True
