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


def test_list_presets_nonempty():
    names = [p["name"] for p in presets.list_presets()]
    assert {"b2b_saas", "ecommerce_orders", "healthcare_lite"} <= set(names)


def test_unknown_preset_raises():
    with pytest.raises(SchemaError):
        presets.load_preset("does_not_exist")


@pytest.mark.parametrize("name", ["b2b_saas", "ecommerce_orders", "healthcare_lite"])
def test_each_preset_valid_and_round_trips(name, tmp_path):
    schema = presets.load_preset(name)
    _validate_schema_shape(schema)
    gen = run_generation(GenerateConfig(schema=schema, rows=50, seed=5, output_dir=str(tmp_path)))
    res = run_validation(ValidateConfig(rows_path=gen["output"]["rows_path"], schema=schema))
    assert res["valid"] is True, res["violations"][:3]
