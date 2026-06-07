# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline validation of generated rows against a schema (the `test` command).

`run_validation(cfg)` loads rows (CSV or JSON), checks every field with the
single `coerce_and_check` primitive (ADR-0006), then enum/regex constraints,
and returns a structured validation report. No AWS.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocsynth import schema as schema_mod
from pocsynth.errors import SchemaError

EventCallback = Callable[..., None] | None


def _noop(*_a, **_k) -> None:
    pass


@dataclass
class ValidateConfig:
    rows_path: str
    schema: dict[str, Any]
    in_format: str | None = None  # "csv" | "json"; inferred from extension if None
    output_dir: str | None = None


def _load_rows(path: Path, in_format: str | None) -> list[dict[str, Any]]:
    fmt = in_format or ("json" if path.suffix.lower() == ".json" else "csv")
    if not path.exists():
        raise SchemaError(f"rows file not found: {path}", context={"path": str(path)})
    if fmt == "json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise SchemaError("JSON rows file must be a list of objects")
        return data
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def run_validation(cfg: ValidateConfig, on_event: EventCallback = None) -> dict[str, Any]:
    emit = on_event or _noop
    schema_mod._validate_schema_shape(cfg.schema)
    rows = _load_rows(Path(cfg.rows_path), cfg.in_format)
    fields = cfg.schema["fields"]

    violations: list[dict[str, Any]] = []
    by_field: dict[str, int] = {}
    by_rule: dict[str, int] = {}

    def record(row_idx: int, field: str, rule: str, expected: Any, got: Any) -> None:
        violations.append({
            "row": row_idx, "field": field, "rule": rule,
            "expected": expected, "got": got,
        })
        by_field[field] = by_field.get(field, 0) + 1
        by_rule[rule] = by_rule.get(rule, 0) + 1

    # Pre-coerce each field's enum members to the field type so membership is
    # compared like-for-like (a CSV "1" and a JSON 1 both match an integer enum).
    enum_sets: dict[str, set] = {}
    for field in fields:
        if "enum" in field:
            coerced_members = set()
            for member in field["enum"]:
                ok, cm = schema_mod.coerce_and_check(member, field["type"])
                coerced_members.add(cm if ok else member)
            enum_sets[field["name"]] = coerced_members

    emit("validation_started", rows=len(rows), fields=len(fields))
    for idx, row in enumerate(rows):
        for field in fields:
            name = field["name"]
            ftype = field["type"]
            raw = row.get(name)
            ok, coerced = schema_mod.coerce_and_check(raw, ftype)
            if not ok:
                record(idx, name, "type", ftype, raw)
                continue
            if coerced is None:
                continue  # null is allowed (nullable v1)
            if name in enum_sets and coerced not in enum_sets[name]:
                record(idx, name, "enum", field["enum"], coerced)
            if "regex" in field and not re.fullmatch(field["regex"], str(coerced)):
                record(idx, name, "regex", field["regex"], coerced)

    valid = not violations
    emit("validation_complete", valid=valid, violations=len(violations))

    result: dict[str, Any] = {
        "valid": valid,
        "rows_checked": len(rows),
        "violations": violations,
        "summary": {"by_field": by_field, "by_rule": by_rule},
        "cost": None,
    }
    return result
