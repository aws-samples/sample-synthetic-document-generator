# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One-shot orchestration verb (`run`) — safe-by-default (ADR-0011).

`run` chains the whole pipeline so no persona threads artifacts by hand:

  --preset NAME    → load preset schema → generate                 (free)
  --prompt "…"     → schema(from-prompt) → generate                (paid)
  --document FILE  → extract → schema(infer) → generate → verify    (paid)

Two defaults make it safe rather than surprising:

* **Code-enforced cost gate** — the paid paths auto-estimate and refuse to spend
  when the projected cost exceeds a threshold unless explicitly confirmed
  (`assume_yes`). The gate is enforced here in code, not as an SKILL.md
  instruction an agent could skip.
* **Fail-closed verify** — the document path always runs `verify`; a failed
  attestation aborts with the leak detail and marks the output NOT cleared for
  sharing (the dataset is still written for inspection). Override is explicit
  (`share_anyway`) and recorded in the result.

The preset / prompt paths are synthetic by construction (no real source), so
their verdict is `not_applicable` and the output is cleared for sharing.

This module reuses the existing run_* functions verbatim — no new generation,
extraction, or verification logic — so the one-shot path can never drift from
the individual verbs.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocsynth.bedrock import DEFAULT_MAX_TOKENS, DEFAULT_MODEL
from pocsynth.errors import CostGateError, LeakDetectedError, SchemaError
from pocsynth.pricing import (
    DEFAULT_CHARS_PER_TOKEN,
    SCHEMA_INFER_OUTPUT_TOKENS,
    actual_convert_cost,
    estimate_bedrock_cost,
    estimate_extract_cost,
    load_pricing,
)

EventCallback = Callable[..., None] | None

# ADR-0011: the cost gate trips above this projected spend unless confirmed.
DEFAULT_COST_THRESHOLD_USD = 0.10


def _noop(*_a, **_k) -> None:
    pass


@dataclass
class RunConfig:
    # Exactly one seed source:
    preset: str | None = None
    prompt: str | None = None
    document: str | None = None       # PDF path or https:// URL

    rows: int = 100
    export_format: str = "csv"        # csv | json
    seed: int | None = None
    locale: str = "en_US"

    # Paid-path knobs.
    model_key: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    pii_audit: bool = True

    # Safe-by-default controls (ADR-0011).
    assume_yes: bool = False          # confirm the cost gate (CLI --yes / UI confirm)
    gate: bool = True                 # False = --no-gate, skip the cost gate entirely
    cost_threshold: float = DEFAULT_COST_THRESHOLD_USD
    share_anyway: bool = False        # override a failed verify (deliberate + logged)

    output_dir: str | None = None
    region: str | None = None
    profile: str | None = None

    def seed_source(self) -> str:
        n = sum(bool(x) for x in (self.preset, self.prompt, self.document))
        if n != 1:
            raise SchemaError(
                "provide exactly one seed source: --preset, --prompt, or --document",
                hint="e.g. run --preset crm_contacts --rows 1000  OR  "
                     "run --document intake.pdf --yes",
            )
        if self.preset:
            return "preset"
        if self.prompt:
            return "prompt"
        return "document"

    def is_paid(self) -> bool:
        return self.seed_source() in ("prompt", "document")


# --------------------------------------------------------------------------- #
# Cost gate (enforced BEFORE any paid call)
# --------------------------------------------------------------------------- #
def _estimate_paid_cost(cfg: RunConfig, pricing: dict[str, Any], region: str | None) -> dict[str, Any]:
    """Pre-flight projection for the paid path. Heuristic (±30-50%); the gate
    only needs to know whether we are clearly under/over the threshold."""
    source = cfg.seed_source()
    if source == "prompt":
        input_tokens = math.ceil(len(cfg.prompt or "") / DEFAULT_CHARS_PER_TOKEN) + 300
        bedrock = estimate_bedrock_cost(
            cfg.model_key, input_tokens, SCHEMA_INFER_OUTPUT_TOKENS, pricing, region=region
        )
        return {"total_cost_usd": bedrock["total_cost_usd"], "method": "prompt-heuristic",
                "breakdown": {"schema_from_prompt": bedrock["total_cost_usd"]}}

    # document: extract dominates; add a small schema-infer constant on top.
    extract = estimate_extract_cost(
        cfg.document, cfg.model_key, pricing,
        pii_audit=cfg.pii_audit, region=region,
    )
    schema_infer = estimate_bedrock_cost(
        cfg.model_key, 600, SCHEMA_INFER_OUTPUT_TOKENS, pricing, region=region
    )
    total = round(extract["total_cost_usd"] + schema_infer["total_cost_usd"], 6)
    return {"total_cost_usd": total, "method": "document-heuristic",
            "breakdown": {"extract": extract["total_cost_usd"],
                          "schema_infer": schema_infer["total_cost_usd"]}}


def _apply_cost_gate(cfg: RunConfig, pricing: dict[str, Any], region: str | None,
                     emit: Callable[..., None]) -> dict[str, Any]:
    estimate = _estimate_paid_cost(cfg, pricing, region)
    projected = estimate["total_cost_usd"]
    emit("cost_estimated", total_cost_usd=projected, threshold=cfg.cost_threshold)
    if cfg.gate and not cfg.assume_yes and projected > cfg.cost_threshold:
        raise CostGateError(
            f"projected cost ${projected:.4f} exceeds the ${cfg.cost_threshold:.2f} "
            "gate; pass --yes to proceed (or --no-gate to disable the gate)",
            context={"estimate": estimate, "threshold": cfg.cost_threshold,
                     "seed_source": cfg.seed_source()},
            hint="Re-run with --yes once you've reviewed the estimate.",
        )
    return estimate


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_pipeline(cfg: RunConfig, on_event: EventCallback = None) -> dict[str, Any]:
    # Imported lazily so the free preset path never imports the paid stack.
    from pocsynth import presets as presets_mod
    from pocsynth.generate import GenerateConfig, run_generation

    emit = on_event or _noop
    source = cfg.seed_source()
    out_dir = cfg.output_dir or str(Path.cwd())

    steps: list[dict[str, Any]] = []
    costs: list[dict[str, Any]] = []
    estimate: dict[str, Any] | None = None
    region = None

    # ---- cost gate (paid paths only), before spending a cent ----
    if cfg.is_paid():
        from pocsynth.aws import resolve_region
        pricing = load_pricing()
        region, _src = resolve_region(cfg.region, cfg.profile)
        estimate = _apply_cost_gate(cfg, pricing, region, emit)

    emit("run_started", seed_source=source, rows=cfg.rows)

    # ---- obtain a schema (+ sample, for the document path) ----
    sample_path: str | None = None
    schema_dict: dict[str, Any]

    if source == "preset":
        schema_dict = presets_mod.load_preset(cfg.preset)
        steps.append({"step": "load_preset", "preset": cfg.preset})

    elif source == "prompt":
        schema_dict, schema_path, cost = _run_schema_from_prompt(cfg, out_dir, emit)
        steps.append({"step": "schema_from_prompt", "schema_path": schema_path})
        if cost:
            costs.append({"step": "schema", **cost})

    else:  # document
        sample_path, extract_cost = _run_extract(cfg, out_dir, emit, steps)
        schema_dict, schema_path, schema_cost = _run_schema_from_sample(
            cfg, sample_path, out_dir, emit
        )
        steps.append({"step": "schema_infer", "schema_path": schema_path})
        if extract_cost:
            costs.append({"step": "extract", **extract_cost})
        if schema_cost:
            costs.append({"step": "schema", **schema_cost})

    # ---- generate (free, offline) ----
    gen = run_generation(
        GenerateConfig(
            schema=schema_dict, rows=cfg.rows, export_format=cfg.export_format,
            seed=cfg.seed, locale=cfg.locale, output_dir=out_dir,
        ),
        on_event=lambda n, **p: emit(n, **p),
    )
    rows_path = gen["output"]["rows_path"]
    steps.append({"step": "generate", "rows_written": gen["output"]["rows_written"]})

    # ---- verify (document path: fail-closed; synthetic paths: not applicable) ----
    verdict, attestation, cleared = _verify_step(
        cfg, source, rows_path, sample_path, schema_dict, out_dir, emit
    )

    total_cost = round(sum(c.get("total_cost_usd", 0) or 0 for c in costs), 6) if costs else None

    result: dict[str, Any] = {
        "input": {
            "seed_source": source,
            "preset": cfg.preset, "prompt": cfg.prompt, "document": cfg.document,
            "rows": cfg.rows, "format": cfg.export_format, "seed": cfg.seed,
        },
        "steps": steps,
        "verdict": verdict,
        "cleared_for_sharing": cleared,
        "output": {
            "dir": out_dir,
            "rows_path": rows_path,
            "rows_written": gen["output"]["rows_written"],
            "sample_path": sample_path,
            "attestation_path": (attestation or {}).get("attestation_path"),
        },
        "attestation": attestation,
        "cost": {
            "estimated": estimate,
            "by_step": costs or None,
            "total_cost_usd": total_cost,
        } if cfg.is_paid() else None,
    }

    # ---- fail-closed gate (ADR-0011) ----
    if verdict == "fail" and not cfg.share_anyway:
        leaks = attestation.get("leaks", []) if attestation else []
        raise LeakDetectedError(
            f"verify failed: {len(leaks)} real source value(s) leaked into the "
            "generated output — NOT cleared for sharing",
            context={"attestation": attestation, "run": result},
            hint="Inspect result.context.run.output then attestation.leaks. Re-run "
                 "schema --fix or regenerate. Override with --share-anyway (logged).",
        )
    if verdict == "fail" and cfg.share_anyway:
        result["override_acknowledged"] = True
        emit("verify_override", verdict=verdict)

    emit("run_complete", verdict=verdict, cleared=cleared)
    return result


def _verify_step(cfg, source, rows_path, sample_path, schema_dict, out_dir, emit):
    """Returns (verdict, attestation, cleared_for_sharing)."""
    if source != "document":
        # Synthetic by construction — no real source to leak.
        return "not_applicable", None, True
    from pocsynth.verify import VerifyConfig, run_verify
    vres = run_verify(
        VerifyConfig(
            rows_path=rows_path, sample_path=sample_path, schema=schema_dict,
            rows_in_format=cfg.export_format, output_dir=out_dir,
        ),
        on_event=lambda n, **p: emit(n, **p),
    )
    verdict = vres["verdict"]
    cleared = verdict in ("pass", "not_applicable")
    return verdict, vres["attestation"], cleared


# --------------------------------------------------------------------------- #
# Paid-step wrappers (thin; mirror the CLI cost-wiring so `run` reports cost)
# --------------------------------------------------------------------------- #
def _bedrock_cost_from_usage(usage: dict[str, Any], cfg: RunConfig, region: str | None):
    if not usage:
        return None
    try:
        pricing = load_pricing()
        return estimate_bedrock_cost(
            cfg.model_key, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
            pricing, region=region,
        )
    except Exception:  # noqa: BLE001 - cost is advisory; never fail the run on pricing
        return None


def _run_extract(cfg: RunConfig, out_dir: str, emit, steps: list[dict[str, Any]]):
    from pocsynth.aws import resolve_region
    from pocsynth.extract import ExtractConfig, run_extraction
    res = run_extraction(
        ExtractConfig(
            pdf_url=cfg.document, schema=None, model_key=cfg.model_key,
            export_format="json", num_pages=None, max_tokens=cfg.max_tokens,
            pii_audit=cfg.pii_audit, region=cfg.region, profile=cfg.profile,
            output_dir=out_dir,
        ),
        on_event=lambda n, **p: emit(n, **p),
    )
    steps.append({"step": "extract",
                  "records_extracted": res["output"]["records_extracted"],
                  "pii_fields": res.get("pii_audit", {}).get("pii_fields", [])})
    cost = None
    try:
        pricing = load_pricing()
        region, _src = resolve_region(cfg.region, cfg.profile)
        cost = actual_convert_cost(res, pricing, model_key=cfg.model_key, region=region)
    except Exception:  # noqa: BLE001 - advisory
        cost = None
    return res["output"]["sample_path"], cost


def _run_schema_from_sample(cfg: RunConfig, sample_path: str, out_dir: str, emit):
    from pocsynth.aws import resolve_region
    from pocsynth.schema import load_schema
    from pocsynth.schemagen import SchemaConfig, run_schema
    res = run_schema(
        SchemaConfig(
            sample_path=sample_path, model_key=cfg.model_key, distribution="auto",
            locale=cfg.locale, max_tokens=cfg.max_tokens, output_dir=out_dir,
            region=cfg.region, profile=cfg.profile,
        ),
        on_event=lambda n, **p: emit(n, **p),
    )
    schema_path = res["output"]["schema_path"]
    region, _src = resolve_region(cfg.region, cfg.profile)
    cost = _bedrock_cost_from_usage(res["output"].get("bedrock_usage"), cfg, region)
    return load_schema(schema_path), schema_path, cost


def _run_schema_from_prompt(cfg: RunConfig, out_dir: str, emit):
    from pocsynth.aws import resolve_region
    from pocsynth.schema import load_schema
    from pocsynth.schemagen import SchemaConfig, run_schema
    res = run_schema(
        SchemaConfig(
            prompt=cfg.prompt, model_key=cfg.model_key, distribution="auto",
            locale=cfg.locale, max_tokens=cfg.max_tokens, output_dir=out_dir,
            region=cfg.region, profile=cfg.profile,
        ),
        on_event=lambda n, **p: emit(n, **p),
    )
    schema_path = res["output"]["schema_path"]
    region, _src = resolve_region(cfg.region, cfg.profile)
    cost = _bedrock_cost_from_usage(res["output"].get("bedrock_usage"), cfg, region)
    return load_schema(schema_path), schema_path, cost
