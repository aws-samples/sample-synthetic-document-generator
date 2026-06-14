# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bundled preset schemas — the fastest, fully-offline demo path.

Preset schema JSON files live alongside this module and are loaded via
importlib.resources (same idiom as pricing.json). Each is a valid v1 schema
with faker/enum/weights already set, ready for `generate`.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from pocsynth.errors import SchemaError
from pocsynth.schema import _validate_schema_shape

# name -> one-line description (kept in sync with the shipped *.json files).
# Every preset is fully synthetic by construction (faker/enum/regex only, no real
# source) so generated rows are safe to share with no `verify` step needed.
_PRESETS: dict[str, str] = {
    "b2b_saas": "B2B SaaS customer accounts: company, plan tier, MRR, seats, region.",
    "ecommerce_orders": "E-commerce orders: order id, SKU, category, quantity, amount, channel, status.",
    "healthcare_lite": "Healthcare-lite patient intake (synthetic): name, DOB, state, plan, MRN.",
    "crm_contacts": "CRM contacts: name, email, company, title, lead source, lifecycle stage, score.",
    "insurance_claims": "Insurance claims intake: claim/policy id, type, state, amount, status, channel.",
    "utility_meter": "Utility smart-meter reads: meter/account id, service, consumption, voltage, quality.",
    "loyalty_pos": "Retail loyalty POS transactions: member tier, department, basket, amount, points.",
    "ad_campaign": "Digital ad-campaign daily performance: channel, impressions, clicks, conversions, spend.",
    "knowledge_corpus": "Knowledge-base article corpus (RAG seed): title, body, category, audience, status.",
    "security_telemetry": "Security/auth-event telemetry: user, source IP, event type, geo, risk score, outcome.",
    "support_tickets": "Support tickets (RAG/agent seed): subject, free-text body, category, priority, status, queue.",
    "commercial_leases": "Commercial lease abstracts: tenant, property type, sq ft, base rent, term, renewal, clauses.",
    "product_reviews": "Product reviews: product, rating, free-text title/body, verified purchase, votes, sentiment.",
    "financial_transactions": "Payment transactions: amount, currency, merchant category, channel, status, fraud flag.",
    "contact_center_transcripts": "Contact-center transcripts: agent, queue, intent, free-text transcript, CSAT.",
}


def list_presets() -> list[dict[str, str]]:
    """Return [{name, description}] for every bundled preset."""
    return [{"name": name, "description": desc} for name, desc in _PRESETS.items()]


def load_preset(name: str) -> dict[str, Any]:
    """Load + validate a bundled preset schema by name. Raises SchemaError."""
    if name not in _PRESETS:
        raise SchemaError(
            f"unknown preset: {name!r}",
            context={"preset": name, "available": sorted(_PRESETS)},
            hint=f"Available presets: {', '.join(sorted(_PRESETS))}",
        )
    try:
        text = resources.files("pocsynth.presets").joinpath(f"{name}.json").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise SchemaError(
            f"preset file missing for {name!r}",
            context={"preset": name},
        ) from exc
    data = json.loads(text)
    _validate_schema_shape(data)
    return data
