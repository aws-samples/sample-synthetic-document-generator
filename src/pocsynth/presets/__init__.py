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
_PRESETS: dict[str, str] = {
    "b2b_saas": "B2B SaaS customer accounts: company, plan tier, MRR, seats, region.",
    "ecommerce_orders": "E-commerce orders: order id, SKU, category, quantity, amount, channel, status.",
    "healthcare_lite": "Healthcare-lite patient intake (synthetic): name, DOB, state, plan, MRN.",
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
