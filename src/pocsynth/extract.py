# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Structured extraction from a PDF via Bedrock (the paid `extract` stage).

`run_extraction(cfg)` pulls structured records (conform mode, given a schema)
or grouped field observations (discovery mode) out of a PDF using a forced
`toolConfig` call (ADR-0002), runs the Comprehend PII audit on the extracted
values (ADR-0005), and writes a sample file for `schema --infer`.
"""

from __future__ import annotations

import csv
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from pocsynth.bedrock import MODELS, make_session, read_tool_use, translate_aws_error
from pocsynth.comprehend import scan_for_pii
from pocsynth.errors import InputNotPdfError, PartialError
from pocsynth.pdf import get_pdf_file
from pocsynth.prompts import build_extract_prompt, build_extract_system_prompt
from pocsynth.schema import discovery_toolspec, merge_observations, schema_to_toolspec

EventCallback = Callable[..., None] | None


def _noop(*_a, **_k) -> None:
    pass


@dataclass
class ExtractConfig:
    pdf_url: str
    schema: dict[str, Any] | None = None  # None => discovery mode
    model_key: str = "sonnet"
    export_format: str = "json"  # json | csv | jsonl
    num_pages: int | None = None
    max_tokens: int = 8000
    pii_audit: bool = True
    region: str | None = None
    profile: str | None = None
    output_dir: str | None = None
    bedrock_client: Any = field(default=None, repr=False)
    comprehend_client: Any = field(default=None, repr=False)


def _pii_fields(records: list[dict], comprehend) -> set[str]:
    """Return field names whose concatenated values Comprehend flags as PII."""
    if comprehend is None:
        return set()
    by_field: dict[str, list[str]] = {}
    for rec in records:
        for k, v in rec.items():
            if v is None:
                continue
            by_field.setdefault(k, []).append(str(v))
    flagged: set[str] = set()
    for name, values in by_field.items():
        text = " ".join(values)
        try:
            resp = comprehend.detect_pii_entities(Text=text[:90_000], LanguageCode="en")
        except Exception:  # noqa: BLE001 - per-field probe; absence != PII
            continue
        if resp.get("Entities"):
            flagged.add(name)
    return flagged


def _records_to_csv(records: list[dict], fields: list[str], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rec in records:
            writer.writerow({k: rec.get(k, "") for k in fields})


def run_extraction(cfg: ExtractConfig, on_event: EventCallback = None) -> dict[str, Any]:
    emit = on_event or _noop
    conform = cfg.schema is not None
    mode = "conform" if conform else "discovery"
    tool = schema_to_toolspec(cfg.schema) if conform else discovery_toolspec()
    tool_name = "extract_records" if conform else "observe_fields"
    prompt_template = build_extract_prompt(mode, cfg.schema)
    system_prompts = [{"text": build_extract_system_prompt()}]

    pdf_bytes = get_pdf_file(cfg.pdf_url)

    # Clients (shared session; tests inject both).
    bedrock_client = cfg.bedrock_client
    comprehend_client = cfg.comprehend_client
    needs_session = bedrock_client is None or (cfg.pii_audit and comprehend_client is None)
    if needs_session:
        session = make_session(profile=cfg.profile, region=cfg.region)
        if bedrock_client is None:
            bedrock_client = session.client("bedrock-runtime")
        if cfg.pii_audit and comprehend_client is None:
            comprehend_client = session.client("comprehend")

    parent = Path(cfg.output_dir) if cfg.output_dir else Path.cwd()
    parent.mkdir(parents=True, exist_ok=True)

    try:
        doc_cm = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise InputNotPdfError(
            f"Could not parse PDF: {exc}",
            context={"path": cfg.pdf_url, "exception": type(exc).__name__},
            hint="Verify the file is a valid, non-encrypted PDF",
        ) from exc

    start = time.monotonic()
    total_input_tokens = 0
    total_output_tokens = 0
    pages_attempted = 0
    pages_processed = 0
    page_failures: list[dict] = []
    conform_records: list[dict] = []
    discovery_pages: list[dict] = []

    with doc_cm as doc:
        total_pages = len(doc)
        if cfg.num_pages is not None:
            total_pages = min(total_pages, cfg.num_pages)
        emit("extraction_started", total_pages=total_pages, mode=mode)

        for page_num in range(len(doc)):
            if cfg.num_pages and page_num >= cfg.num_pages:
                break
            pages_attempted += 1
            page = doc.load_page(page_num)
            pix = page.get_pixmap()
            img_bytes = pix.tobytes()
            page_text = page.get_text("text")
            prompt = prompt_template.replace("{page_text}", page_text)

            message = {
                "role": "user",
                "content": [
                    {"text": f"Page {page_num + 1}:"},
                    {"image": {"format": "png", "source": {"bytes": img_bytes}}},
                    {"text": prompt},
                ],
            }
            emit("page_started", page=page_num + 1, of=total_pages)
            try:
                response = bedrock_client.converse(
                    modelId=MODELS[cfg.model_key]["id"],
                    messages=[message],
                    system=system_prompts,
                    inferenceConfig={"maxTokens": cfg.max_tokens, "temperature": 0},
                    toolConfig=tool,
                )
            except Exception as exc:  # noqa: BLE001 - translated below
                err = translate_aws_error(exc, service="bedrock")
                page_failures.append({"page": page_num + 1, "error": err.code,
                                      "message": err.message})
                emit("page_failed", page=page_num + 1, of=total_pages, error_code=err.code)
                continue

            usage = response.get("usage", {}) or {}
            total_input_tokens += int(usage.get("inputTokens", 0) or 0)
            total_output_tokens += int(usage.get("outputTokens", 0) or 0)

            payload = read_tool_use(response, tool_name)
            if payload is None:
                page_failures.append({"page": page_num + 1, "error": "NO_TOOL_USE",
                                      "message": f"no {tool_name} toolUse block"})
                emit("page_failed", page=page_num + 1, of=total_pages, error_code="NO_TOOL_USE")
                continue

            if conform:
                conform_records.extend(payload.get("records", []) or [])
            else:
                discovery_pages.append({"fields": payload.get("fields", []) or []})
            pages_processed += 1
            emit("page_processed", page=page_num + 1, of=total_pages)

    wall = round(time.monotonic() - start, 3)

    if pages_attempted == 0:
        raise PartialError("PDF contained no pages", context={"pdf": cfg.pdf_url})
    if pages_processed == 0:
        raise PartialError(
            f"All {pages_attempted} pages failed to extract",
            context={"pdf": cfg.pdf_url, "pages_attempted": pages_attempted,
                     "page_failures": page_failures},
            hint="Inspect page_failures for the underlying errors",
        )

    # Assemble the sample.
    if conform:
        records = conform_records
        scan_records = records
    else:
        merged = merge_observations(discovery_pages)
        records = merged
        # For PII flagging in discovery, synthesize pseudo-records from the
        # observed distinct values. Separate distinct values with newlines (not
        # spaces) so multi-word values like "John Smith" aren't run together
        # with the next value into a spurious span.
        scan_records = [
            {f["name"]: "\n".join(map(str, f.get("value_counts", {}).keys()))}
            for f in merged
        ]

    # PII audit (ADR-0005).
    pii_path = None
    entities_found = 0
    pii_field_names: list[str] = []
    combined_text = ""
    if cfg.pii_audit and comprehend_client is not None:
        combined_text = json.dumps(records, default=str)
        stem = Path(cfg.pdf_url).stem or "extract"
        detected = scan_for_pii(
            combined_text, folder_name=str(parent / "pii-audit"), filename=stem,
            comprehend=comprehend_client,
        )
        entities_found = len(detected)
        pii_path = str(parent / "pii-audit" / f"{stem}_pii_scan_audit.csv")
        pii_field_names = sorted(_pii_fields(scan_records, comprehend_client))
        if not conform:
            flagged = set(pii_field_names)
            for f in records:
                f["pii"] = f["name"] in flagged

    # Write the sample.
    if conform:
        sample: dict[str, Any] = {"schema": 1, "source": cfg.pdf_url, "records": records}
    else:
        sample = {"schema": 1, "source": cfg.pdf_url, "fields": records}

    fmt = cfg.export_format
    if fmt == "csv" and conform:
        sample_path = parent / "sample.csv"
        fields = [f["name"] for f in cfg.schema["fields"]]
        _records_to_csv(records, fields, sample_path)
    elif fmt == "jsonl" and conform:
        sample_path = parent / "sample.jsonl"
        with open(sample_path, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
    else:
        sample_path = parent / "sample.json"
        sample_path.write_text(json.dumps(sample, indent=2), encoding="utf-8")

    # combined_path: the audited text, for actual_convert_cost's Comprehend step.
    combined_path = str(parent / "extract_combined.txt")
    Path(combined_path).write_text(combined_text or json.dumps(records, default=str),
                                   encoding="utf-8")

    emit("extraction_complete", records=len(records), pages_processed=pages_processed)
    return {
        "input": {"path": cfg.pdf_url, "mode": mode,
                  "schema": cfg.schema.get("name") if conform else None},
        "output": {
            "dir": str(parent),
            "sample_path": str(sample_path),
            "format": fmt,
            "records_extracted": len(records),
            "pages_processed": pages_processed,
            "pages_attempted": pages_attempted,
            "wall_time_seconds": wall,
            "combined_path": combined_path,
            "bedrock_usage": {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            },
        },
        "pii_audit": {
            "enabled": cfg.pii_audit,
            "path": pii_path,
            "entities_found": entities_found,
            "pii_fields": pii_field_names,
        },
        "page_failures": page_failures,
    }
