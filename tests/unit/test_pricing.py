# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pricing estimator unit tests.

Covers loading, Bedrock math, Comprehend tiered calculation with 300-char
minimum and 100-char rounding, staleness, region coverage, and key-drift
between MODELS and pricing.json.
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta

import pytest

from pocsynth.bedrock import MODELS
from pocsynth.errors import PricingDataError
from pocsynth.pricing import (
    DEFAULT_IMAGE_TOKENS_PER_PAGE,
    MIN_OUTPUT_TOKENS_PER_PAGE,
    actual_convert_cost,
    estimate_bedrock_cost,
    estimate_comprehend_cost,
    estimate_convert_cost,
    load_pricing,
    pricing_age_days,
    region_coverage,
)


@pytest.fixture
def pricing():
    """Load the real packaged pricing.json."""
    return load_pricing()


# ---------- load_pricing --------------------------------------------------


class TestLoadPricing:
    def test_default_path_loads(self, pricing):
        assert pricing["schema"] == 1
        assert "bedrock" in pricing
        assert "comprehend" in pricing

    def test_custom_path_loads(self, tmp_path, pricing):
        p = tmp_path / "custom.json"
        p.write_text(json.dumps(pricing))
        loaded = load_pricing(p)
        assert loaded == pricing

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(PricingDataError) as excinfo:
            load_pricing(tmp_path / "nope.json")
        assert "not found" in str(excinfo.value).lower()

    def test_malformed_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        with pytest.raises(PricingDataError) as excinfo:
            load_pricing(p)
        assert "valid json" in str(excinfo.value).lower()

    @pytest.mark.parametrize("missing", ["schema", "retrieved", "currency",
                                           "bedrock", "comprehend", "applies_to_regions"])
    def test_missing_required_field_raises(self, tmp_path, pricing, missing):
        data = copy.deepcopy(pricing)
        del data[missing]
        p = tmp_path / "x.json"
        p.write_text(json.dumps(data))
        with pytest.raises(PricingDataError) as excinfo:
            load_pricing(p)
        assert missing in str(excinfo.value)

    def test_wrong_schema_version_raises(self, tmp_path, pricing):
        data = copy.deepcopy(pricing)
        data["schema"] = 99
        p = tmp_path / "x.json"
        p.write_text(json.dumps(data))
        with pytest.raises(PricingDataError):
            load_pricing(p)


# ---------- key-drift guard ------------------------------------------------


class TestKeyDrift:
    def test_bedrock_keys_match_models(self, pricing):
        assert set(pricing["bedrock"].keys()) == set(MODELS.keys()), (
            "pricing.json bedrock keys diverged from MODELS. "
            "If a model was added/removed, update pricing.json too."
        )

    def test_model_ids_match(self, pricing):
        for key, info in MODELS.items():
            assert pricing["bedrock"][key]["model_id"] == info["id"]


# ---------- estimate_bedrock_cost -----------------------------------------


class TestEstimateBedrockCost:
    def test_one_mtok_input_sonnet(self, pricing):
        result = estimate_bedrock_cost("sonnet", 1_000_000, 0, pricing)
        assert result["input_cost_usd"] == 3.00
        assert result["output_cost_usd"] == 0.00
        assert result["total_cost_usd"] == 3.00

    def test_half_mtok_output_haiku(self, pricing):
        result = estimate_bedrock_cost("haiku", 0, 500_000, pricing)
        assert result["output_cost_usd"] == 2.50
        assert result["total_cost_usd"] == 2.50

    def test_zero_tokens(self, pricing):
        result = estimate_bedrock_cost("sonnet", 0, 0, pricing)
        assert result["total_cost_usd"] == 0.0

    def test_opus_is_5x_sonnet_for_same_tokens(self, pricing):
        sonnet = estimate_bedrock_cost("sonnet", 1_000_000, 1_000_000, pricing)
        opus = estimate_bedrock_cost("opus", 1_000_000, 1_000_000, pricing)
        # Sonnet: $3 + $15 = $18; Opus: $15 + $75 = $90; ratio = 5
        assert opus["total_cost_usd"] == sonnet["total_cost_usd"] * 5

    def test_unknown_model_raises(self, pricing):
        with pytest.raises(PricingDataError):
            estimate_bedrock_cost("fake-model", 1000, 100, pricing)


# ---------- estimate_comprehend_cost --------------------------------------


class TestComprehendMinimum:
    """The 300-char / 3-unit minimum per request."""

    @pytest.mark.parametrize("chars", [0, 1, 50, 250, 299, 300])
    def test_below_or_at_minimum_bills_3_units(self, pricing, chars):
        result = estimate_comprehend_cost(chars, pricing)
        assert result["units"] == 3

    def test_301_chars_bills_4_units(self, pricing):
        assert estimate_comprehend_cost(301, pricing)["units"] == 4


class TestComprehendRounding:
    """100-char rounding (ceil)."""

    @pytest.mark.parametrize("chars,expected_units", [
        (301, 4),
        (400, 4),
        (401, 5),
        (500, 5),
        (501, 6),
    ])
    def test_100_char_ceil(self, pricing, chars, expected_units):
        assert estimate_comprehend_cost(chars, pricing)["units"] == expected_units


class TestComprehendTiers:
    def test_tier_1_only_for_poc_volumes(self, pricing):
        # 10k units = 1M chars. Well under tier 1 cap (10M units).
        result = estimate_comprehend_cost(1_000_000, pricing)
        assert len(result["tier_breakdown"]) == 1
        assert result["tier_breakdown"][0]["price_per_unit_usd"] == 0.0001
        assert result["cost_usd"] == pytest.approx(10_000 * 0.0001, rel=1e-6)  # 10_000 units * $0.0001

    def test_crosses_tier_1_to_tier_2(self, pricing):
        # 100k new units when already at 9.9M consumed → 100k at tier 1, 0 at tier 2
        # Push further: 200k new units → 100k tier 1 + 100k tier 2
        result = estimate_comprehend_cost(
            char_count=20_000_000,  # 200k units
            pricing=pricing,
            monthly_units_already_consumed=9_900_000,
        )
        assert len(result["tier_breakdown"]) == 2
        tier1 = result["tier_breakdown"][0]
        tier2 = result["tier_breakdown"][1]
        assert tier1["upto_units_per_month"] == 10_000_000
        assert tier1["units"] == 100_000
        assert tier1["price_per_unit_usd"] == 0.0001
        assert tier2["upto_units_per_month"] == 50_000_000
        assert tier2["units"] == 100_000
        assert tier2["price_per_unit_usd"] == 0.00005
        # Total: 100k * $0.0001 + 100k * $0.00005 = $10 + $5 = $15
        assert result["cost_usd"] == pytest.approx(15.0)

    def test_spans_multiple_tiers(self, pricing):
        # 150M new units from zero: 10M tier1 + 40M tier2 + 50M tier3 + 50M tier4
        result = estimate_comprehend_cost(
            char_count=15_000_000_000,  # 150M units
            pricing=pricing,
            monthly_units_already_consumed=0,
        )
        assert len(result["tier_breakdown"]) == 4
        units = [t["units"] for t in result["tier_breakdown"]]
        assert units == [10_000_000, 40_000_000, 50_000_000, 50_000_000]

    def test_unknown_api_raises(self, pricing):
        with pytest.raises(PricingDataError):
            estimate_comprehend_cost(100, pricing, api="fake_api")

    # ---- Gap 5: exact tier-boundary behavior ----

    def test_exactly_fills_tier_1_stays_single_tier(self, pricing):
        # 10M units exactly = the tier-1 cap. Must NOT spill into tier 2.
        result = estimate_comprehend_cost(
            char_count=1_000_000_000,  # 10M units
            pricing=pricing, monthly_units_already_consumed=0)
        assert len(result["tier_breakdown"]) == 1
        assert result["tier_breakdown"][0]["units"] == 10_000_000
        assert result["cost_usd"] == pytest.approx(10_000_000 * 0.0001)

    def test_already_at_tier_1_cap_rolls_entirely_to_tier_2(self, pricing):
        # Tier 1 has zero remaining capacity (already consumed its 10M cap);
        # new units must all bill at tier 2, with tier 1 omitted from breakdown.
        result = estimate_comprehend_cost(
            char_count=100_000,  # 1_000 units
            pricing=pricing, monthly_units_already_consumed=10_000_000)
        assert len(result["tier_breakdown"]) == 1
        only = result["tier_breakdown"][0]
        assert only["upto_units_per_month"] == 50_000_000  # tier 2
        assert only["price_per_unit_usd"] == 0.00005
        assert only["units"] == 1_000

    def test_unbounded_final_tier_absorbs_remainder(self, pricing):
        # Start already past the last capped tier (100M consumed) so everything
        # lands in the final tier whose cap is None (unbounded).
        result = estimate_comprehend_cost(
            char_count=10_000_000,  # 100k units
            pricing=pricing, monthly_units_already_consumed=100_000_000)
        assert len(result["tier_breakdown"]) == 1
        last = result["tier_breakdown"][0]
        assert last["upto_units_per_month"] is None
        assert last["units"] == 100_000
        assert last["price_per_unit_usd"] == 5e-06


# ---------- pricing_age_days ----------------------------------------------


class TestStaleness:
    def test_same_day_is_zero(self, pricing):
        data = copy.deepcopy(pricing)
        data["retrieved"] = date.today().isoformat()
        assert pricing_age_days(data) == 0

    def test_100_days_old(self, pricing):
        data = copy.deepcopy(pricing)
        data["retrieved"] = (date.today() - timedelta(days=100)).isoformat()
        assert pricing_age_days(data) == 100

    def test_future_date_clamps_to_zero(self, pricing):
        data = copy.deepcopy(pricing)
        data["retrieved"] = (date.today() + timedelta(days=5)).isoformat()
        assert pricing_age_days(data) == 0

    def test_custom_reference_date(self, pricing):
        data = copy.deepcopy(pricing)
        data["retrieved"] = "2026-01-01"
        # Reference 90 days later
        assert pricing_age_days(data, today=date(2026, 4, 1)) == 90


# ---------- region_coverage -----------------------------------------------


class TestRegionCoverage:
    def test_known_region(self, pricing):
        assert region_coverage(pricing, "us-east-1") == "known"

    def test_unknown_region(self, pricing):
        assert region_coverage(pricing, "ap-southeast-2") == "unknown"

    def test_override_makes_known(self, pricing):
        data = copy.deepcopy(pricing)
        data["region_specific_overrides"] = {
            "gov-us-east-1": {"bedrock": {"sonnet": {"input_per_mtok": 4.0}}}
        }
        assert region_coverage(data, "gov-us-east-1") == "known"


# ---------- estimate_convert_cost (heuristics) ----------------------------


@pytest.fixture
def tiny_pdf(tmp_path):
    """A minimal fitz-generated PDF fixture for the heuristic tests."""
    import fitz
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page()
        page.insert_text((72, 96), "Hello World", fontsize=18)
        page.insert_text((72, 130), "Test content for pricing heuristics", fontsize=11)
    p = tmp_path / "tiny.pdf"
    doc.save(p)
    doc.close()
    return p


class TestEstimateConvertCost:
    def test_basic_shape(self, pricing, tiny_pdf):
        result = estimate_convert_cost(tiny_pdf, "sonnet", pricing, pages=2)
        assert result["pages"] == 2
        assert "bedrock" in result
        assert "comprehend" in result
        assert result["total_cost_usd"] > 0
        assert result["estimate"]["confidence"] == "low"
        assert result["estimate"]["method"] == "heuristic"

    def test_pii_audit_disabled_omits_comprehend(self, pricing, tiny_pdf):
        result = estimate_convert_cost(tiny_pdf, "sonnet", pricing, pages=2, pii_audit=False)
        assert result["comprehend"] is None

    def test_opus_costs_more_than_sonnet(self, pricing, tiny_pdf):
        sonnet = estimate_convert_cost(tiny_pdf, "sonnet", pricing, pages=2)
        opus = estimate_convert_cost(tiny_pdf, "opus", pricing, pages=2)
        assert opus["total_cost_usd"] > sonnet["total_cost_usd"]

    def test_unknown_region_adds_warning(self, pricing, tiny_pdf):
        result = estimate_convert_cost(
            tiny_pdf, "sonnet", pricing, pages=1, region="ap-southeast-2"
        )
        assert result["region_coverage"] == "unknown"
        assert any("ap-southeast-2" in w for w in result["warnings"])

    def test_stale_pricing_adds_warning(self, pricing, tiny_pdf):
        data = copy.deepcopy(pricing)
        data["retrieved"] = (date.today() - timedelta(days=120)).isoformat()
        result = estimate_convert_cost(tiny_pdf, "sonnet", data, pages=1)
        assert any("days old" in w for w in result["warnings"])

    def test_assumptions_recorded_in_envelope(self, pricing, tiny_pdf):
        result = estimate_convert_cost(tiny_pdf, "sonnet", pricing, pages=2)
        assumptions = result["estimate"]["assumptions"]
        assert assumptions["chars_per_token"] > 0
        assert assumptions["image_tokens_per_page"] > 0
        # Output is now density-scaled, not a flat per-page constant.
        assert 0 < assumptions["output_tokens_ratio"] <= 1
        assert assumptions["output_tokens_min_per_page"] > 0
        assert assumptions["output_tokens_max_per_page"] > assumptions["output_tokens_min_per_page"]
        assert assumptions["output_tokens_estimated"] > 0
        assert assumptions["text_tokens_estimated"] >= 0
        assert "text_chars_measured" in assumptions

    # ---- Gap 5: zero-char (image-only / blank) PDF ----

    def test_blank_pdf_uses_image_and_min_output_tokens(self, pricing, tmp_path):
        # A page with no text layer (scanned/image-only) yields 0 text chars.
        # The estimate must still bill image tokens + the per-page output floor,
        # and never produce a negative/NaN cost.
        import fitz
        doc = fitz.open()
        for _ in range(2):
            doc.new_page()  # no text inserted
        p = tmp_path / "blank.pdf"
        doc.save(p)
        doc.close()

        result = estimate_convert_cost(p, "sonnet", pricing, pages=2, pii_audit=False)
        a = result["estimate"]["assumptions"]
        assert a["text_chars_measured"] == 0
        assert a["text_tokens_estimated"] == 0
        # input tokens = image tokens (2 pages) + the ~200 system-prompt constant.
        assert a["image_tokens_per_page"] == DEFAULT_IMAGE_TOKENS_PER_PAGE
        assert result["bedrock"]["input_tokens"] >= 2 * DEFAULT_IMAGE_TOKENS_PER_PAGE
        # output falls back to the per-page minimum floor (no text to scale from).
        assert a["output_tokens_estimated"] == 2 * MIN_OUTPUT_TOKENS_PER_PAGE
        assert result["total_cost_usd"] > 0


# ---------- actual_convert_cost -------------------------------------------


class TestActualConvertCost:
    def test_exact_math_from_bedrock_usage(self, pricing, tmp_path):
        # 10k input, 2k output on Sonnet → (10000/1M * 3) + (2000/1M * 15) = 0.03 + 0.03 = 0.06
        result_dict = {
            "output": {"bedrock_usage": {"input_tokens": 10_000, "output_tokens": 2_000}},
            "pii_audit": {"enabled": False},
        }
        cost = actual_convert_cost(result_dict, pricing, model_key="sonnet")
        assert cost["bedrock"]["total_cost_usd"] == pytest.approx(0.06)
        assert cost["total_cost_usd"] == pytest.approx(0.06)
        assert cost["estimate"]["confidence"] == "actual"
        assert cost["comprehend"] is None

    def test_includes_comprehend_when_audit_enabled(self, pricing, tmp_path):
        combined = tmp_path / "out.html"
        combined.write_text("x" * 1000, encoding="utf-8")
        result_dict = {
            "output": {
                "bedrock_usage": {"input_tokens": 0, "output_tokens": 0},
                "combined_path": str(combined),
            },
            "pii_audit": {"enabled": True, "path": "/tmp/audit.txt"},
        }
        cost = actual_convert_cost(result_dict, pricing, model_key="sonnet")
        assert cost["comprehend"] is not None
        assert cost["comprehend"]["chars_billed"] == 1000
        assert cost["comprehend"]["units"] == 10  # 1000/100 = 10 units (above 3 min)


# ---------- estimate-vs-actual accuracy (stubbed) -------------------------


class TestEstimateAccuracyStubbed:
    """How close does the pre-flight heuristic land vs. a known-shape actual run?

    This uses a canned result dict that mimics what run_conversion would
    produce. The live counterpart (tests/live/test_live_aws.py::
    test_estimate_accuracy_vs_real_convert) validates against actual Bedrock
    token counts.

    Bounds are deliberately generous. The estimate is a heuristic; tightening
    these bounds risks flaky tests when either Anthropic tokenization drifts
    slightly or we tune our constants. The test's job is "estimate is in the
    right order of magnitude", not "estimate is precise".
    """

    def test_sonnet_2_page_estimate_within_envelope(self, pricing, tiny_pdf):
        # Pre-flight heuristic estimate
        estimate = estimate_convert_cost(tiny_pdf, "sonnet", pricing, pages=2,
                                         pii_audit=True)
        est_input = estimate["bedrock"]["input_tokens"]
        est_output = estimate["bedrock"]["output_tokens"]
        est_total = estimate["total_cost_usd"]

        # Canned "actual" that represents a reasonable real run on the same
        # 2-page fitz-generated PDF. Numbers chosen to mimic what real
        # Sonnet 4.6 has actually returned on similar fixtures (see
        # result.output.bedrock_usage in past test runs):
        canned_actual = {
            "output": {
                "bedrock_usage": {"input_tokens": 4000, "output_tokens": 300},
                "combined_path": None,
            },
            "pii_audit": {"enabled": False},
        }
        actual = actual_convert_cost(canned_actual, pricing, model_key="sonnet")
        actual_input = actual["bedrock"]["input_tokens"]
        actual_output = actual["bedrock"]["output_tokens"]
        actual_total = actual["bedrock"]["total_cost_usd"]

        # Input tokens: heuristic is text_chars/4 + pages*1500 image + 200 system
        # (see DEFAULT_* constants in pricing.py). Real Bedrock returns approximate
        # token counts on the same inputs. ±4x bound — basically "same order of
        # magnitude". The estimate overshoots on image-heavy docs and undershoots
        # on text-heavy ones.
        ratio_in = est_input / max(actual_input, 1)
        assert 0.25 <= ratio_in <= 4.0, (
            f"input-token estimate is off by >4x: "
            f"estimated={est_input}, actual={actual_input}, ratio={ratio_in:.2f}"
        )

        # Output: model nondeterminism + our density-scaled output estimate
        # (0.40 * text-tokens/page, floored/capped) means this one is genuinely
        # hard. Accept anything within 5x.
        ratio_out = est_output / max(actual_output, 1)
        assert 0.2 <= ratio_out <= 5.0, (
            f"output-token estimate is off by >5x: "
            f"estimated={est_output}, actual={actual_output}, ratio={ratio_out:.2f}"
        )

        # Total cost sanity: estimate should be positive and in the ballpark
        # of actual. ±4x is a very loose envelope; tighter bounds have
        # flaked historically on comparable tools.
        ratio_total = est_total / max(actual_total, 1e-6)
        assert 0.2 <= ratio_total <= 5.0, (
            f"total cost estimate off by >5x: "
            f"estimated=${est_total:.4f}, actual=${actual_total:.4f}"
        )

    def test_estimate_scales_linearly_with_pages(self, pricing, tmp_path):
        """A 4-page estimate should be ~2x a 2-page estimate for the same PDF."""
        import fitz
        doc = fitz.open()
        for _ in range(4):
            page = doc.new_page()
            page.insert_text((72, 96), "Hello", fontsize=18)
            page.insert_text((72, 130), "Content for the page.", fontsize=11)
        p = tmp_path / "four.pdf"
        doc.save(p)
        doc.close()

        two = estimate_convert_cost(p, "sonnet", pricing, pages=2, pii_audit=False)
        four = estimate_convert_cost(p, "sonnet", pricing, pages=4, pii_audit=False)

        ratio = four["total_cost_usd"] / two["total_cost_usd"]
        # Not exactly 2x because system-prompt tokens amortize across pages,
        # but should land between 1.7 and 2.3.
        assert 1.7 <= ratio <= 2.3, (
            f"4-page / 2-page ratio should be ~2.0, got {ratio:.2f}"
        )


# ---------- packaged pricing.json sanity ----------------------------------


class TestPackagedPricing:
    """Smoke: the committed pricing file passes every structural check and
    contains sane values. Catches finger-trouble edits to pricing.json."""

    def test_bedrock_prices_positive(self, pricing):
        for key, entry in pricing["bedrock"].items():
            assert entry["input_per_mtok"] > 0, f"{key} input_per_mtok must be > 0"
            assert entry["output_per_mtok"] > entry["input_per_mtok"], (
                f"{key} output should be more expensive than input"
            )

    def test_comprehend_tiers_monotonically_decreasing(self, pricing):
        for api_entry in pricing["comprehend"]["apis"].values():
            prices = [t["price_per_unit_usd"] for t in api_entry["tiers"]]
            for a, b in zip(prices, prices[1:], strict=False):
                assert b <= a, f"tier prices should only decrease: {prices}"

    def test_applies_to_regions_non_empty(self, pricing):
        assert len(pricing["applies_to_regions"]) >= 1
