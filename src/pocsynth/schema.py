# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared schema model for the structured-data pipeline (offline, no AWS).

A schema is a dependency-free JSON document (`schema: 1`, a list of typed
`fields`). This module is the single source of truth for:

  * validation (`load_schema` / `_validate_schema_shape`) — mirrors the
    `pricing._validate_pricing_shape` + `PricingDataError` idiom;
  * typing (`FIELD_TYPES`, `serialize`, `coerce_and_check`) — the closed type
    set and the one serialize/coerce path that makes the generate→test
    round-trip robust across CSV and JSON (ADR-0006);
  * linting (`lint_schema`, `apply_fixes`) and documentation
    (`document_schema`);
  * the Bedrock `toolConfig` builders for the paid stages (ADR-0002);
  * distribution helpers (`weights_from_counts`) (ADR-0004).
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pocsynth.errors import SchemaError

SCHEMA_VERSION = 1

# Closed set of field types (ADR-0006). Each has explicit serialize + validate
# semantics; nothing else is accepted by `_validate_schema_shape`.
FIELD_TYPES: frozenset[str] = frozenset(
    {"string", "integer", "number", "boolean", "date", "datetime"}
)

# Faker providers treated as identifying. Shared by the schema-infer PII guard
# (schemagen) and the offline lint rule (ADR-0005).
PII_FAKER_PROVIDERS: frozenset[str] = frozenset(
    {
        "name", "first_name", "last_name", "prefix", "suffix",
        "ssn", "email", "ascii_email", "safe_email", "free_email",
        "phone_number", "msisdn",
        "address", "street_address", "street_name", "building_number",
        "credit_card_number", "credit_card_full", "iban", "swift",
        "passport_number", "license_plate",
    }
)

MAX_EXAMPLES_PER_FIELD = 20


# --------------------------------------------------------------------------- #
# Load + validate
# --------------------------------------------------------------------------- #
def load_schema(path: str | Path) -> dict[str, Any]:
    """Read, parse, and validate a schema file. Raises SchemaError."""
    p = Path(path)
    if not p.exists():
        raise SchemaError(
            f"Schema file not found: {p}",
            context={"path": str(p)},
            hint="Provide an existing schema JSON file",
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaError(
            f"Schema file is not valid JSON: {exc}",
            context={"path": str(p), "json_error": str(exc)},
        ) from exc
    _validate_schema_shape(data)
    return data


def _validate_schema_shape(data: Any) -> None:
    if not isinstance(data, dict):
        raise SchemaError("Schema is not a JSON object")
    if data.get("schema") != SCHEMA_VERSION:
        raise SchemaError(
            f"Unsupported schema version: {data.get('schema')!r}",
            context={"schema": data.get("schema")},
            hint=f"Use schema version {SCHEMA_VERSION}",
        )
    fields = data.get("fields")
    if not isinstance(fields, list) or not fields:
        raise SchemaError(
            "Schema 'fields' must be a non-empty list",
            context={"fields_type": type(fields).__name__},
        )
    seen: set[str] = set()
    for i, field in enumerate(fields):
        if not isinstance(field, dict):
            raise SchemaError(f"fields[{i}] is not an object")
        name = field.get("name")
        if not name or not isinstance(name, str):
            raise SchemaError(f"fields[{i}] missing a string 'name'")
        if name in seen:
            raise SchemaError(
                f"duplicate field name: {name!r}", context={"field": name}
            )
        seen.add(name)
        ftype = field.get("type")
        if ftype not in FIELD_TYPES:
            raise SchemaError(
                f"field {name!r} has invalid type {ftype!r}",
                context={"field": name, "type": ftype, "valid": sorted(FIELD_TYPES)},
                hint=f"type must be one of: {', '.join(sorted(FIELD_TYPES))}",
            )


def field_names(schema: dict[str, Any]) -> list[str]:
    """Ordered column list (CSV headers; used by generate + test)."""
    return [f["name"] for f in schema["fields"]]


# --------------------------------------------------------------------------- #
# Typing: canonical serialize + coercion-based validate (ADR-0006)
# --------------------------------------------------------------------------- #
def serialize(value: Any, ftype: str, fmt: str) -> Any:
    """Canonical serialization shared by generate + extract.

    dates/datetimes → ISO-8601 strings in BOTH formats (JSON has no date type).
    For ``csv`` everything becomes ``str`` (None → ""); for ``json`` numeric and
    boolean values stay native, None → None (``null``).
    """
    if value is None:
        return "" if fmt == "csv" else None
    if ftype in ("date", "datetime"):
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        value = str(value)
        return value  # already a string in both formats
    if fmt == "csv":
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
    # json: keep native scalars
    return value


_TRUE = {"true", "1", "yes", "y", "t"}
_FALSE = {"false", "0", "no", "n", "f"}


def coerce_and_check(cell: Any, ftype: str) -> tuple[bool, Any]:
    """The single validation primitive used by `test` for CSV and JSON alike.

    Empty/None is treated as null (v1 fields are nullable) → (True, None).
    Otherwise attempt to parse `cell` to `ftype`; coercion is a no-op for
    already-typed JSON values, so CSV "42" and JSON 42 both pass `integer`.
    Returns (ok, coerced_value).
    """
    if cell is None or (isinstance(cell, str) and cell == ""):
        return True, None

    if ftype == "string":
        return True, str(cell)

    if ftype == "boolean":
        if isinstance(cell, bool):
            return True, cell
        s = str(cell).strip().lower()
        if s in _TRUE:
            return True, True
        if s in _FALSE:
            return True, False
        return False, cell

    if ftype == "integer":
        if isinstance(cell, bool):
            return False, cell
        if isinstance(cell, int):
            return True, cell
        try:
            f = float(str(cell))
        except (TypeError, ValueError):
            return False, cell
        if f.is_integer():
            return True, int(f)
        return False, cell

    if ftype == "number":
        if isinstance(cell, bool):
            return False, cell
        if isinstance(cell, (int, float)):
            return True, float(cell)
        try:
            return True, float(str(cell))
        except (TypeError, ValueError):
            return False, cell

    if ftype == "date":
        try:
            return True, date.fromisoformat(str(cell))
        except (TypeError, ValueError):
            return False, cell

    if ftype == "datetime":
        s = str(cell).replace("Z", "+00:00")
        try:
            return True, datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return False, cell

    return False, cell


# --------------------------------------------------------------------------- #
# Distributions (ADR-0004)
# --------------------------------------------------------------------------- #
def weights_from_counts(value_counts: dict[str, int]) -> dict[str, float]:
    """Normalize summed observation counts to a probability map."""
    total = sum(int(v) for v in value_counts.values())
    if total <= 0:
        n = len(value_counts) or 1
        return {k: round(1.0 / n, 6) for k in value_counts}
    return {k: round(int(v) / total, 6) for k, v in value_counts.items()}


def merge_observations(per_page: list[dict], cap: int = MAX_EXAMPLES_PER_FIELD) -> list[dict]:
    """Merge per-page discovery results by field name.

    Each per-page entry is ``{"fields": [{name, type_hint, value_counts}]}``.
    Distinct values are unioned (capped) and `value_counts` summed, so both
    cardinality and frequency survive across pages.
    """
    merged: "OrderedDict[str, dict]" = OrderedDict()
    for page in per_page:
        for field in page.get("fields", []):
            name = field.get("name")
            if not name:
                continue
            slot = merged.setdefault(
                name, {"name": name, "type_hint": field.get("type_hint", "string"),
                       "value_counts": {}}
            )
            for value, count in (field.get("value_counts") or {}).items():
                slot["value_counts"][value] = slot["value_counts"].get(value, 0) + int(count)
    # Cap distinct values (keep the most frequent).
    for slot in merged.values():
        vc = slot["value_counts"]
        if len(vc) > cap:
            top = sorted(vc.items(), key=lambda kv: kv[1], reverse=True)[:cap]
            slot["value_counts"] = dict(top)
    return list(merged.values())


# --------------------------------------------------------------------------- #
# Lint + fix + document
# --------------------------------------------------------------------------- #
def lint_schema(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Offline checks → list of issue dicts.

    Each issue: {field, issue, severity, recommendation, autofixable, fixed?}.
    """
    from pocsynth.generate import valid_faker_providers  # local import: avoids cycle

    issues: list[dict[str, Any]] = []
    providers = valid_faker_providers()
    for field in schema["fields"]:
        name = field["name"]
        faker = field.get("faker")
        has_enum = "enum" in field
        # Unknown faker provider.
        if faker and faker not in providers:
            issues.append({
                "field": name, "issue": "unknown_faker_provider",
                "severity": "error",
                "recommendation": f"{faker!r} is not a known Faker provider",
                "autofixable": False,
            })
        # enum + faker both set → ambiguous.
        if faker and has_enum:
            issues.append({
                "field": name, "issue": "enum_and_faker",
                "severity": "warning",
                "recommendation": "both 'enum' and 'faker' set; enum wins, drop 'faker'",
                "autofixable": True, "fixed": {"drop": "faker"},
            })
        # regex that doesn't compile.
        if "regex" in field:
            try:
                re.compile(field["regex"])
            except re.error as exc:
                issues.append({
                    "field": name, "issue": "bad_regex", "severity": "error",
                    "recommendation": f"regex does not compile: {exc}",
                    "autofixable": False,
                })
        # PII provider + literal enum (ADR-0005 backstop).
        if faker in PII_FAKER_PROVIDERS and has_enum:
            issues.append({
                "field": name, "issue": "pii_enum",
                "severity": "error",
                "recommendation": (
                    f"{name!r} uses PII provider {faker!r} with a real-value enum; "
                    "dropping enum/weights so real values cannot leak"
                ),
                "autofixable": True, "fixed": {"drop": ["enum", "weights"]},
            })
        # Missing description (informational).
        if not field.get("description"):
            issues.append({
                "field": name, "issue": "missing_description",
                "severity": "info",
                "recommendation": "add a one-line description for the data dictionary",
                "autofixable": False,
            })
    return issues


def apply_fixes(
    schema: dict[str, Any], lint: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a new schema with autofixable issues applied + the change list."""
    fixed = json.loads(json.dumps(schema))  # deep copy
    by_name = {f["name"]: f for f in fixed["fields"]}
    changes: list[dict[str, Any]] = []
    for issue in lint:
        if not issue.get("autofixable"):
            continue
        field = by_name.get(issue["field"])
        if field is None:
            continue
        drop = issue.get("fixed", {}).get("drop")
        drop_keys = [drop] if isinstance(drop, str) else (drop or [])
        removed = [k for k in drop_keys if k in field]
        for k in removed:
            field.pop(k, None)
        if removed:
            changes.append({"field": issue["field"], "issue": issue["issue"],
                            "removed": removed})
    return fixed, changes


def document_schema(schema: dict[str, Any]) -> str:
    """Render a Markdown data dictionary."""
    name = schema.get("name", "dataset")
    lines = [f"# Schema: {name}", ""]
    if schema.get("description"):
        lines += [schema["description"], ""]
    lines += [
        "| field | type | source | constraints | description |",
        "|---|---|---|---|---|",
    ]
    for f in schema["fields"]:
        source = f.get("faker") and f"faker.{f['faker']}" or (
            "enum" if "enum" in f else ("regex" if "regex" in f else "—")
        )
        constraints = []
        if "enum" in f:
            constraints.append("enum=" + "/".join(map(str, f["enum"])))
        if "weights" in f:
            constraints.append("weighted")
        if "regex" in f:
            constraints.append(f"regex=`{f['regex']}`")
        lines.append(
            f"| {f['name']} | {f['type']} | {source} | "
            f"{', '.join(constraints) or '—'} | {f.get('description', '')} |"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Bedrock toolConfig builders (ADR-0002) — used by the paid stages
# --------------------------------------------------------------------------- #
_JSON_TYPE = {
    "string": "string", "integer": "integer", "number": "number",
    "boolean": "boolean", "date": "string", "datetime": "string",
}


def schema_to_toolspec(schema: dict[str, Any]) -> dict[str, Any]:
    """Conform-mode extract tool: records of the known schema's fields."""
    props = {}
    for f in schema["fields"]:
        p: dict[str, Any] = {"type": _JSON_TYPE.get(f["type"], "string")}
        if "enum" in f:
            p["enum"] = list(f["enum"])
        props[f["name"]] = p
    names = field_names(schema)
    return {
        "tools": [{
            "toolSpec": {
                "name": "extract_records",
                "description": "Return the records found, matching the given fields.",
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {"records": {
                        "type": "array",
                        "items": {"type": "object", "properties": props,
                                  "required": names},
                    }},
                    "required": ["records"],
                }},
            }
        }],
        "toolChoice": {"tool": {"name": "extract_records"}},
    }


def discovery_toolspec() -> dict[str, Any]:
    """Discovery-mode extract tool: observed fields with value counts."""
    return {
        "tools": [{
            "toolSpec": {
                "name": "observe_fields",
                "description": (
                    "Report each distinct field observed on the page, with the "
                    "distinct values seen and how many times each occurred."
                ),
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {"fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type_hint": {"type": "string"},
                                "value_counts": {"type": "object"},
                            },
                            "required": ["name", "value_counts"],
                        },
                    }},
                    "required": ["fields"],
                }},
            }
        }],
        "toolChoice": {"tool": {"name": "observe_fields"}},
    }


def schema_infer_toolspec() -> dict[str, Any]:
    """schema-infer / from-prompt tool: emit a generation-ready schema."""
    field_obj = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "type": {"type": "string", "enum": sorted(FIELD_TYPES)},
            "faker": {"type": "string"},
            "enum": {"type": "array", "items": {"type": "string"}},
            "weights": {"type": "object"},
            "regex": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["name", "type"],
    }
    return {
        "tools": [{
            "toolSpec": {
                "name": "emit_schema",
                "description": "Emit a generation-ready synthetic-data schema.",
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "fields": {"type": "array", "items": field_obj},
                    },
                    "required": ["name", "fields"],
                }},
            }
        }],
        "toolChoice": {"tool": {"name": "emit_schema"}},
    }
