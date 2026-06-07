# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""FastAPI + HTMX demo UI over the structured-data pipeline (ADR-0009).

A thin layer: every endpoint calls the same core functions and reads the same
artifacts as the CLI. Three seed sources — preset (free), prompt (paid), upload
(paid) — feed `schema`, then `generate` produces a 10-row preview and the full
download. Paid Bedrock/Comprehend clients are FastAPI dependencies so tests and
the real app swap them.

Run: `pocsynth ui`  (or `uvicorn pocsynth.ui.app:app`).
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

import fitz
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse

from pocsynth import presets as presets_mod
from pocsynth.comprehend import scan_for_pii
from pocsynth.generate import GenerateConfig, run_generation
from pocsynth.schema import _validate_schema_shape, field_names
from pocsynth.schemagen import SchemaConfig, run_schema

# In-memory schema store keyed by a session id (demo-grade; not for production).
_SCHEMA_STORE: dict[str, dict[str, Any]] = {}

# Guardrails (demo UI takes untrusted HTTP input, unlike the trusted CLI).
MAX_DOWNLOAD_ROWS = 1_000_000   # ceiling on /download row count
MAX_UPLOAD_BYTES = 25 * 1024 * 1024   # 25 MB cap on uploaded seed PDFs
MAX_SCHEMA_STORE = 256          # bound the session schema cache (FIFO eviction)


# --------------------------------------------------------------------------- #
# Injected clients (overridden in tests; built from the AWS session otherwise)
#
# These return the client DIRECTLY so tests can override them with a stub via
# `app.dependency_overrides[get_bedrock_client] = lambda: stub`. To keep the
# free preset path from ever touching AWS, the handler resolves them lazily —
# see `_lazy` below — so a missing AWS region/credentials only errors on the
# paid branches that actually need a client.
# --------------------------------------------------------------------------- #
def get_bedrock_client():
    from pocsynth.bedrock import make_session
    return make_session().client("bedrock-runtime")


def get_comprehend_client():
    from pocsynth.bedrock import make_session
    return make_session().client("comprehend")


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _render_preview(schema: dict, rows: list[dict], *, cost: float | None,
                    pii_note: str | None) -> str:
    cols = field_names(schema)
    head = "".join(f"<th>{_html_escape(c)}</th>" for c in cols)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(
            f"<td>{_html_escape(str(row.get(c, '')))}</td>" for c in cols
        ) + "</tr>"
    cost_line = (
        f'<p class="cost">Estimated cost for this preview: '
        f'<strong>${cost:.4f}</strong></p>' if cost is not None
        else '<p class="cost">cost: <strong>$0.00</strong> (offline)</p>'
    )
    pii_line = f'<p class="pii">{_html_escape(pii_note)}</p>' if pii_note else ""
    return (
        '<div id="preview">'
        f"<h3>Generated data — sample of {len(rows)} rows</h3>"
        f"{cost_line}{pii_line}"
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        '<form hx-post="/download" hx-swap="none">'
        '<input type="hidden" name="rows" value="1000">'
        '<button type="submit" name="format" value="csv">Download CSV (1000 rows)</button> '
        '<button type="submit" name="format" value="json">Download JSON</button>'
        "</form></div>"
    )


def create_app() -> FastAPI:
    app = FastAPI(title="pocsynth — Open Source Synthetic Data Generator")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX_HTML

    @app.get("/presets", response_class=HTMLResponse)
    def list_presets_endpoint() -> str:
        opts = '<option value="">— none (use prompt or upload below) —</option>'
        opts += "".join(
            f'<option value="{_html_escape(p["name"])}">{_html_escape(p["description"])}</option>'
            for p in presets_mod.list_presets()
        )
        return f'<select name="preset">{opts}</select>'

    @app.post("/preview", response_class=HTMLResponse)
    def preview(
        request: Request,
        rows: int = Form(10),
        preset: str | None = Form(None),
        prompt: str | None = Form(None),
        seed_document: UploadFile | None = None,
    ) -> HTMLResponse:
        cost: float | None = None
        pii_note: str | None = None

        # Lazy client resolution: only the paid branches build/inject a client,
        # so the free preset path never touches AWS. Honors test overrides.
        def _client(dep):
            override = request.app.dependency_overrides.get(dep)
            return (override or dep)()

        # An empty string from the form's default <option> counts as "unset".
        preset = preset or None
        prompt = (prompt or "").strip() or None

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            if seed_document is not None and seed_document.filename:
                bedrock = _client(get_bedrock_client)
                comprehend = _client(get_comprehend_client)
                # Read PDF text locally, audit for PII, infer a schema from a sample.
                pdf_bytes = seed_document.file.read(MAX_UPLOAD_BYTES + 1)
                if len(pdf_bytes) > MAX_UPLOAD_BYTES:
                    return HTMLResponse(
                        '<div id="preview"><p>Upload too large (max '
                        f'{MAX_UPLOAD_BYTES // (1024 * 1024)} MB).</p></div>',
                        status_code=413,
                    )
                text = _pdf_text(pdf_bytes)
                detected = scan_for_pii(
                    text, folder_name=str(tdp / "pii-audit"),
                    filename="upload", comprehend=comprehend,
                )
                pii_fields = _pii_field_names(text, comprehend)
                pii_note = (
                    f"PII audit: {len(detected)} entities found; "
                    f"{len(pii_fields)} field(s) flagged — real values are barred "
                    "from the synthetic output."
                )
                sample = {
                    "schema": 1, "source": seed_document.filename or "upload",
                    "fields": [
                        {"name": w, "type_hint": "string", "value_counts": {},
                         "pii": w in pii_fields}
                        for w in _candidate_fields(text)
                    ],
                }
                sample_path = tdp / "sample.json"
                sample_path.write_text(json.dumps(sample))
                res = run_schema(SchemaConfig(sample_path=str(sample_path),
                                              output_dir=str(tdp), bedrock_client=bedrock))
                schema = json.loads(Path(res["output"]["schema_path"]).read_text())
                cost = _rough_cost(res["output"].get("bedrock_usage", {}))
            elif prompt:
                bedrock = _client(get_bedrock_client)
                res = run_schema(SchemaConfig(prompt=prompt, output_dir=str(tdp),
                                              bedrock_client=bedrock))
                schema = json.loads(Path(res["output"]["schema_path"]).read_text())
                cost = _rough_cost(res["output"].get("bedrock_usage", {}))
            elif preset:
                schema = presets_mod.load_preset(preset)
            else:
                return HTMLResponse(
                    '<div id="preview"><p>No data — choose a preset, describe a '
                    'business, or upload a document.</p></div>'
                )

            _validate_schema_shape(schema)
            gen = run_generation(GenerateConfig(schema=schema, rows=min(rows, 10),
                                                seed=42, export_format="json",
                                                output_dir=str(tdp)))
            preview_rows = json.loads(Path(gen["output"]["rows_path"]).read_text())

        # Stash the schema for the download step (no second model call).
        sid = request.cookies.get("sid") or uuid.uuid4().hex
        # Bound the cache: FIFO-evict the oldest entry when over the cap.
        if sid not in _SCHEMA_STORE and len(_SCHEMA_STORE) >= MAX_SCHEMA_STORE:
            _SCHEMA_STORE.pop(next(iter(_SCHEMA_STORE)), None)
        _SCHEMA_STORE[sid] = schema
        resp = HTMLResponse(_render_preview(schema, preview_rows, cost=cost, pii_note=pii_note))
        resp.set_cookie("sid", sid, httponly=True, samesite="strict")
        return resp

    @app.post("/download")
    def download(
        request: Request,
        rows: int = Form(1000),
        format: str = Form("csv"),
        seed: int = Form(42),
    ):
        sid = request.cookies.get("sid")
        schema = _SCHEMA_STORE.get(sid) if sid else None
        if schema is None:
            return PlainTextResponse("No previewed schema; run a preview first.",
                                     status_code=400)
        # Clamp to a sane ceiling so a stray/large value can't exhaust memory.
        rows = max(0, min(rows, MAX_DOWNLOAD_ROWS))
        fmt = format if format in ("csv", "json") else "csv"
        with tempfile.TemporaryDirectory() as td:
            gen = run_generation(GenerateConfig(schema=schema, rows=rows, seed=seed,
                                                export_format=fmt, output_dir=td))
            body = Path(gen["output"]["rows_path"]).read_text(encoding="utf-8")
        media = "text/csv" if fmt == "csv" else "application/json"
        fname = "synthetic_data." + fmt
        return PlainTextResponse(body, media_type=media, headers={
            "content-disposition": f'attachment; filename="{fname}"'})

    return app


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _pdf_text(pdf_bytes: bytes) -> str:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "\n".join(page.get_text("text") for page in doc)


def _candidate_fields(text: str) -> list[str]:
    """Heuristic: 'Label: value' lines become candidate field names. Falls back
    to a generic set so the demo always produces a schema."""
    import re
    fields: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z][A-Za-z _]{1,40}):", line)
        if m:
            name = m.group(1).strip().lower().replace(" ", "_")
            if name not in fields:
                fields.append(name)
    return fields or ["full_name", "email", "amount", "status"]


def _pii_field_names(text: str, comprehend) -> set[str]:
    """Flag candidate fields whose label/value line Comprehend marks as PII."""
    import re
    flagged: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z][A-Za-z _]{1,40}):\s*(.+)", line)
        if not m:
            continue
        name = m.group(1).strip().lower().replace(" ", "_")
        try:
            resp = comprehend.detect_pii_entities(Text=m.group(2)[:4000], LanguageCode="en")
        except Exception:  # noqa: BLE001
            continue
        if resp.get("Entities"):
            flagged.add(name)
    return flagged


def _rough_cost(usage: dict) -> float | None:
    """Best-effort cost for the preview banner; falls back to None on any error."""
    try:
        from pocsynth.pricing import estimate_bedrock_cost, load_pricing
        pricing = load_pricing()
        c = estimate_bedrock_cost("sonnet", int(usage.get("input_tokens", 0)),
                                  int(usage.get("output_tokens", 0)), pricing)
        return c["total_cost_usd"]
    except Exception:  # noqa: BLE001
        return None


app = create_app()


_INDEX_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>pocsynth — Synthetic Data Generator</title>
<script src="https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js"
 integrity="sha384-0895/pl2MU10Hqc6jd4RvrthNlDiE9U1tWmX7WRESftEDRosgxNsQG/Ze9YMRzHq"
 crossorigin="anonymous"></script>
<style>
 body{font-family:system-ui,sans-serif;max-width:880px;margin:2rem auto;padding:0 1rem;color:#1a1a2e}
 h1{font-size:1.8rem}
 .sentence{font-size:1.25rem;line-height:2.2;background:#f6f7fb;padding:1.5rem;border-radius:12px}
 select,input[type=number],input[type=text]{font-size:1rem;padding:.2rem .4rem;border-radius:8px;
   border:1px solid #cdd;background:#fff}
 input[name=prompt]{width:100%;margin-top:.6rem}
 button{background:#3b82f6;color:#fff;border:0;border-radius:8px;padding:.5rem 1rem;cursor:pointer}
 table{border-collapse:collapse;margin-top:1rem;width:100%}
 th,td{border:1px solid #e3e3ef;padding:.3rem .5rem;font-size:.85rem;text-align:left}
 .cost{color:#475569}.pii{color:#b45309;font-weight:600}
 fieldset{border:1px solid #e3e3ef;border-radius:10px;margin-top:1rem}
</style></head><body>
<h1>Open Source Synthetic Data Generator</h1>
<form hx-post="/preview" hx-target="#preview" hx-encoding="multipart/form-data">
 <div class="sentence">
  I want to generate a
  <input type="number" name="rows" value="10" min="1" max="10" style="width:4rem"> row dataset
  preview, seeded by &hellip;
  <fieldset><legend>1 — pick a preset (free)</legend>
   <span hx-get="/presets" hx-trigger="load" hx-swap="innerHTML">
     <select name="preset"><option value="b2b_saas">B2B SaaS</option></select>
   </span>
  </fieldset>
  <fieldset><legend>2 — or describe a business (uses Bedrock)</legend>
   <input type="text" name="prompt" placeholder="e.g. a B2B SaaS company's customer accounts">
  </fieldset>
  <fieldset><legend>3 — or upload a seed document (uses Bedrock; PII audited)</legend>
   <input type="file" name="seed_document" accept="application/pdf">
  </fieldset>
  <p><button type="submit">Preview &uarr;</button>
   <small>Preview is 10 rows. Downloads generate any size, free.</small></p>
 </div>
</form>
<div id="preview"><p>No data yet — make a choice above and click Preview.</p></div>
</body></html>
"""
