# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bundled presets load, validate, generate, and round-trip — fully offline."""

from __future__ import annotations

import pytest

from pocsynth import presets
from pocsynth.errors import SchemaError
from pocsynth.generate import GenerateConfig, run_generation
from pocsynth.schema import _validate_schema_shape
from pocsynth.validate import ValidateConfig, run_validation

# Every preset name the registry advertises — the parametrized tests below cover
# the full set, so adding a preset to _PRESETS automatically extends coverage.
ALL_PRESETS = [p["name"] for p in presets.list_presets()]

# The SIM-vertical presets added for secure prototyping (F3). The three originals
# plus these should all be present.
F3_PRESETS = {
    "crm_contacts", "insurance_claims", "utility_meter", "loyalty_pos",
    "ad_campaign", "knowledge_corpus", "security_telemetry",
}


def test_list_presets_covers_originals_and_verticals():
    names = set(ALL_PRESETS)
    assert {"b2b_saas", "ecommerce_orders", "healthcare_lite"} <= names
    assert F3_PRESETS <= names, F3_PRESETS - names


def test_unknown_preset_raises():
    with pytest.raises(SchemaError):
        presets.load_preset("does_not_exist")


def test_every_advertised_preset_has_a_file():
    # The _PRESETS dict and the shipped *.json files must not drift apart.
    for name in ALL_PRESETS:
        schema = presets.load_preset(name)
        assert schema.get("fields"), name


@pytest.mark.parametrize("name", ALL_PRESETS)
def test_each_preset_valid_and_round_trips(name, tmp_path):
    schema = presets.load_preset(name)
    _validate_schema_shape(schema)
    gen = run_generation(GenerateConfig(schema=schema, rows=50, seed=5, output_dir=str(tmp_path)))
    res = run_validation(ValidateConfig(rows_path=gen["output"]["rows_path"], schema=schema))
    assert res["valid"] is True, res["violations"][:3]


@pytest.mark.parametrize("name", ALL_PRESETS)
def test_presets_are_synthetic_by_construction(name):
    """Presets are the safe-by-construction fast path: no field may carry a literal
    `enum` of real-looking PII. Identifying values come from faker/regex, and any
    enum must be a small closed set of category labels (the PII guard invariant,
    ADR-0005, asserted statically here so the fast path needs no `verify`)."""
    schema = presets.load_preset(name)
    for field in schema["fields"]:
        enum = field.get("enum")
        if enum is None:
            continue
        # Category enums are short, finite label sets — never free-form identifiers.
        assert len(enum) <= 16, f"{name}.{field['name']}: enum too large to be a category set"
        for value in enum:
            assert "@" not in str(value), f"{name}.{field['name']}: enum looks like an email"
