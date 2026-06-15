# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Affirmative safety verification of generated output (the `verify` command).

`run_verify(cfg)` answers the question the PII guard (ADR-0005) could only
*design for*: does this generated dataset actually contain any real source value?
It scans the generated **Rows** AND the **Schema** artifact (enum values, regex
patterns, descriptions — the Schema is shared too) against the real PII values
recorded in the originating **Sample**, and emits an **Attestation** (ADR-0010).

Matching is on the Comprehend-flagged PII values from the Sample, by exact
whole-value containment — not blanket substring scanning. The rows are scanned
both as raw serialized text and as decoded cell values, so a value that leaked
into a cell can't hide behind CSV quote-doubling or JSON escaping. Non-PII real
values (state codes, plan tiers) are *allowed* to survive as enums by design, so
they are not scanned. Offline, free, no AWS.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocsynth import __version__
from pocsynth.errors import SchemaError

EventCallback = Callable[..., None] | None


def _noop(*_a, **_k) -> None:
    pass


@dataclass
class VerifyConfig:
    rows_path: str
    sample_path: str
    schema: dict[str, Any] | None = None      # the shared Schema artifact (also scanned)
    rows_in_format: str | None = None          # csv | json; inferred from extension
    output_dir: str | None = None              # where the Attestation is written
    min_value_len: int = 4                     # ignore very short values (false-positive guard)


# --------------------------------------------------------------------------- #
# Real-value extraction from the Sample
# --------------------------------------------------------------------------- #
def _real_pii_values(sample: dict[str, Any], min_len: int) -> set[str]:
    """Collect the real, Comprehend-flagged PII values recorded in a Sample.

    Discovery sample: fields carry `pii: true` + `value_counts` whose KEYS are
    the distinct real values. Conform sample: records keyed by field; PII fields
    are not flagged per-field in conform mode, so we fall back to every value of
    every field (conform records ARE real source values). Short values are
    dropped to avoid false positives on codes like "CA".
    """
    values: set[str] = set()
    fields = sample.get("fields")
    records = sample.get("records")

    if isinstance(fields, list):  # discovery sample
        for f in fields:
            if not isinstance(f, dict) or not f.get("pii"):
                continue
            for v in (f.get("value_counts") or {}):
                s = str(v).strip()
                if len(s) >= min_len:
                    values.add(s)
    elif isinstance(records, list):  # conform sample — every recorded value is real
        for rec in records:
            if not isinstance(rec, dict):
                continue
            for v in rec.values():
                if v is None:
                    continue
                s = str(v).strip()
                if len(s) >= min_len:
                    values.add(s)
    return values


def _real_value_fields(sample: dict[str, Any], min_len: int) -> dict[str, str]:
    """Map each real PII value to the Sample field it came from, so a leak can
    name the offending field. Mirrors `_real_pii_values`' extraction rules; the
    first field a value appears under wins (a value rarely repeats across fields).
    """
    mapping: dict[str, str] = {}
    fields = sample.get("fields")
    records = sample.get("records")
    if isinstance(fields, list):  # discovery sample
        for f in fields:
            if not isinstance(f, dict) or not f.get("pii"):
                continue
            name = str(f.get("name", "")) or "?"
            for v in (f.get("value_counts") or {}):
                s = str(v).strip()
                if len(s) >= min_len:
                    mapping.setdefault(s, name)
    elif isinstance(records, list):  # conform sample
        for rec in records:
            if not isinstance(rec, dict):
                continue
            for k, v in rec.items():
                if v is None:
                    continue
                s = str(v).strip()
                if len(s) >= min_len:
                    mapping.setdefault(s, str(k))
    return mapping


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def _load_rows_text(path: Path, in_format: str | None) -> tuple[str, str]:
    """Return (raw_text, sha256) of the rows file. Raw text is enough: we scan
    for whole-value containment, format-agnostically."""
    if not path.exists():
        raise SchemaError(f"rows file not found: {path}", context={"path": str(path)})
    raw = path.read_text(encoding="utf-8", errors="replace")
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _schema_searchable_text(schema: dict[str, Any]) -> str:
    """The parts of a Schema a real value could leak into: enum values, regex
    patterns, descriptions, names. Tolerates a malformed schema (e.g. a model-
    emitted `fields: null`) — verify is a safety gate and must never crash."""
    parts: list[str] = []
    fields = schema.get("fields")
    for f in fields if isinstance(fields, list) else []:
        if not isinstance(f, dict):
            continue
        parts.append(str(f.get("name", "")))
        parts.append(str(f.get("description", "")))
        parts.append(str(f.get("regex", "")))
        enum = f.get("enum")
        for v in enum if isinstance(enum, list) else []:
            parts.append(str(v))
        weights = f.get("weights")
        for k in (weights if isinstance(weights, dict) else {}):
            parts.append(str(k))
    return "\n".join(parts)


def _decoded_rows_text(rows_text: str) -> str:
    """The rows' DECODED cell values, joined — so a real value that leaked into a
    cell survives format escaping in the scan. A raw substring scan over the
    serialized text misses `Robert "Bob" Smith` because CSV writes it as
    `"Robert ""Bob"" Smith"` and JSON as `Robert \\"Bob\\" Smith`; decoding undoes
    both. Best-effort and never raises (verify is a safety gate): if the text
    parses as neither CSV nor JSON, returns "" and the raw scan still runs.

    Tries JSON first (a streamed array of objects), then CSV. Returns the
    concatenation of every decoded scalar cell value.
    """
    text = rows_text.strip()
    if not text:
        return ""
    parts: list[str] = []
    # JSON: a list of row objects (the generate/stream JSON shape).
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                parts.extend(str(v) for v in row.values() if v is not None)
            elif row is not None:
                parts.append(str(row))
        return "\n".join(parts)
    # CSV: header + rows. Decoding undoes quote-doubling and quoted newlines.
    try:
        reader = csv.reader(io.StringIO(rows_text))
        for record in reader:
            parts.extend(cell for cell in record if cell)
    except (csv.Error, ValueError):
        return "\n".join(parts)
    return "\n".join(parts)


def verify_values(
    real_values: set[str], rows_text: str, schema: dict[str, Any] | None,
    value_fields: dict[str, str] | None = None,
) -> tuple[str, list[dict[str, Any]], bool]:
    """Core scan, shared by the CLI `verify` and the UI safety panel.

    Given the set of real PII values, the generated rows as text, and the
    (optional) Schema artifact, return (verdict, leaks, schema_scanned). The
    verdict is `not_applicable` when there are no real values to check (a public
    or synthetic seed), else `fail` if any value appears in the rows or schema,
    else `pass`. Leaks carry only a masked preview — never the full value.

    The rows are scanned BOTH as raw serialized text and as decoded cell values,
    so a real value that leaked into a cell can't hide behind CSV quote-doubling
    or JSON string escaping (a fail-open in a safety gate). `value_fields`
    optionally maps a real value to the source field it came from, so a leak can
    name the offending field (`field` key) for the caller.
    """
    schema_text = _schema_searchable_text(schema) if schema else ""
    schema_scanned = bool(schema_text)

    if not real_values:
        return "not_applicable", [], schema_scanned

    decoded_rows_text = _decoded_rows_text(rows_text)

    leaks: list[dict[str, Any]] = []
    for val in sorted(real_values):
        in_rows = val in rows_text or (bool(decoded_rows_text) and val in decoded_rows_text)
        in_schema = schema_scanned and val in schema_text
        if in_rows or in_schema:
            where = []
            if in_rows:
                where.append("rows")
            if in_schema:
                where.append("schema")
            leak: dict[str, Any] = {"value_preview": _mask(val), "where": where}
            field = (value_fields or {}).get(val)
            if field:
                leak["field"] = field
            leaks.append(leak)

    verdict = "fail" if leaks else "pass"
    return verdict, leaks, schema_scanned


def run_verify(cfg: VerifyConfig, on_event: EventCallback = None) -> dict[str, Any]:
    emit = on_event or _noop

    sample_p = Path(cfg.sample_path)
    if not sample_p.exists():
        raise SchemaError(f"sample file not found: {sample_p}",
                          context={"path": str(sample_p)})
    try:
        sample = json.loads(sample_p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaError(f"sample is not valid JSON: {exc}",
                          context={"path": str(sample_p)}) from exc

    real_values = _real_pii_values(sample, cfg.min_value_len)
    rows_path = Path(cfg.rows_path)
    rows_text, rows_hash = _load_rows_text(rows_path, cfg.rows_in_format)

    # Fail CLOSED on a JSON rows file that won't parse. Otherwise the decoded
    # scan silently falls back to a CSV decode of JSON text, which leaves `\"`
    # escapes intact — an escaped real value would then scan as clean (pass).
    # A safety gate must never silently under-scan; an unparseable JSON file is
    # truncated/corrupt and the verdict can't be trusted.
    effective_format = (cfg.rows_in_format
                        or ("json" if rows_path.suffix.lower() == ".json" else "csv"))
    if effective_format == "json" and rows_text.strip():
        try:
            json.loads(rows_text)
        except json.JSONDecodeError as exc:
            raise SchemaError(
                f"rows file is not valid JSON (truncated or corrupt): {exc}",
                context={"path": str(rows_path)},
                hint="Re-generate the rows; a partial JSON file can't be safely verified.",
            ) from exc

    emit("verify_started", candidate_values=len(real_values))

    value_fields = _real_value_fields(sample, cfg.min_value_len)
    verdict, leaks, schema_scanned = verify_values(
        real_values, rows_text, cfg.schema, value_fields)
    source = sample.get("source", "")
    source_hash = hashlib.sha256(str(source).encode("utf-8")).hexdigest() if source else None

    attestation = {
        "schema": 1,
        "verdict": verdict,
        "tool_version": __version__,
        "source": source,
        "source_hash": source_hash,
        "rows_path": str(cfg.rows_path),
        "rows_sha256": rows_hash,
        "candidate_pii_values": len(real_values),
        "leaks": leaks,
        "scanned": {"rows": True, "schema": schema_scanned},
    }

    att_path = None
    if cfg.output_dir:
        parent = Path(cfg.output_dir)
        parent.mkdir(parents=True, exist_ok=True)
        att_path = str(parent / "attestation.json")
        Path(att_path).write_text(json.dumps(attestation, indent=2), encoding="utf-8")
    attestation["attestation_path"] = att_path

    emit("verify_complete", verdict=verdict, leaks=len(leaks))
    # Field names a leak was attributed to (deduped, order-preserving) — a
    # convenience view over `leaks` for callers that just want the field list.
    leaked_fields = list(dict.fromkeys(
        lk["field"] for lk in leaks if lk.get("field")))
    return {
        "input": {"rows": str(cfg.rows_path), "sample": str(cfg.sample_path),
                  "schema_scanned": schema_scanned},
        "verdict": verdict,
        "leaked_fields": leaked_fields,
        "attestation": attestation,
        "cost": None,
    }


def _mask(value: str) -> str:
    """A non-reversible preview so the Attestation itself never carries the full
    real value (it would otherwise reintroduce the leak it reports)."""
    v = str(value)
    if len(v) <= 4:
        return "*" * len(v)
    return v[:2] + "*" * (len(v) - 4) + v[-2:]
