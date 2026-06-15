# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Live AWS smoke tests.

These tests make real calls to Bedrock and Comprehend and require:
    - AWS credentials with permissions listed in README.md
    - Network access
    - (for Bedrock) model access granted in the target region

Run explicitly with:
    pytest -m live
or with the helper:
    pytest tests/live -m live
"""
import os

import boto3
import fitz
import pytest

from pocsynth.bedrock import MODELS, process_page
from pocsynth.comprehend import scan_for_pii
from pocsynth.prompts import build_prompt

pytestmark = pytest.mark.live


REGION = os.environ.get("AWS_REGION", "us-east-1")


@pytest.fixture(scope="module")
def bedrock_client():
    return boto3.client("bedrock-runtime", region_name=REGION)


@pytest.fixture(scope="module")
def comprehend_client():
    return boto3.client("comprehend", region_name=REGION)


def test_bedrock_converse_sonnet_46(bedrock_client):
    model_id = MODELS["sonnet"]["id"]
    resp = bedrock_client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": "Reply with exactly: OK"}]}],
        inferenceConfig={"maxTokens": 20, "temperature": 0},
    )
    text = resp["output"]["message"]["content"][0]["text"]
    assert "OK" in text


def test_comprehend_detects_obvious_pii(tmp_path, monkeypatch, comprehend_client):
    """True positive: obviously-sensitive text yields at least one PII entity."""
    monkeypatch.chdir(tmp_path)
    text = "Call John Smith at 206-555-0123 or email john@example.com."
    detected = scan_for_pii(
        text, folder_name="pii-audit", filename="live_positive",
        comprehend=comprehend_client,
    )
    types = {entry["Type"] for entry in detected}
    assert types, "Comprehend returned no PII for obviously sensitive text"


def test_process_page_multimodal_live(bedrock_client, tmp_path):
    """Full per-page path: render a synthetic PDF with fitz, send text+PNG to Sonnet 4.6."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 96), "Acme Invoice #42", fontsize=18)
    page.insert_text((72, 130), "Total due: $123.45", fontsize=12)
    pdf_path = tmp_path / "sample.pdf"
    doc.save(pdf_path)
    doc.close()

    with fitz.open(pdf_path) as rendered:
        page = rendered.load_page(0)
        img_bytes = page.get_pixmap().tobytes()

        result = process_page(
            bedrock_client,
            MODELS["sonnet"]["id"],
            [{"text": "You convert document image to html."}],
            build_prompt(synthetic=False, export_format="HTML"),
            page,
            page_num=0,
            img_bytes=img_bytes,
            max_tokens=1000,
        )

    assert isinstance(result, dict)
    text = result["text"]
    assert isinstance(text, str) and text.strip(), "Empty response from Bedrock"
    lowered = text.lower()
    assert "acme" in lowered or "invoice" in lowered or "42" in text, (
        f"Model output did not reflect page content: {text[:500]}"
    )
    assert result["usage"]["input_tokens"] > 0
    assert result["usage"]["output_tokens"] > 0


def test_comprehend_returns_no_pii_for_neutral_text(tmp_path, monkeypatch, comprehend_client):
    """True negative: neutral text yields no PII entities and no CSV rows."""
    monkeypatch.chdir(tmp_path)
    text = (
        "The quarterly report summarizes performance across the three "
        "business units. Revenue grew modestly while operating costs held steady."
    )
    detected = scan_for_pii(
        text, folder_name="pii-audit", filename="live_negative",
        comprehend=comprehend_client,
    )
    assert detected == [], f"Unexpected PII detected in neutral text: {detected}"

    # Audit file exists with only the header row when nothing was detected
    # (the header makes the artifact parseable by csv / pandas consumers
    # without needing a "file exists?" branch).
    from pathlib import Path
    audit = Path("pii-audit/live_negative_pii_scan_audit.csv")
    assert audit.exists()
    rows = audit.read_text(encoding="utf-8").strip().splitlines()
    assert rows == [
        "FileName,PageNumber,Type,Score,BeginOffset,EndOffset,Value"
    ]


# ---------- Extended live coverage (A-D + multi-page) -------------------


def _make_live_fixture_pdf(tmp_path, pages: int, name: str = "live.pdf"):
    """Write a tiny deterministic PDF for live-convert tests."""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 96), f"Synthetic Contract (page {i + 1} of {pages})", fontsize=16)
        page.insert_text(
            (72, 130),
            "This is synthetic placeholder text used for live conversion tests.",
            fontsize=11,
        )
        page.insert_text((72, 150), "No real PII; no real terms.", fontsize=11)
    pdf = tmp_path / name
    doc.save(pdf)
    doc.close()
    return pdf


def test_convert_end_to_end_with_cost(tmp_path, monkeypatch):
    """A — full run_conversion happy path with real Bedrock + Comprehend.

    Covers the wiring between process_page, combine, PII audit, and the
    actual_convert_cost block that the CLI injects.
    """
    from pocsynth.core import ConversionConfig, run_conversion
    from pocsynth.pricing import actual_convert_cost, load_pricing

    monkeypatch.chdir(tmp_path)
    pdf = _make_live_fixture_pdf(tmp_path, pages=1, name="live_cost.pdf")

    cfg = ConversionConfig(
        pdf_url=str(pdf),
        model_key="sonnet",
        export_format="html",
        num_pages=1,
        pii_audit=True,
        region=REGION,
    )
    result = run_conversion(cfg)
    cost = actual_convert_cost(result, load_pricing(), model_key="sonnet", region=REGION)

    # Structural assertions on the conversion
    assert result["output"]["pages_processed"] == 1
    assert result["output"]["pages_attempted"] == 1
    from pathlib import Path
    combined = Path(result["output"]["combined_path"])
    assert combined.exists()
    assert combined.read_text(encoding="utf-8").strip(), "Combined HTML file is empty"

    # Cost block
    assert cost["bedrock"]["input_tokens"] > 0
    assert cost["bedrock"]["output_tokens"] > 0
    assert cost["bedrock"]["total_cost_usd"] > 0
    assert cost["total_cost_usd"] > 0
    # Comprehend included because pii_audit=True
    assert cost["comprehend"] is not None
    assert cost["comprehend"]["units"] >= 3  # at least the 3-unit minimum
    assert cost["estimate"]["confidence"] == "actual"


def test_multipage_convert_live(tmp_path, monkeypatch):
    """+1 — multi-page convert is a real use case; exercise the page loop live.

    Smallest possible multi-page run (2 pages) to catch regressions in the
    page loop, HTML <section> concatenation, and token aggregation.
    """
    from pocsynth.core import ConversionConfig, run_conversion
    from pocsynth.pricing import actual_convert_cost, load_pricing

    monkeypatch.chdir(tmp_path)
    pdf = _make_live_fixture_pdf(tmp_path, pages=2, name="live_multi.pdf")

    cfg = ConversionConfig(
        pdf_url=str(pdf),
        model_key="sonnet",
        export_format="html",
        num_pages=2,
        pii_audit=False,  # skip Comprehend to keep the test cheap
        region=REGION,
    )
    result = run_conversion(cfg)
    cost = actual_convert_cost(result, load_pricing(), model_key="sonnet", region=REGION)

    out = result["output"]
    assert out["pages_attempted"] == 2
    assert out["pages_processed"] == 2, f"page failures: {result.get('page_failures')}"
    assert len(out["per_page_paths"]) == 2
    assert len(out["per_page_images"]) == 2

    from pathlib import Path
    combined = Path(out["combined_path"]).read_text(encoding="utf-8")
    # Combined doc must be well-formed: exactly one DOCTYPE/html/body
    assert combined.count("<!DOCTYPE html>") == 1
    assert combined.count("<html") == 1
    assert combined.count("<body") == 1
    # And exactly two <section> blocks, one per page
    assert combined.count("<section") == 2

    # Token aggregation: both pages contributed tokens
    usage = out["bedrock_usage"]
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0
    assert cost["bedrock"]["total_cost_usd"] > 0


def test_doctor_json_all_pass_live():
    """B — invoke `pocsynth --json doctor` in a subprocess, assert all_ok=true.

    This exercises the real STS/Bedrock/Comprehend probes through the CLI
    wrapper (not the stubbed CliRunner path) — catches IAM regressions the
    stubbed doctor test can't.
    """
    import json
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pocsynth", "--json", "doctor"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}\nstdout:\n{result.stdout[:500]}"
    env = json.loads(result.stdout)
    assert env["ok"] is True
    assert env["command"] == "doctor"
    assert env["result"]["all_ok"] is True
    names = {c["name"] for c in env["result"]["checks"]}
    for required in ("python", "boto3", "pymupdf", "region",
                     "sts_caller_identity", "bedrock_converse",
                     "comprehend_detect_pii"):
        assert required in names, f"doctor missing check: {required}"


def test_bedrock_error_translation_live(bedrock_client):
    """C — request a bogus model ID, assert translate_aws_error maps it sanely.

    Exercises the ClientError → UpstreamError branch of translate_aws_error
    against real AWS; no tokens billed because validation fails before the
    model sees input.
    """
    from botocore.exceptions import ClientError

    from pocsynth.bedrock import translate_aws_error
    from pocsynth.errors import UpstreamError

    try:
        bedrock_client.converse(
            modelId="anthropic.definitely-not-a-real-model-v99",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"maxTokens": 5, "temperature": 0},
        )
        raise AssertionError("Expected Bedrock to reject the bogus model ID")
    except ClientError as exc:
        translated = translate_aws_error(exc, service="bedrock")
        assert isinstance(translated, UpstreamError)
        assert translated.code == "BEDROCK_ERROR"
        assert "boto3_code" in translated.context
        # Real Bedrock returns ValidationException for unknown model IDs;
        # we don't hardcode the exact string since AWS may change it, but
        # a non-empty value is the contract.
        assert translated.context["boto3_code"]


@pytest.mark.parametrize("model_key", ["sonnet", "opus", "haiku"])
def test_estimate_model_ids_usable_live(bedrock_client, tmp_path, monkeypatch, model_key):
    """D — every model_id in pricing.json is actually invocable on live Bedrock.

    Smoke-tests each model with a minimal Converse call (5 tokens out,
    fractions of a cent). Catches drift between pricing.json's model_id
    values and the inference profiles the account can reach.

    NOTE: if this fails for a specific model, don't mask it — either the
    pricing.json entry is stale (fix it), the account lacks access to that
    model (grant access or remove it from MODELS), or Bedrock renamed the
    profile (refresh pricing.json). The test failure is the signal.
    """
    import json
    import subprocess
    import sys

    monkeypatch.chdir(tmp_path)
    pdf = _make_live_fixture_pdf(tmp_path, pages=1, name="estimate_check.pdf")

    est = subprocess.run(
        [sys.executable, "-m", "pocsynth", "--json", "estimate",
         str(pdf), "--model", model_key],
        capture_output=True, text=True, check=False,
    )
    assert est.returncode == 0, est.stderr
    env = json.loads(est.stdout)
    model_id = env["result"]["bedrock"]["model_id"]
    assert model_id, f"estimate envelope for {model_key} did not carry a model_id"

    try:
        bedrock_client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "Say OK"}]}],
            inferenceConfig={"maxTokens": 5, "temperature": 0},
        )
    except Exception as exc:
        raise AssertionError(
            f"pricing.json's {model_key} model_id {model_id!r} is NOT "
            f"invocable on live Bedrock: {exc}. Refresh pricing.json "
            f"(and bedrock.py MODELS) — the real profile ID has changed "
            f"or this account lacks model access."
        ) from exc


def _make_sparse_fixture_pdf(tmp_path, pages: int, name: str = "sparse.pdf"):
    """Sparse fixture: 3 lines per page. Represents cover pages, title
    pages, or a short form. Stresses the heuristic's lower bound."""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 96), f"Cover (page {i + 1})", fontsize=16)
        page.insert_text((72, 130), "Short content.", fontsize=11)
    pdf = tmp_path / name
    doc.save(pdf)
    doc.close()
    return pdf


def _make_dense_fixture_pdf(tmp_path, pages: int, name: str = "dense.pdf"):
    """Dense fixture: 30 lines of contract-ish lorem per page. Represents
    a typical contract or agreement page — realistic output length."""
    doc = fitz.open()
    lorem = (
        "This section of the agreement outlines the terms under which "
        "services are provided. The parties agree that the synthetic "
        "content in this document is generated for testing purposes only. "
        "No real individuals, organizations, contracts, or financial "
        "instruments are represented herein. Payment terms, delivery "
        "schedules, liability limits, and warranty provisions are all "
        "placeholder values. "
    )
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Agreement (page {i + 1} of {pages})", fontsize=14)
        y = 100
        for _ in range(30):
            page.insert_text((72, y), lorem, fontsize=10)
            y += 14
    pdf = tmp_path / name
    doc.save(pdf)
    doc.close()
    return pdf


def _make_mixed_fixture_pdf(tmp_path, name: str = "mixed.pdf"):
    """Mixed fixture: page 1 sparse (cover), pages 2-4 dense (body).
    Represents a real-world multi-page document where density varies."""
    doc = fitz.open()
    lorem = (
        "Terms and conditions for synthetic processing follow. "
        "All references to parties or entities are placeholder values. "
    )
    # Cover page — sparse
    cover = doc.new_page()
    cover.insert_text((72, 200), "Cover Page", fontsize=24)
    cover.insert_text((72, 240), "Synthetic Document", fontsize=14)
    # Body pages — dense
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Section {i + 1}", fontsize=14)
        y = 100
        for _ in range(28):
            page.insert_text((72, y), lorem, fontsize=10)
            y += 14
    pdf = tmp_path / name
    doc.save(pdf)
    doc.close()
    return pdf


@pytest.mark.parametrize(
    "fixture_name,fixture_factory,pages",
    [
        ("sparse-2p", lambda tp: _make_sparse_fixture_pdf(tp, pages=2, name="sparse.pdf"), 2),
        ("dense-2p", lambda tp: _make_dense_fixture_pdf(tp, pages=2, name="dense.pdf"), 2),
        ("mixed-4p", lambda tp: _make_mixed_fixture_pdf(tp, name="mixed.pdf"), 4),
    ],
    ids=["sparse-2p", "dense-2p", "mixed-4p"],
)
def test_estimate_accuracy_vs_real_convert(tmp_path, monkeypatch, fixture_name, fixture_factory, pages):
    """Live accuracy guard across three realistic fixtures.

    Runs `estimate` (offline heuristic) then a real `convert` on the same
    PDF and checks the pre-flight numbers land in the same order of
    magnitude as reality for each of three density profiles:

      - sparse-2p: minimal text per page (cover / title style)
      - dense-2p: realistic contract body
      - mixed-4p: 1 sparse cover + 3 dense body pages

    Bounds are **deliberately wide**. This is an order-of-magnitude guard,
    not a precision test. The test's job is "catch someone making the
    estimator 10x worse by accident". See the printed ratio diagnostics
    (pytest -v -s) for the actual observed accuracy per fixture.

    Cost per run: ~$0.01 total across all three fixtures. Run time: ~30s.
    """
    from pocsynth.core import ConversionConfig, run_conversion
    from pocsynth.pricing import (
        actual_convert_cost,
        estimate_convert_cost,
        load_pricing,
    )

    monkeypatch.chdir(tmp_path)
    pdf = fixture_factory(tmp_path)
    pricing = load_pricing()

    # Pre-flight heuristic
    estimate = estimate_convert_cost(
        pdf, "sonnet", pricing, pages=pages, pii_audit=True, region=REGION
    )
    est_input = estimate["bedrock"]["input_tokens"]
    est_output = estimate["bedrock"]["output_tokens"]
    est_total = estimate["total_cost_usd"]

    # Real convert on the same PDF
    cfg = ConversionConfig(
        pdf_url=str(pdf),
        model_key="sonnet",
        export_format="html",
        num_pages=pages,
        pii_audit=True,
        region=REGION,
    )
    result = run_conversion(cfg)
    actual = actual_convert_cost(result, pricing, model_key="sonnet", region=REGION)
    actual_input = actual["bedrock"]["input_tokens"]
    actual_output = actual["bedrock"]["output_tokens"]
    actual_total = actual["total_cost_usd"]

    assert est_input > 0 and actual_input > 0
    assert est_output > 0 and actual_output > 0
    assert est_total > 0 and actual_total > 0

    ratio_in = est_input / actual_input
    ratio_out = est_output / actual_output
    ratio_total = est_total / actual_total

    # Order-of-magnitude bounds — same across all fixtures.
    assert 0.25 <= ratio_in <= 4.0, (
        f"{fixture_name}: input-token estimate off by >4x: "
        f"estimated={est_input}, actual={actual_input}, ratio={ratio_in:.2f}"
    )
    assert 0.1 <= ratio_out <= 10.0, (
        f"{fixture_name}: output-token estimate off by >10x: "
        f"estimated={est_output}, actual={actual_output}, ratio={ratio_out:.2f}"
    )
    assert 0.2 <= ratio_total <= 5.0, (
        f"{fixture_name}: total cost off by >5x: "
        f"estimated=${est_total:.4f}, actual=${actual_total:.4f}"
    )

    print(
        f"\n  [{fixture_name}] "
        f"input {ratio_in:.2f}x (est={est_input}, act={actual_input})  "
        f"output {ratio_out:.2f}x (est={est_output}, act={actual_output})  "
        f"total {ratio_total:.2f}x (est=${est_total:.4f}, act=${actual_total:.4f})"
    )
