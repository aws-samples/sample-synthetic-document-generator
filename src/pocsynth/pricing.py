# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cost estimation for pocsynth.

Runtime source of truth is the committed `pricing.json`. This module does
NOT import boto3 or call the AWS Pricing API at runtime — the skill
script's PEP 723 declaration keeps boto3 off the estimate path.
"""

from __future__ import annotations

import json
import math
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

from pocsynth.errors import PricingDataError

# Default heuristic constants, exposed so tests can reason about them.
# Calibrated against live Sonnet 4.6 runs on a 2-page dense fixture
# (2026-04-25); image-token constant moved from 1750 → 1500 (low end of
# Anthropic's 1500-2000 documented range) to correct a ~40% input overshoot.
DEFAULT_CHARS_PER_TOKEN = 4          # English-prose rule of thumb for Anthropic tokenizers
DEFAULT_IMAGE_TOKENS_PER_PAGE = 1500  # Low end of Anthropic's documented 1500-2000 per page
DEFAULT_CHARS_PER_PAGE_FOR_PII = 3500  # Text-dense page; used when we don't have actual output yet
# Output-token scaling: synthetic HTML/Markdown output length correlates
# with input text density. Calibrated against live Sonnet 4.6 runs across
# three fixture densities (2026-04-25). Observed true output/input ratios:
#     sparse-2p   → 0.02  (near-empty pages; text is almost all output)
#     dense-2p    → 0.46  (realistic contract body)
#     mixed-4p    → 0.34  (1 cover + 3 dense body)
# A single ratio can't fit all three perfectly. 0.40 is chosen as the
# realistic-document midpoint; sparse pages over-estimate (we accept that —
# sparse absolute numbers are tiny so error is cheap), dense/mixed pages
# land within 0.85-1.18x which is well inside our documented ±30-50% margin.
OUTPUT_TOKENS_PER_INPUT_TOKEN_RATIO = 0.40
MIN_OUTPUT_TOKENS_PER_PAGE = 150   # Even a near-empty page produces some HTML
MAX_OUTPUT_TOKENS_PER_PAGE = 2000  # Saturates before the 8k maxTokens cap
STALE_WARN_DAYS = 90                  # Warn on pricing snapshots older than this

# Structured extraction emits compact tool-call JSON (field/value pairs), far
# shorter than convert's rendered HTML/Markdown. Lower output ratio + ceiling.
# NOTE: a first-pass guess pending live calibration (cf. the convert ratio).
EXTRACT_OUTPUT_TOKENS_PER_INPUT_TOKEN_RATIO = 0.15
EXTRACT_MAX_OUTPUT_TOKENS_PER_PAGE = 1200
# schema-infer/from-prompt: one small call. A schema for a wide table rarely
# exceeds a few hundred output tokens.
SCHEMA_INFER_OUTPUT_TOKENS = 600


def load_pricing(path: str | Path | None = None) -> dict[str, Any]:
    """Load the pricing snapshot.

    Default path is the packaged `src/pocsynth/pricing.json`. A custom path
    allows users with negotiated pricing to override (future `--pricing-file`
    flag can wire this).
    """
    if path is None:
        try:
            with resources.files("pocsynth").joinpath("pricing.json").open("r") as fh:
                data = json.load(fh)
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise PricingDataError(
                "Bundled pricing.json not found",
                context={"path": "pocsynth/pricing.json"},
                hint="Reinstall pocsynth, or pass --pricing-file to point at an external file",
            ) from exc
    else:
        p = Path(path)
        if not p.exists():
            raise PricingDataError(
                f"Pricing file not found: {p}",
                context={"path": str(p)},
            )
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PricingDataError(
                f"Pricing file is not valid JSON: {exc}",
                context={"path": str(p), "json_error": str(exc)},
            ) from exc

    _validate_pricing_shape(data)
    return data


def _validate_pricing_shape(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise PricingDataError("Pricing file is not a JSON object")
    for required in ("schema", "retrieved", "currency", "bedrock", "comprehend",
                     "applies_to_regions"):
        if required not in data:
            raise PricingDataError(
                f"Pricing file missing required field: {required!r}",
                context={"missing_field": required},
            )
    if data["schema"] != 1:
        raise PricingDataError(
            f"Unsupported pricing schema version: {data['schema']}",
            context={"schema": data["schema"]},
            hint="Regenerate pricing.json with the current refresh script",
        )


def pricing_age_days(pricing: dict[str, Any], *, today: date | None = None) -> int:
    """Whole days between `pricing.retrieved` and today (or supplied date).

    Clamped to zero — a future-dated snapshot reports 0, never negative.
    """
    try:
        retrieved = date.fromisoformat(pricing["retrieved"])
    except ValueError as exc:
        raise PricingDataError(
            f"Pricing 'retrieved' field is not an ISO date: {pricing.get('retrieved')!r}",
            context={"retrieved": pricing.get("retrieved")},
            hint="Regenerate pricing.json with the current refresh script",
        ) from exc
    reference = today or date.today()
    return max(0, (reference - retrieved).days)


def region_coverage(pricing: dict[str, Any], region: str) -> str:
    """Return 'known' if `region` is in applies_to_regions or has an override,
    'unknown' otherwise.
    """
    if region in pricing.get("applies_to_regions", []):
        return "known"
    if region in pricing.get("region_specific_overrides", {}):
        return "known"
    return "unknown"


def _bedrock_entry(pricing: dict[str, Any], model_key: str, region: str | None = None) -> dict:
    """Return the per-model pricing record, honoring region overrides."""
    if region and region in pricing.get("region_specific_overrides", {}):
        override = pricing["region_specific_overrides"][region].get("bedrock", {}).get(model_key)
        if override is not None:
            return override
    try:
        return pricing["bedrock"][model_key]
    except KeyError as exc:
        raise PricingDataError(
            f"No pricing entry for model {model_key!r}",
            context={"model_key": model_key, "available": sorted(pricing["bedrock"].keys())},
            hint="Regenerate pricing.json or pick one of the listed models",
        ) from exc


def estimate_bedrock_cost(
    model_key: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, Any],
    *,
    region: str | None = None,
) -> dict[str, Any]:
    """Compute Bedrock cost for a known token count, model, and region."""
    entry = _bedrock_entry(pricing, model_key, region)
    input_cost = input_tokens / 1_000_000 * float(entry["input_per_mtok"])
    output_cost = output_tokens / 1_000_000 * float(entry["output_per_mtok"])
    return {
        "model": model_key,
        "model_id": entry.get("model_id"),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "input_cost_usd": round(input_cost, 6),
        "output_cost_usd": round(output_cost, 6),
        "total_cost_usd": round(input_cost + output_cost, 6),
        "provenance": entry.get("provenance", "unknown"),
    }


def _comprehend_billable_units(char_count: int, min_units: int) -> int:
    """100-char rounding + per-request minimum."""
    if char_count <= 0:
        return min_units
    units = math.ceil(char_count / 100)
    return max(min_units, units)


def estimate_comprehend_cost(
    char_count: int,
    pricing: dict[str, Any],
    *,
    api: str = "detect_pii_entities",
    monthly_units_already_consumed: int = 0,
) -> dict[str, Any]:
    """Tiered Comprehend cost.

    `monthly_units_already_consumed` is the pre-existing unit count for the
    AWS account this month; defaults to 0 so single-run PoC estimates always
    bill at tier 1 (which is correct — Comprehend resets at month boundaries
    and PoC volumes never cross tier 1 in a single run).
    """
    apis = pricing["comprehend"]["apis"]
    if api not in apis:
        raise PricingDataError(
            f"Unknown Comprehend API: {api!r}",
            context={"api": api, "available": sorted(apis.keys())},
        )
    api_entry = apis[api]
    tiers = api_entry["tiers"]
    min_units = pricing["comprehend"]["minimum_units_per_request"]

    units = _comprehend_billable_units(char_count, min_units)

    # Walk tiers, consuming `units` against remaining capacity starting from
    # `monthly_units_already_consumed`.
    consumed = int(monthly_units_already_consumed)
    remaining = units
    total_cost = 0.0
    breakdown: list[dict[str, Any]] = []

    for tier in tiers:
        if remaining <= 0:
            break
        cap = tier["upto_units_per_month"]
        rate = float(tier["price_per_unit_usd"])
        if cap is None:
            in_tier = remaining
        else:
            capacity = max(0, cap - consumed)
            in_tier = min(remaining, capacity)
        if in_tier > 0:
            tier_cost = in_tier * rate
            breakdown.append({
                "upto_units_per_month": cap,
                "units": in_tier,
                "price_per_unit_usd": rate,
                "cost_usd": round(tier_cost, 10),
            })
            total_cost += tier_cost
            remaining -= in_tier
            consumed += in_tier

    return {
        "api": api,
        "chars_billed": char_count,
        "units": units,
        "minimum_units_per_request": min_units,
        "tier_breakdown": breakdown,
        "cost_usd": round(total_cost, 6),
    }


def _count_text_chars_from_pdf(pdf_path: Path, max_pages: int | None = None) -> tuple[int, int]:
    """Return (total_text_chars, total_pages_measured)."""
    import fitz  # local import so test_pricing_pure tests can run without fitz

    total_chars = 0
    with fitz.open(str(pdf_path)) as doc:
        total = len(doc)
        if max_pages is not None:
            total = min(total, max_pages)
        for i in range(total):
            page = doc.load_page(i)
            total_chars += len(page.get_text("text"))
    return total_chars, total


def _estimate_output_tokens(
    text_tokens: int,
    pages: int,
    *,
    ratio: float = OUTPUT_TOKENS_PER_INPUT_TOKEN_RATIO,
    min_per_page: int = MIN_OUTPUT_TOKENS_PER_PAGE,
    max_per_page: int = MAX_OUTPUT_TOKENS_PER_PAGE,
) -> int:
    """Output-token estimate that scales with input text density.

    Dense pages produce long output; near-empty pages produce short output.
    Floors and ceilings prevent extreme values at either end.
    """
    per_page = text_tokens / max(pages, 1) * ratio
    per_page = max(min_per_page, min(per_page, max_per_page))
    return int(round(per_page * pages))


def _finalize_envelope(
    envelope: dict[str, Any],
    pricing: dict[str, Any],
    *,
    age: int,
    region: str | None,
    noun: str,
) -> dict[str, Any]:
    """Populate shared envelope fields (region coverage + stale-pricing warnings).

    `noun` is "estimate" or "cost" — used only to phrase the region warning.
    """
    if region is not None:
        envelope["region"] = region
        envelope["region_coverage"] = region_coverage(pricing, region)
        if envelope["region_coverage"] == "unknown":
            envelope["warnings"].append(
                f"Region {region!r} is not covered by this pricing snapshot; "
                f"{noun} is approximate."
            )
    if age > STALE_WARN_DAYS:
        envelope["warnings"].append(
            f"Pricing snapshot is {age} days old; verify current rates at AWS."
        )
    return envelope


def estimate_convert_cost(
    pdf_path: str | Path,
    model_key: str,
    pricing: dict[str, Any],
    *,
    pages: int | None = None,
    pii_audit: bool = True,
    region: str | None = None,
) -> dict[str, Any]:
    """Pre-flight estimate. Opens the PDF, counts text chars, applies heuristics.

    No AWS calls. Returns the cost envelope with `confidence: "low"` and the
    heuristic assumptions recorded explicitly so agents and humans can see
    how the number was computed.
    """
    path = Path(pdf_path)
    text_chars, pages_measured = _count_text_chars_from_pdf(path, max_pages=pages)

    text_tokens = math.ceil(text_chars / DEFAULT_CHARS_PER_TOKEN) if text_chars else 0
    image_tokens = pages_measured * DEFAULT_IMAGE_TOKENS_PER_PAGE
    # Small constant for the system prompt; cheap to include and keeps the
    # math honest at low page counts.
    system_prompt_tokens = 200
    total_input = text_tokens + image_tokens + system_prompt_tokens
    total_output = _estimate_output_tokens(text_tokens, pages_measured)

    bedrock = estimate_bedrock_cost(model_key, total_input, total_output, pricing, region=region)

    comprehend: dict[str, Any] | None = None
    if pii_audit:
        pii_chars = pages_measured * DEFAULT_CHARS_PER_PAGE_FOR_PII
        comprehend = estimate_comprehend_cost(pii_chars, pricing)

    total = bedrock["total_cost_usd"] + (comprehend["cost_usd"] if comprehend else 0.0)
    age = pricing_age_days(pricing)

    envelope: dict[str, Any] = {
        "pages": pages_measured,
        "bedrock": bedrock,
        "comprehend": comprehend,
        "total_cost_usd": round(total, 6),
        "pricing_retrieved": pricing["retrieved"],
        "pricing_stale_days": age,
        "pricing_source_note": pricing.get("source"),
        "estimate": {
            "confidence": "low",
            "method": "heuristic",
            "assumptions": {
                "chars_per_token": DEFAULT_CHARS_PER_TOKEN,
                "image_tokens_per_page": DEFAULT_IMAGE_TOKENS_PER_PAGE,
                "output_tokens_ratio": OUTPUT_TOKENS_PER_INPUT_TOKEN_RATIO,
                "output_tokens_min_per_page": MIN_OUTPUT_TOKENS_PER_PAGE,
                "output_tokens_max_per_page": MAX_OUTPUT_TOKENS_PER_PAGE,
                "chars_per_page_for_pii": DEFAULT_CHARS_PER_PAGE_FOR_PII,
                "system_prompt_tokens": system_prompt_tokens,
                "text_chars_measured": text_chars,
                "text_tokens_estimated": text_tokens,
                "output_tokens_estimated": total_output,
            },
        },
        "warnings": [],
    }
    return _finalize_envelope(envelope, pricing, age=age, region=region, noun="estimate")


def actual_convert_cost(
    result: dict[str, Any],
    pricing: dict[str, Any],
    *,
    model_key: str,
    region: str | None = None,
) -> dict[str, Any]:
    """Post-flight cost computed from a real run_conversion result dict.

    Bedrock numbers come from `result.output.bedrock_usage`. Comprehend chars
    come from reading the combined output file on disk (when pii_audit was
    enabled and wrote something).
    """
    usage = result.get("output", {}).get("bedrock_usage", {})
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))

    bedrock = estimate_bedrock_cost(model_key, input_tokens, output_tokens, pricing, region=region)

    comprehend: dict[str, Any] | None = None
    pii_audit = result.get("pii_audit", {})
    if pii_audit.get("enabled") and pii_audit.get("path"):
        combined = result.get("output", {}).get("combined_path")
        if combined and Path(combined).exists():
            char_count = len(Path(combined).read_text(encoding="utf-8", errors="replace"))
            comprehend = estimate_comprehend_cost(char_count, pricing)

    total = bedrock["total_cost_usd"] + (comprehend["cost_usd"] if comprehend else 0.0)
    age = pricing_age_days(pricing)

    envelope: dict[str, Any] = {
        "bedrock": bedrock,
        "comprehend": comprehend,
        "total_cost_usd": round(total, 6),
        "pricing_retrieved": pricing["retrieved"],
        "pricing_stale_days": age,
        "pricing_source_note": pricing.get("source"),
        "estimate": {
            "confidence": "actual",
            "method": "measured",
        },
        "warnings": [],
    }
    return _finalize_envelope(envelope, pricing, age=age, region=region, noun="cost")


def estimate_extract_cost(
    pdf_path: str | Path,
    model_key: str,
    pricing: dict[str, Any],
    *,
    pages: int | None = None,
    pii_audit: bool = True,
    region: str | None = None,
) -> dict[str, Any]:
    """Pre-flight estimate for `extract` (ADR-0007).

    Same per-page image+text input heuristic as convert, but a lower output
    ratio (structured records are far shorter than rendered HTML/Markdown).
    """
    path = Path(pdf_path)
    text_chars, pages_measured = _count_text_chars_from_pdf(path, max_pages=pages)
    text_tokens = math.ceil(text_chars / DEFAULT_CHARS_PER_TOKEN) if text_chars else 0
    image_tokens = pages_measured * DEFAULT_IMAGE_TOKENS_PER_PAGE
    system_prompt_tokens = 200
    total_input = text_tokens + image_tokens + system_prompt_tokens
    total_output = _estimate_output_tokens(
        text_tokens, pages_measured,
        ratio=EXTRACT_OUTPUT_TOKENS_PER_INPUT_TOKEN_RATIO,
        min_per_page=MIN_OUTPUT_TOKENS_PER_PAGE,
        max_per_page=EXTRACT_MAX_OUTPUT_TOKENS_PER_PAGE,
    )

    bedrock = estimate_bedrock_cost(model_key, total_input, total_output, pricing, region=region)
    comprehend: dict[str, Any] | None = None
    if pii_audit:
        pii_chars = pages_measured * DEFAULT_CHARS_PER_PAGE_FOR_PII
        comprehend = estimate_comprehend_cost(pii_chars, pricing)
    total = bedrock["total_cost_usd"] + (comprehend["cost_usd"] if comprehend else 0.0)
    age = pricing_age_days(pricing)

    envelope: dict[str, Any] = {
        "target": "extract",
        "pages": pages_measured,
        "bedrock": bedrock,
        "comprehend": comprehend,
        "total_cost_usd": round(total, 6),
        "pricing_retrieved": pricing["retrieved"],
        "pricing_stale_days": age,
        "pricing_source_note": pricing.get("source"),
        "estimate": {"confidence": "low", "method": "heuristic"},
        "warnings": [],
    }
    return _finalize_envelope(envelope, pricing, age=age, region=region, noun="estimate")


def estimate_schema_infer_cost(
    sample_path: str | Path,
    model_key: str,
    pricing: dict[str, Any],
    *,
    region: str | None = None,
) -> dict[str, Any]:
    """Pre-flight estimate for `schema --infer/--from-prompt` (ADR-0007).

    Offline: token-count the sample file (or prompt text) for input; a small
    fixed output budget for the emitted schema. Typically pennies.
    """
    p = Path(sample_path)
    sample_chars = len(p.read_text(encoding="utf-8", errors="replace")) if p.exists() else 0
    input_tokens = math.ceil(sample_chars / DEFAULT_CHARS_PER_TOKEN) + 300
    output_tokens = SCHEMA_INFER_OUTPUT_TOKENS

    bedrock = estimate_bedrock_cost(model_key, input_tokens, output_tokens, pricing, region=region)
    age = pricing_age_days(pricing)
    envelope: dict[str, Any] = {
        "target": "schema",
        "bedrock": bedrock,
        "comprehend": None,
        "total_cost_usd": bedrock["total_cost_usd"],
        "pricing_retrieved": pricing["retrieved"],
        "pricing_stale_days": age,
        "pricing_source_note": pricing.get("source"),
        "estimate": {"confidence": "low", "method": "heuristic",
                     "note": "schema inference is typically pennies"},
        "warnings": [],
    }
    return _finalize_envelope(envelope, pricing, age=age, region=region, noun="estimate")
