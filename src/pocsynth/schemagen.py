# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The `schema` command: turn a sample / prompt / user-schema into a
generation-ready schema + documentation + a lint report.

Three sources (ADR-0008):
  * lint        — a user schema → report/fixes (free, offline)
  * infer       — an extract sample → schema (paid, Bedrock)
  * from_prompt — a natural-language description → schema (paid, Bedrock)

All paid paths share the forced `emit_schema` toolConfig (ADR-0002). The PII
guard (ADR-0005) and distribution resolution (ADR-0004) run after inference.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pocsynth import schema as schema_mod
from pocsynth.bedrock import MODELS, make_session, read_tool_use, translate_aws_error
from pocsynth.errors import SchemaError
from pocsynth.prompts import build_schema_from_prompt_prompt, build_schema_infer_prompt
from pocsynth.schema import (
    PII_FAKER_PROVIDERS,
    apply_fixes,
    document_schema,
    lint_schema,
    schema_infer_toolspec,
    weights_from_counts,
)

EventCallback = Callable[..., None] | None


def _noop(*_a, **_k) -> None:
    pass


@dataclass
class SchemaConfig:
    sample_path: str | None = None
    in_schema_path: str | None = None
    prompt: str | None = None
    distribution: str = "auto"  # auto | infer | synthetic | uniform
    model_key: str = "sonnet"
    fix: bool = False
    max_tokens: int = 8000
    output_dir: str | None = None
    region: str | None = None
    profile: str | None = None
    bedrock_client: Any = field(default=None, repr=False)

    @property
    def mode(self) -> str:
        if self.sample_path:
            return "infer"
        if self.prompt:
            return "from_prompt"
        return "lint"


def _read_tooluse(response: dict, tool_name: str) -> dict:
    payload = read_tool_use(response, tool_name)
    if payload is None:
        raise SchemaError(
            f"model did not call the {tool_name!r} tool",
            context={"stop_reason": response.get("stopReason")},
            hint="Retry; if persistent, the model/region may not support tool use",
        )
    return payload


def _converse_for_schema(cfg: SchemaConfig, prompt: str) -> tuple[dict, dict]:
    """Run one forced-tool converse; return (draft_schema, usage)."""
    client = cfg.bedrock_client
    if client is None:
        session = make_session(profile=cfg.profile, region=cfg.region)
        client = session.client("bedrock-runtime")
    try:
        response = client.converse(
            modelId=MODELS[cfg.model_key]["id"],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": cfg.max_tokens, "temperature": 0},
            toolConfig=schema_infer_toolspec(),
        )
    except Exception as exc:  # noqa: BLE001 - translated below
        raise translate_aws_error(exc, service="bedrock") from exc
    draft = _read_tooluse(response, "emit_schema")
    draft.setdefault("schema", 1)
    usage = response.get("usage", {}) or {}
    return draft, usage


# Type → safe fallback Faker provider when the model proposes an invalid one.
_FALLBACK_BY_TYPE = {
    "string": "word",
    "integer": "random_int",
    "number": "pyfloat",
    "boolean": "boolean",
    "date": "date_object",
    "datetime": "date_time",
}


def _sanitize_providers(schema: dict) -> list[dict]:
    """Coerce model-proposed `faker` values that aren't real bare providers to a
    type-appropriate fallback, so a recoverable model quirk (a hallucinated
    `book.title`, a dotted/namespaced name, or a call expression like
    `bothify(...)`) never hard-fails generation. Returns lint-style notes.

    Fields already bound by `enum`/`regex` keep those (they win in generation);
    only a leftover invalid `faker` on such a field is dropped.
    """
    from pocsynth.generate import valid_faker_providers

    valid = valid_faker_providers()
    notes: list[dict] = []
    for f in schema.get("fields", []):
        faker = f.get("faker")
        if not faker or faker in valid:
            continue
        if f.get("enum") or f.get("regex"):
            # enum/regex already drives this field; just drop the bad faker.
            f.pop("faker", None)
            fixed_to = "enum" if f.get("enum") else "regex"
        else:
            fixed_to = _FALLBACK_BY_TYPE.get(f.get("type", "string"), "word")
            f["faker"] = fixed_to
        notes.append({
            "field": f["name"], "issue": "invalid_faker_provider_coerced",
            "severity": "warning",
            "recommendation": (
                f"{faker!r} is not a valid Faker provider; "
                f"using {fixed_to} so generation stays deterministic and offline"
            ),
        })
    return notes


def _apply_pii_guard(schema: dict, sample_pii: set[str]) -> list[dict]:
    """Strip real-value enums from PII fields (ADR-0005). Returns lint notes."""
    notes: list[dict] = []
    for f in schema["fields"]:
        is_pii = f["name"] in sample_pii or f.get("faker") in PII_FAKER_PROVIDERS
        if is_pii and ("enum" in f or "weights" in f):
            f.pop("enum", None)
            f.pop("weights", None)
            f.pop("weights_source", None)
            if not f.get("faker") and not f.get("regex"):
                f["faker"] = "word"
            notes.append({
                "field": f["name"], "issue": "pii_enum_suppressed",
                "severity": "info",
                "recommendation": (
                    f"{f['name']!r} is PII → real-value enum suppressed; "
                    f"using faker.{f.get('faker', 'word')}; real values discarded"
                ),
            })
    return notes


def _resolve_distribution(
    schema: dict, mode: str, counts_by_field: dict[str, dict]
) -> dict[str, str]:
    """Set per-field weights per the distribution mode. Returns weights_source map."""
    per_field: dict[str, str] = {}
    for f in schema["fields"]:
        name = f["name"]
        if "enum" not in f:
            continue
        counts = counts_by_field.get(name)
        if mode == "uniform":
            f.pop("weights", None)
            source = "uniform"
        elif mode == "synthetic":
            # Keep the model's invented weights, if any.
            source = "synthetic" if f.get("weights") else "uniform"
        else:  # infer or auto: prefer exact counts, else fall back to synthetic
            if counts:
                f["weights"] = weights_from_counts(counts)
                source = "infer"
            else:
                source = "synthetic" if f.get("weights") else "uniform"
        f["weights_source"] = source
        per_field[name] = source
    return per_field


def run_schema(cfg: SchemaConfig, on_event: EventCallback = None) -> dict[str, Any]:
    emit = on_event or _noop
    n_sources = sum(bool(x) for x in (cfg.sample_path, cfg.in_schema_path, cfg.prompt))
    if n_sources != 1:
        raise SchemaError(
            "provide exactly one of sample_path / in_schema_path / prompt"
        )

    parent = Path(cfg.output_dir) if cfg.output_dir else Path.cwd()
    parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    usage: dict[str, Any] = {}
    mode = cfg.mode
    source = cfg.sample_path or cfg.in_schema_path or "<prompt>"

    # ----- obtain the schema -----
    sample_pii: set[str] = set()
    counts_by_field: dict[str, dict] = {}

    if mode == "lint":
        schema = schema_mod.load_schema(cfg.in_schema_path)
    elif mode == "from_prompt":
        emit("schema_infer_started", mode=mode)
        schema, usage = _converse_for_schema(cfg, build_schema_from_prompt_prompt(cfg.prompt))
        schema_mod._validate_schema_shape(schema)
    else:  # infer
        emit("schema_infer_started", mode=mode)
        sample = json.loads(Path(cfg.sample_path).read_text(encoding="utf-8"))
        for fobs in sample.get("fields", []):
            if fobs.get("value_counts"):
                counts_by_field[fobs["name"]] = fobs["value_counts"]
        sample_pii = {f["name"] for f in sample.get("fields", []) if f.get("pii")}
        schema, usage = _converse_for_schema(
            cfg, build_schema_infer_prompt(sample.get("fields", []))
        )
        schema_mod._validate_schema_shape(schema)

    # ----- sanitize + PII guard + distribution (paid modes only) -----
    pii_notes: list[dict] = []
    provider_notes: list[dict] = []
    per_field_source: dict[str, str] = {}
    if mode in ("infer", "from_prompt"):
        # Coerce invalid model-proposed providers FIRST so the paid call never
        # hard-fails on a recoverable quirk (e.g. a hallucinated `book.title`).
        provider_notes = _sanitize_providers(schema)
        pii_notes = _apply_pii_guard(schema, sample_pii)
        dist_mode = cfg.distribution
        # from_prompt has no counts → infer/auto degrade to synthetic.
        per_field_source = _resolve_distribution(schema, dist_mode, counts_by_field)

    # ----- lint + (optional) fix -----
    lint = lint_schema(schema)
    lint_all = lint + provider_notes + pii_notes
    fixed_schema_path = None
    applied_changes: list[dict] = []
    out_schema = schema
    if cfg.fix:
        out_schema, applied_changes = apply_fixes(schema, lint)
        fixed_schema_path = str(parent / "schema.fixed.json")
        Path(fixed_schema_path).write_text(json.dumps(out_schema, indent=2), encoding="utf-8")

    # ----- write artifacts -----
    schema_path = None
    if mode in ("infer", "from_prompt"):
        schema_path = str(parent / "schema.json")
        Path(schema_path).write_text(json.dumps(out_schema, indent=2), encoding="utf-8")
    doc_path = str(parent / "schema.md")
    Path(doc_path).write_text(document_schema(out_schema), encoding="utf-8")
    lint_report_path = str(parent / "lint_report.json")
    Path(lint_report_path).write_text(json.dumps(lint_all, indent=2), encoding="utf-8")

    wall = round(time.monotonic() - start, 3)
    emit("schema_complete", mode=mode, issues=len(lint_all))

    result: dict[str, Any] = {
        "input": {"mode": mode, "source": source,
                  "distribution": cfg.distribution if mode != "lint" else None},
        "output": {
            "dir": str(parent), "schema_path": schema_path, "doc_path": doc_path,
            "lint_report_path": lint_report_path, "fixed_schema_path": fixed_schema_path,
            "wall_time_seconds": wall,
        },
        "lint": {
            "issues_total": len(lint_all),
            "autofixable": sum(1 for i in lint if i.get("autofixable")),
            "applied": applied_changes if cfg.fix else None,
            "notes": lint_all,
        },
        "fields": len(out_schema["fields"]),
    }
    if mode in ("infer", "from_prompt"):
        result["distribution"] = {
            "requested": cfg.distribution,
            "per_field_source": per_field_source,
        }
        result["output"]["bedrock_usage"] = {
            "input_tokens": int(usage.get("inputTokens", 0) or 0),
            "output_tokens": int(usage.get("outputTokens", 0) or 0),
        }
        result["cost"] = None  # wired by the CLI like convert
    else:
        result["cost"] = None
    return result
