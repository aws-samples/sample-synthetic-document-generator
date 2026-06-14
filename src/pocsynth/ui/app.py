# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""FastAPI + HTMX web app over the structured-data pipeline (ADR-0009).

A real data-generation utility (not a demo): compose a dataset by tuning pills —
business domain, schema shape, time range, growth/variation/granularity — or
describe a custom dataset in your own words. The pills compose a precise prompt
that Bedrock turns into a schema; `generate` then produces the rows. Preview is
capped at a sample, but downloads stream the FULL requested row count (no cap).

Every endpoint calls the same core functions and reads the same artifacts as the
CLI. Bedrock/Comprehend clients are FastAPI dependencies so tests can swap them.

Run: `pocsynth ui`  (or `uvicorn pocsynth.ui.app:app`).
"""

from __future__ import annotations

import html
import json
import shlex
import tempfile
import uuid
from pathlib import Path
from typing import Any

import fitz
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse

from pocsynth import __version__
from pocsynth.comprehend import scan_for_pii
from pocsynth.errors import SchemaError
from pocsynth.generate import stream_rows
from pocsynth.schema import field_names
from pocsynth.schemagen import SchemaConfig, run_schema
from pocsynth.verify import verify_values

# In-memory schema store keyed by a session id.
_SCHEMA_STORE: dict[str, dict[str, Any]] = {}
# Per-session safety verdict + attestation (F4 / ADR-0010, ADR-0011). The
# download endpoint is fail-closed: a `fail` verdict here blocks the download.
_ATTESTATION_STORE: dict[str, dict[str, Any]] = {}

PREVIEW_ROWS = 10               # rows shown in the preview pane
MAX_UPLOAD_BYTES = 25 * 1024 * 1024   # 25 MB cap on uploaded seed PDFs
MAX_SCHEMA_STORE = 512          # bound the session schema cache (FIFO eviction)
MIN_PII_VALUE_LEN = 4           # ignore very short PII values (false-positive guard)
# Downloads stream row-by-row (constant memory), so the only ceiling is a
# sanity backstop against a typo'd 10-billion request, not a product limit.
MAX_DOWNLOAD_ROWS = 100_000_000

# Pill vocabularies — drive the composed prompt. The RECORD TYPE carries the
# domain (one row per ticket/claim/review/…); the SCENARIO tunes the prompt for
# the downstream workload. Aligned to the real SA/customer use cases seen across
# the engagement SIMs (RAG eval, agent building, document extraction, …).
# Each maps the short pill value -> the rich guidance injected into the NL prompt.
RECORD_TYPES = {
    "support tickets": "customer support tickets, one row per ticket — subject, a free-text body describing the problem, category, priority, channel, status, assigned queue, customer tier, and resolution time",
    "insurance claims": "insurance claims intake, one row per claim — claim id, policy number, line of business, a free-text loss description, incident state, amount claimed, intake channel, and status",
    "commercial leases": "commercial real-estate lease abstracts, one row per lease — lease id, tenant, property type, square footage, base rent, term start/end, renewal options, and a free-text clause summary",
    "product reviews": "product reviews, one row per review — product, rating, a free-text review title and body, verified-purchase flag, helpful votes, and sentiment",
    "contact-center transcripts": "contact-center call transcripts, one row per call — call id, agent, queue, detected intent, a full free-text transcript, disposition, sentiment, talk time, and CSAT",
    "knowledge articles": "knowledge-base / documentation articles, one row per document — title, a free-text retrievable body, category, audience, status, and word count",
    "financial transactions": "financial / payment transactions, one row per transaction — transaction id, account, amount, currency, merchant category, channel, status, and fraud flag",
    "customer contacts": "CRM customer contact records, one row per contact — name, email, company, title, lead source, lifecycle stage, region, and engagement score",
    "orders": "e-commerce orders, one row per order — order id, SKU, category, quantity, amount, channel, fulfilment status, and order date",
    "telemetry events": "device / clickstream / security telemetry, one row per event — event id, actor, source, event type, geo, numeric measures, and outcome",
}

SCENARIOS = {
    "RAG eval corpus": "Optimize for retrieval-augmented-generation evaluation: include at least one substantial free-text body field (several sentences to a few paragraphs) that is meaningfully retrievable, plus a stable identifier and topical category fields to support chunking, grounding, and answer-citation checks.",
    "agent building": "Optimize for building and testing agents: include clear branchable status/disposition and category fields with realistic, well-distributed categorical values so deterministic routing, tool-selection, and state-transition logic can be exercised end to end.",
    "model benchmarking": "Optimize for model benchmarking: vary difficulty and length across rows — mix short/simple and long/ambiguous free-text and include edge-case and outlier records — so accuracy can be measured across a spread of input complexity.",
    "load testing": "Optimize for load and performance testing: favor realistic high-volume distributions — heavy-tailed amounts/durations, skewed categorical frequencies, and a small fraction of outliers — so throughput and percentile latencies reflect production traffic shapes.",
    "analytics / BI": "Optimize for analytics and BI: include clean dimensions, well-defined categorical attributes with believable value distributions, and the key numeric measures an analyst would group, filter, and aggregate on.",
    "demo / prototype": "Optimize for a clear, presentable demo: pick the columns a stakeholder would immediately recognize for this record type, with believable values and a few illustrative outliers.",
}

SCHEMA_SHAPES = {
    "one-big-table": "a single denormalized wide table (one big table / OBT)",
    "star-schema": "a star schema with a central fact table and dimension tables",
}
VARIATION = ["low", "medium", "high"]

# Time-series framing is OPTIONAL — most record sets are flat (one row per entity).
# PERIOD / GRANULARITY / TREND apply only when "a time series" is chosen.
TIME_SHAPE = {
    "a flat record set": "a flat record set (each row an independent record; no time-series framing)",
    "a time series": "a time series over a defined period",
}
PERIOD = ["last 30 days", "last 90 days", "last 12 months", "2024", "2025", "2026"]
GRANULARITY = ["hourly", "daily", "weekly", "monthly"]
TREND = ["steady", "spike", "decline", "seasonal", "hypergrowth"]

# A strong worked example for the custom / describe path — a SIM-representative
# support-ticket dataset for agent building (exercises free-text + branchable fields).
EXAMPLE_PROMPT = (
    "A SaaS support desk's ticket dataset for building and testing a triage agent: "
    "one row per ticket. Columns: ticket_id, opened_at, customer_tier "
    "(free/pro/enterprise), channel (email/chat/phone/web), category "
    "(billing/auth/integration/bug/how-to/outage), priority (low/medium/high/urgent), "
    "subject, body (a realistic multi-sentence customer description of the problem), "
    "status (new/triaged/in_progress/waiting_on_customer/resolved/closed), "
    "assigned_queue, first_response_minutes, resolution_minutes, reopened, and "
    "csat (1-5). Make categories and statuses well-distributed so routing logic can "
    "be exercised, vary body length and difficulty across tickets, and include a few "
    "long, ambiguous, multi-issue tickets as edge cases."
)


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
    # quote=True also escapes " and ', so this is safe in attribute contexts
    # (title="…", option value="…"), not just element text.
    return html.escape(str(s), quote=True)


_PREVIEW_CSS = """
<style>
 #preview .pv{background:var(--card); border:1px solid var(--line); border-radius:16px;
   padding:clamp(1.2rem,3vw,2rem); box-shadow:0 18px 40px -28px var(--shadow);
   animation:fade .3s ease;}
 #preview .pv-head{display:flex; align-items:baseline; justify-content:space-between;
   gap:1rem; flex-wrap:wrap; border-bottom:1px solid var(--line); padding-bottom:.9rem;}
 #preview h3{font-family:'Fraunces',serif; font-weight:600; font-size:1.45rem; margin:0;
   letter-spacing:-.01em;}
 #preview h3 span{color:var(--ink-soft); font-weight:400; font-style:italic;}
 #preview .badges{display:flex; gap:.5rem; flex-wrap:wrap;}
 #preview .badge{font-family:'JetBrains Mono',monospace; font-size:.7rem; letter-spacing:.04em;
   padding:.3rem .6rem; border-radius:7px; display:inline-flex; align-items:center; gap:.35rem;}
 #preview .badge.free{background:var(--teal-soft); color:var(--teal);}
 #preview .badge.pii{background:#fbf3d8; color:var(--gold);}
 #preview .tablewrap{overflow:auto; margin:1.1rem 0; border:1px solid var(--line);
   border-radius:12px; max-height:420px;}
 #preview table{border-collapse:collapse; width:100%; font-family:'JetBrains Mono',monospace;
   font-size:.78rem;}
 #preview thead th{position:sticky; top:0; background:var(--ink); color:var(--card);
   text-align:left; padding:.55rem .7rem; font-weight:500; letter-spacing:.03em; white-space:nowrap;}
 #preview tbody td{padding:.45rem .7rem; border-bottom:1px solid var(--line);
   color:var(--ink); white-space:nowrap;}
 #preview tbody tr:nth-child(odd){background:rgba(255,255,255,.5);}
 #preview tbody tr:hover{background:var(--teal-soft);}
 #preview .dl{display:flex; gap:.7rem; align-items:center; flex-wrap:wrap;}
 #preview .dl button{font-family:'Hanken Grotesk',sans-serif; font-weight:600; font-size:.9rem;
   border:1.5px solid var(--ink); background:#fff; color:var(--ink); border-radius:10px;
   padding:.6rem 1.1rem; cursor:pointer; box-shadow:2px 2px 0 var(--ink);
   transition:transform .12s, box-shadow .12s;}
 #preview .dl button:hover{transform:translate(-1px,-1px); box-shadow:3px 3px 0 var(--teal);}
 #preview .dl small{color:var(--ink-soft); font-size:.8rem;}
 #preview .dlrows{font-family:'JetBrains Mono',monospace; font-size:.8rem; color:var(--ink-soft);
   display:inline-flex; align-items:center;}
 #preview .dlrows input{width:7rem; font:inherit; color:var(--ink); border:1px solid var(--line);
   border-radius:8px; padding:.5rem .6rem; background:#fff;}
 #preview .dlrows input:focus{outline:none; border-color:var(--teal);}
</style>"""


def _render_preview(schema: dict, rows: list[dict], *,
                    pii_note: str | None, full_rows: int, seed: int) -> str:
    cols = field_names(schema)
    head = "".join(f"<th>{_html_escape(c)}</th>" for c in cols)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(
            f"<td>{_html_escape(str(row.get(c, '')))}</td>" for c in cols
        ) + "</tr>"
    pii_badge = (
        f'<span class="badge pii" title="{_html_escape(pii_note)}">⚠ PII audited</span>'
        if pii_note else ""
    )
    schema_name = _html_escape(str(schema.get("name", "dataset")))
    return (
        _PREVIEW_CSS
        + '<div id="preview"><div class="pv">'
        + '<div class="pv-head">'
        + f'<h3>{schema_name} <span>· {len(rows)}-row sample</span></h3>'
        + f'<div class="badges">{pii_badge}'
        + f'<span class="badge free">{len(cols)} fields</span></div>'
        + "</div>"
        + f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead>'
        + f"<tbody>{body}</tbody></table></div>"
        + '<form class="dl" hx-post="/download" hx-swap="none">'
        + f'<input type="hidden" name="seed" value="{int(seed)}">'
        + '<label class="dlrows">rows&nbsp;'
        + f'<input type="number" name="rows" value="{int(full_rows)}" min="1" '
        + 'step="1"></label>'
        + '<button type="submit" name="format" value="csv">↓ CSV</button>'
        + '<button type="submit" name="format" value="json">↓ JSON</button>'
        + "<small>full dataset · streamed · runs locally · reuses this schema</small>"
        + "</form></div></div>"
    )


_SAFETY_CSS = """
<style>
 #safety .sp{margin-top:1.2rem; border-radius:14px; padding:1.1rem 1.3rem;
   border:1.5px solid var(--line); background:var(--card); animation:fade .3s ease;}
 #safety .sp.pass{border-color:var(--teal); background:var(--teal-soft);}
 #safety .sp.fail{border-color:var(--vermilion); background:var(--vermilion-soft);}
 #safety .sp-head{display:flex; align-items:center; gap:.6rem; flex-wrap:wrap;}
 #safety .verdict{font-family:'JetBrains Mono',monospace; font-weight:600; font-size:.85rem;
   letter-spacing:.05em; padding:.3rem .7rem; border-radius:8px; display:inline-flex; gap:.4rem;}
 #safety .verdict.pass{background:var(--teal); color:#fff;}
 #safety .verdict.fail{background:var(--vermilion); color:#fff;}
 #safety .verdict.na{background:var(--ink-soft); color:#fff;}
 #safety h4{font-family:'Fraunces',serif; font-weight:600; font-size:1.1rem; margin:0;}
 #safety .facts{font-family:'JetBrains Mono',monospace; font-size:.78rem; color:var(--ink-soft);
   margin:.7rem 0 0; line-height:1.9;}
 #safety .facts b{color:var(--ink);}
 #safety .leaks{font-family:'JetBrains Mono',monospace; font-size:.76rem; color:var(--vermilion);
   margin:.5rem 0 0;}
 #safety .att{margin-top:.8rem;}
 #safety .att a{font-family:'JetBrains Mono',monospace; font-size:.76rem; color:var(--teal);
   text-decoration:underline; cursor:pointer;}
 #safety .blocked{font-family:'Hanken Grotesk',sans-serif; font-weight:600; color:var(--vermilion);
   margin-top:.6rem;}
 #safety .disclaimer{font-size:.74rem; line-height:1.5; color:var(--ink-soft); margin-top:.8rem;
   padding-top:.7rem; border-top:1px dashed var(--line);}
</style>"""


def _render_safety_panel(att: dict, *, pii_entities: int, suppressed_fields: list[str]) -> str:
    """The safety / attestation panel (F4). Reuses the preview-badge palette.

    Shows: PII entities found, fields suppressed by the guard, the verify verdict
    (✓ PASSED / ✗ FAILED + leaked fields), and a Download attestation link. On a
    failed verdict it states plainly the output is NOT cleared for sharing.
    """
    verdict = att["verdict"]
    leaks = att.get("leaks", [])
    if verdict == "fail":
        cls, badge, badge_cls = "fail", "✗ LEAK DETECTED", "fail"
        headline = "Not cleared for sharing"
    elif verdict == "pass":
        cls, badge, badge_cls = "pass", "✓ NO LEAK DETECTED", "pass"
        headline = "No real value detected in the output"
    else:  # not_applicable
        cls, badge, badge_cls = "", "— N/A", "na"
        headline = "No real source to verify"

    suppressed = ", ".join(_html_escape(f) for f in suppressed_fields) or "none"
    facts = (
        f'<div class="facts">'
        f'<div>PII entities found in source: <b>{int(pii_entities)}</b></div>'
        f'<div>fields suppressed by the PII guard: <b>{suppressed}</b></div>'
        f'<div>real values checked against output: <b>{att.get("candidate_pii_values", 0)}</b> '
        f'(rows + schema)</div>'
        f"</div>"
    )
    leak_html = ""
    blocked_html = ""
    if verdict == "fail":
        def _leak_label(lk: dict) -> str:
            # "field: ****" when we know the source field, else just the preview.
            field = lk.get("field")
            prev = _html_escape(lk["value_preview"])
            return f'{_html_escape(field)}: {prev}' if field else prev
        previews = ", ".join(_leak_label(lk) for lk in leaks)
        where = sorted({w for lk in leaks for w in lk.get("where", [])})
        leak_html = (
            f'<div class="leaks">⚠ {len(leaks)} real value(s) leaked into '
            f'{_html_escape(", ".join(where))} — {previews}</div>'
        )
        blocked_html = (
            '<div class="blocked">Download blocked — regenerate or fix the schema '
            "before sharing.</div>"
        )
    att_link = (
        '<div class="att"><a hx-get="/attestation" hx-target="#att-sink" '
        'hx-swap="none">↓ Download attestation (JSON)</a>'
        '<span id="att-sink"></span></div>'
    )
    # Best-effort disclaimer, shown on every verdict (pass/fail/na) so the panel
    # never implies a guarantee.
    disclaimer = (
        '<div class="disclaimer">Detection is best-effort — Amazon Comprehend PII '
        "detection plus an exact-value scan of the rows and schema — and may miss "
        "values (e.g. reformatted, partial, or unflagged PII). Review the output "
        "yourself before sharing.</div>"
    )
    return (
        _SAFETY_CSS
        + '<div id="safety"><div class="sp ' + cls + '">'
        + '<div class="sp-head">'
        + f'<span class="verdict {badge_cls}">{badge}</span>'
        + f"<h4>{_html_escape(headline)}</h4></div>"
        + facts + leak_html
        + att_link
        + blocked_html
        + disclaimer
        + "</div></div>"
    )


_COMMANDS_CSS = """
<style>
 #commands{margin-top:1.6rem;}
 #commands .ch{font-family:'Fraunces',serif; font-weight:600; font-size:1.15rem; color:var(--ink);
   margin:0 0 .25rem;}
 #commands .csub{font-size:.85rem; color:var(--ink-soft); margin:0 0 1rem; max-width:60ch;}
 #commands .cmd{margin-bottom:1rem;}
 #commands .cmd-head{display:flex; align-items:center; gap:.6rem; margin-bottom:.35rem;}
 #commands .cmd-label{font-family:'JetBrains Mono',monospace; font-size:.72rem; letter-spacing:.06em;
   text-transform:uppercase; color:var(--ink-soft);}
 #commands .copy{margin-left:auto; font-family:'JetBrains Mono',monospace; font-size:.68rem;
   letter-spacing:.04em; border:1px solid var(--line); background:#fff; color:var(--ink-soft);
   border-radius:7px; padding:.25rem .6rem; cursor:pointer; transition:all .12s ease;}
 #commands .copy:hover{border-color:var(--teal); color:var(--teal);}
 #commands .copy.copied{border-color:var(--teal); color:var(--teal); background:var(--teal-soft);}
 #commands pre{margin:0; background:var(--ink); color:#f4efe6; border-radius:12px;
   padding:.9rem 1.05rem; overflow-x:auto; white-space:pre-wrap; word-break:break-word;
   font-family:'JetBrains Mono',monospace; font-size:.78rem; line-height:1.55;}
 #commands .cnote{font-size:.74rem; line-height:1.5; color:var(--ink-soft); margin-top:.9rem;
   padding-top:.7rem; border-top:1px dashed var(--line);}
 #commands .cnote code{background:var(--surface2); padding:.05rem .3rem; border-radius:4px;}
</style>"""


def _build_run_command(mode: str, *, prompt: str | None, document_name: str | None,
                       rows: int, seed: int) -> str:
    """The body of the one-shot `run` command (no leading binary) that reproduces
    what the pills composed. `mode` is pills | custom | document.

    Honest teaching artifact, not a byte-faithful replay (the document path runs
    a fuller Bedrock extraction on the CLI than the in-browser preview)."""
    args = ["run"]
    if mode == "document":
        # The browser has no local filesystem path; use an explicit placeholder.
        # Shell-quote the uploaded filename — a name with spaces or shell
        # metacharacters (e.g. "x'; rm -rf ~ #.pdf") must not produce a broken or
        # injectable copy-pasteable command. The placeholder needs no quoting.
        doc = shlex.quote(document_name) if document_name else "<your-file.pdf>"
        args += ["--document", doc]
    else:
        args += ["--prompt", shlex.quote(prompt or "")]
    args += ["--rows", str(int(rows)), "--seed", str(int(seed)), "-o", "./out", "--yes"]
    return " ".join(args)


def _build_skill_request(mode: str, *, prompt: str | None, document_name: str | None,
                         rows: int, seed: int) -> str:
    """A NATURAL-LANGUAGE `/pocsynth` request — how you actually invoke the skill
    in Kiro or Claude Code (the skill then composes + runs the CLI for you). NOT
    the underlying ./pocsynth.py command."""
    if mode == "document":
        what = f"a synthetic dataset shaped like {document_name or 'my document'}"
    elif mode == "custom":
        # The user's own description, collapsed to a single readable line.
        desc = " ".join((prompt or "").split()).rstrip(".")
        what = f"a synthetic dataset of {desc}" if desc else "a synthetic dataset"
    else:  # pills — short, friendly phrasing rather than the long composed prompt
        what = "a synthetic dataset for this record type and scenario"
    return (f"/pocsynth generate {what} — {int(rows)} rows, "
            f"seed {int(seed)}, written to ./out")


def _render_command_panel(mode: str, *, prompt: str | None = None,
                          document_name: str | None = None, rows: int, seed: int) -> str:
    """Show how to reproduce this dataset two ways: the exact CLI command, and a
    natural-language /pocsynth skill request (Kiro or Claude Code). A teaching
    artifact for SAs and customers (CONTEXT: Command equivalent)."""
    cli = "pocsynth " + _build_run_command(
        mode, prompt=prompt, document_name=document_name, rows=rows, seed=seed)
    skill = _build_skill_request(
        mode, prompt=prompt, document_name=document_name, rows=rows, seed=seed)

    skill_note = (
        "The <code>/pocsynth</code> skill (Kiro or Claude Code) takes a plain-language "
        "request and composes + runs the CLI for you — no flags to remember."
    )
    if mode == "document":
        note = (
            'On the CLI, <code>run --document</code> performs a full Bedrock '
            "extraction (richer than the in-browser preview), then schema → "
            "generate → verify. Point it at the file's local path. Add "
            f"<code>--format json</code> for JSON. {skill_note}"
        )
    else:
        note = (
            "Reproduces this dataset from the same prompt. Add "
            f"<code>--format json</code> for JSON output. {skill_note}"
        )

    def _block(label: str, cmd: str) -> str:
        return (
            '<div class="cmd"><div class="cmd-head">'
            f'<span class="cmd-label">{_html_escape(label)}</span>'
            '<button type="button" class="copy" onclick="copyCmd(this)">copy</button>'
            "</div>"
            f'<pre>{_html_escape(cmd)}</pre></div>'
        )

    return (
        _COMMANDS_CSS
        + '<div id="commands">'
        + '<h3 class="ch">Reproduce this outside the browser</h3>'
        + '<p class="csub">The exact CLI command, or a plain-language request to '
        + "the /pocsynth agent skill — for a demo, a script, or an agent.</p>"
        + _block("CLI", cli)
        + _block("Agent skill · /pocsynth (Kiro or Claude Code)", skill)
        + f'<div class="cnote">{note}</div>'
        + "</div>"
    )


def _real_pii_from_scan(detected: list[dict], min_len: int = MIN_PII_VALUE_LEN) -> set[str]:
    """Distinct real PII values Comprehend flagged in the source, long enough to
    scan for without false positives."""
    values: set[str] = set()
    for ent in detected:
        v = str(ent.get("Value", "")).strip()
        if len(v) >= min_len:
            values.add(v)
    return values


def create_app() -> FastAPI:
    app = FastAPI(title="pocsynth — Open Source Synthetic Data Generator")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _render_index()

    @app.post("/preview", response_class=HTMLResponse)
    def preview(
        request: Request,
        rows: int = Form(100),
        record_type: str = Form("support tickets"),
        scenario: str = Form("agent building"),
        shape: str = Form("one-big-table"),
        variation: str | None = Form("medium"),
        time_shape: str = Form("a flat record set"),
        period: str | None = Form(None),
        granularity: str | None = Form(None),
        trend: str | None = Form(None),
        prompt: str | None = Form(None),
        seed: int = Form(42),
        seed_mode: str = Form("pills"),
        seed_document: UploadFile | None = None,
    ) -> HTMLResponse:
        pii_note: str | None = None
        rows = max(1, rows)

        def _client(dep):
            override = request.app.dependency_overrides.get(dep)
            return (override or dep)()

        prompt = (prompt or "").strip() or None
        # Route on the active seed tab, not on whichever field is non-empty: a
        # stale value in a hidden pane (e.g. a pre-filled custom textarea) must
        # never override the user's choice. The browser sets seed_mode from the
        # active tab; it defaults to "pills".
        if seed_mode == "pills":
            prompt = None  # the pills compose the prompt; ignore any hidden text
        elif seed_mode == "custom":
            seed_document = None
        # Set on the document path: real PII values + suppressed fields + entity
        # count, used to build the F4 safety panel after generation.
        real_pii_values: set[str] = set()
        value_fields: dict[str, str] = {}   # real value → source field name
        suppressed_fields: list[str] = []
        pii_entities = 0
        seeded_from_document = False
        # Captured for the "Command equivalent" panel (the composed/custom prompt
        # or the uploaded document name) so we can show the reproducing command.
        command_prompt: str | None = None
        document_name: str | None = None

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            if seed_document is not None and seed_document.filename:
                seeded_from_document = True
                document_name = seed_document.filename
                bedrock = _client(get_bedrock_client)
                comprehend = _client(get_comprehend_client)
                pdf_bytes = seed_document.file.read(MAX_UPLOAD_BYTES + 1)
                if len(pdf_bytes) > MAX_UPLOAD_BYTES:
                    return HTMLResponse(
                        '<div id="preview"><p class="placeholder">Upload too large (max '
                        f'{MAX_UPLOAD_BYTES // (1024 * 1024)} MB).</p></div>',
                        status_code=413,
                    )
                text = _pdf_text(pdf_bytes)
                # ONE Comprehend pass over the whole document; field-level PII
                # flags are derived from it (no second per-field scan).
                detected = scan_for_pii(text, folder_name=str(tdp / "pii-audit"),
                                        filename="upload", comprehend=comprehend)
                field_values = _parse_field_values(text)
                pii_fields = _pii_field_names(field_values, detected)
                real_pii_values = _real_pii_from_scan(detected)
                pii_entities = len(detected)
                # Map each real value to the field it came from, so a leak can
                # name the offending field in the safety panel.
                for fname, vals in field_values.items():
                    for v in vals:
                        if len(v) >= MIN_PII_VALUE_LEN:
                            value_fields.setdefault(v, fname)
                pii_note = (
                    f"PII audit: {len(detected)} entities found; "
                    f"{len(pii_fields)} field(s) flagged — real values are audited "
                    "and the generated output is scanned for leaks (best-effort)."
                )
                # Carry the real per-field values into value_counts so the PII
                # guard (ADR-0005) can strip them at schema-design time — instead
                # of relying solely on the downstream verify backstop. Reuse the
                # already-parsed field names (generic fallback when none parsed).
                field_names = list(field_values) or _GENERIC_FIELDS
                sample = {
                    "schema": 1, "source": seed_document.filename or "upload",
                    "fields": [
                        {"name": w, "type_hint": "string",
                         "value_counts": _value_counts(field_values.get(w, [])),
                         "pii": w in pii_fields}
                        for w in field_names
                    ],
                }
                sample_path = tdp / "sample.json"
                sample_path.write_text(json.dumps(sample))
                res = run_schema(SchemaConfig(sample_path=str(sample_path),
                                              output_dir=str(tdp), bedrock_client=bedrock))
                schema = json.loads(Path(res["output"]["schema_path"]).read_text())
                # Fields the PII guard suppressed are noted in the lint report.
                suppressed_fields = [
                    n.get("field") for n in res.get("lint", {}).get("notes", [])
                    if n.get("issue") == "pii_enum_suppressed" and n.get("field")
                ]
            else:
                # Pills OR a free-text prompt → compose a precise NL prompt and
                # infer the schema from it (the same Bedrock path).
                effective = prompt or _compose_prompt(
                    record_type, scenario, shape, variation,
                    time_shape, period, granularity, trend,
                )
                command_prompt = effective
                bedrock = _client(get_bedrock_client)
                res = run_schema(SchemaConfig(prompt=effective, output_dir=str(tdp),
                                              bedrock_client=bedrock))
                schema = json.loads(Path(res["output"]["schema_path"]).read_text())

            # Render the sample straight from the streaming generator (which
            # validates the schema as its first step) — no temp-file round trip.
            preview_rows = json.loads(
                "".join(stream_rows(schema, PREVIEW_ROWS, export_format="json", seed=seed))
            )

        # Stash the schema + the requested full count for the download step.
        sid = request.cookies.get("sid") or uuid.uuid4().hex
        if sid not in _SCHEMA_STORE and len(_SCHEMA_STORE) >= MAX_SCHEMA_STORE:
            _SCHEMA_STORE.pop(next(iter(_SCHEMA_STORE)), None)
        _SCHEMA_STORE[sid] = schema

        # F4 safety panel: on the document path, verify the generated output (and
        # the shared Schema artifact) carries no real PII value (ADR-0010). The
        # preview rows are byte-identical to the start of the full download (same
        # seed), so a clean preview + a deterministic generator is a sound proxy.
        safety_html = ""
        if seeded_from_document:
            rows_text = "".join(
                stream_rows(schema, PREVIEW_ROWS, export_format="csv", seed=seed)
            )
            verdict, leaks, schema_scanned = verify_values(
                real_pii_values, rows_text, schema, value_fields)
            attestation = {
                "schema": 1, "verdict": verdict, "tool_version": __version__,
                "source": (seed_document.filename or "upload") if seed_document else "upload",
                "candidate_pii_values": len(real_pii_values),
                "leaks": leaks,
                "scanned": {"rows": True, "schema": schema_scanned},
                "suppressed_fields": suppressed_fields,
            }
            if sid not in _ATTESTATION_STORE and len(_ATTESTATION_STORE) >= MAX_SCHEMA_STORE:
                _ATTESTATION_STORE.pop(next(iter(_ATTESTATION_STORE)), None)
            _ATTESTATION_STORE[sid] = attestation
            safety_html = _render_safety_panel(
                attestation, pii_entities=pii_entities, suppressed_fields=suppressed_fields
            )
        else:
            # Synthetic seed (pills/prompt) → no real source; clear any stale verdict.
            _ATTESTATION_STORE.pop(sid, None)

        # Command-equivalent panel: the CLI + agent-skill commands that reproduce
        # this dataset (CONTEXT: Command equivalent). Derive the mode from the
        # active seed tab — the same signal the seed routing keyed on above — so a
        # document upload that also carried a stale prompt can't mislabel itself.
        cmd_mode = ("document" if seeded_from_document
                    else "custom" if seed_mode == "custom" else "pills")
        commands_html = _render_command_panel(
            cmd_mode, prompt=command_prompt, document_name=document_name,
            rows=rows, seed=seed,
        )

        resp = HTMLResponse(
            _render_preview(schema, preview_rows, pii_note=pii_note,
                            full_rows=rows, seed=seed)
            + safety_html
            + commands_html
        )
        resp.set_cookie("sid", sid, httponly=True, samesite="strict")
        return resp

    @app.post("/download")
    def download(
        request: Request,
        rows: int = Form(100),
        format: str = Form("csv"),
        seed: int = Form(42),
    ):
        sid = request.cookies.get("sid")
        schema = _SCHEMA_STORE.get(sid) if sid else None
        if schema is None:
            return PlainTextResponse("No schema yet; run a preview first.",
                                     status_code=400)
        # Fail-closed (ADR-0010/0011): if the safety verdict for this session is
        # `fail`, a real PII value leaked — refuse to serve the dataset as safe.
        att = _ATTESTATION_STORE.get(sid) if sid else None
        if att and att.get("verdict") == "fail":
            return PlainTextResponse(
                "Download blocked: verification FAILED — a real PII value leaked into "
                "the output. NOT cleared for sharing. Regenerate or fix the schema.",
                status_code=409,
            )
        rows = max(0, min(rows, MAX_DOWNLOAD_ROWS))
        fmt = format if format in ("csv", "json") else "csv"
        media = "text/csv" if fmt == "csv" else "application/json"
        fname = _safe_filename(str(schema.get("name", "dataset")), fmt)

        # Drive the generator and pull the FIRST chunk eagerly, so any error
        # (invalid schema, bad faker provider, bad regex) surfaces as a clean
        # 4xx/5xx BEFORE the 200 + headers are committed — rather than aborting
        # mid-stream and leaving the client a truncated body under HTTP 200.
        gen = stream_rows(schema, rows, export_format=fmt, seed=seed)
        try:
            first = next(gen, "")
        except SchemaError as exc:
            return PlainTextResponse(f"Cannot generate: {exc.message}", status_code=400)

        def _body():
            yield first
            yield from gen

        return StreamingResponse(
            _body(),
            media_type=media,
            headers={"content-disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/attestation")
    def attestation(request: Request):
        """Download the session's safety Attestation (F4). Available only after a
        document-seeded preview; synthetic seeds have nothing to attest."""
        sid = request.cookies.get("sid")
        att = _ATTESTATION_STORE.get(sid) if sid else None
        if att is None:
            return PlainTextResponse(
                "No attestation: upload a document and preview first.", status_code=404)
        return StreamingResponse(
            iter([json.dumps(att, indent=2)]),
            media_type="application/json",
            headers={"content-disposition": 'attachment; filename="attestation.json"'},
        )

    return app


def _safe_filename(name: str, ext: str) -> str:
    """Slugify a (model-generated) schema name into a header-safe filename.

    The schema name flows from Bedrock and was being interpolated raw into the
    content-disposition header; a quote or CR/LF could corrupt or inject the
    header. Keep only alnum/dash/underscore/dot, collapse the rest.
    """
    import re as _re
    slug = _re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return f"{slug or 'dataset'}.{ext}"


def _compose_prompt(record_type, scenario, shape, variation,
                    time_shape="a flat record set",
                    period=None, granularity=None, trend=None) -> str:
    """Turn the pill selections into a precise natural-language schema request.

    The record type carries the domain (one row per ticket/claim/review/…); the
    scenario tunes the prompt for the downstream workload; time framing is
    OPTIONAL — a flat record set yields a record-per-entity prompt with no forced
    calendar, while "a time series" adds period/granularity/trend.
    """
    record_desc = RECORD_TYPES.get(record_type, next(iter(RECORD_TYPES.values())))
    shape_desc = SCHEMA_SHAPES.get(shape, SCHEMA_SHAPES["one-big-table"])
    scenario_desc = SCENARIOS.get(scenario, "")

    parts = [f"A realistic dataset of {record_desc}, modeled as {shape_desc}."]

    # Optional time-series framing — only when explicitly chosen.
    if time_shape == "a time series":
        clause = "Model the rows as a time series"
        if period:
            clause += f" covering {period}"
        if granularity:
            clause += f" at {granularity} granularity"
        parts.append(clause + ".")
        if trend:
            parts.append(f"Exhibit a {trend} trend over the period.")

    if variation:
        parts.append(f"Use {variation} variation/noise across records.")

    if scenario_desc:
        parts.append(scenario_desc)

    parts.append(
        "Choose the columns a practitioner would expect for this record type — "
        "identifiers, dimensions, categorical attributes with realistic value "
        "distributions, any free-text fields the record naturally carries, dates, "
        "and the key numeric measures — with believable relationships and a few outliers."
    )
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _pdf_text(pdf_bytes: bytes) -> str:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "\n".join(page.get_text("text") for page in doc)


# Generic fallback fields when a document has no parseable `Label: value` lines,
# so the demo always produces a schema.
_GENERIC_FIELDS = ["full_name", "email", "amount", "status"]


def _parse_field_values(text: str) -> dict[str, list[str]]:
    """Group a document's `Label: value` lines into {field_name: [values…]}.

    The single source for both PII flagging and the sample's value_counts, so
    the schema-inference sample carries the real values the PII guard needs to
    suppress (matching the CLI extract path)."""
    import re

    by_field: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z][A-Za-z _]{1,40}):\s*(.+)", line)
        if not m:
            continue
        name = m.group(1).strip().lower().replace(" ", "_")
        by_field.setdefault(name, []).append(m.group(2).strip())
    return by_field


def _value_counts(values: list[str]) -> dict[str, int]:
    """{value: count} for a field's observed values — the shape schema inference
    and the PII guard expect."""
    counts: dict[str, int] = {}
    for v in values:
        v = str(v).strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    return counts


def _pii_field_names(field_values: dict[str, list[str]], detected: list[dict]) -> set[str]:
    """Flag fields whose values Comprehend marked as PII, derived from the SINGLE
    whole-document scan (`scan_for_pii`) — no second per-field Comprehend pass.

    The whole-doc scan already covers every field's text (with more surrounding
    context than per-field chunks), so a field is PII when one of its parsed
    values contains a detected entity value. Avoids N extra API calls per upload.
    """
    flagged: set[str] = set()
    pii_values = {
        str(ent.get("Value", "")).strip()
        for ent in detected
        if len(str(ent.get("Value", "")).strip()) >= MIN_PII_VALUE_LEN
    }
    if not pii_values:
        return flagged
    for name, values in field_values.items():
        blob = " ".join(values)
        if any(pv in blob for pv in pii_values):
            flagged.add(name)
    return flagged


def _opts(values, *, default=None, labels=None) -> str:
    """Build <option> tags; mark `default` selected."""
    out = []
    for v in values:
        label = (labels or {}).get(v, v)
        sel = " selected" if v == default else ""
        out.append(f'<option value="{_html_escape(str(v))}"{sel}>{_html_escape(str(label))}</option>')
    return "".join(out)


def _render_index() -> str:
    """Fill the index template's pill option lists + the worked example."""
    return (
        _INDEX_HTML
        .replace("__RECORD__", _opts(list(RECORD_TYPES), default="support tickets"))
        .replace("__SCENARIO__", _opts(list(SCENARIOS), default="agent building"))
        .replace("__SHAPE__", _opts(
            list(SCHEMA_SHAPES), default="one-big-table",
            labels={"one-big-table": "One Big Table (OBT)",
                    "star-schema": "Star Schema (multi-table)"}))
        .replace("__VARIATION__", _opts(VARIATION, default="medium"))
        .replace("__TIMESHAPE__", _opts(list(TIME_SHAPE), default="a flat record set"))
        .replace("__PERIOD__", _opts(PERIOD, default="last 12 months"))
        .replace("__GRAN__", _opts(GRANULARITY, default="daily"))
        .replace("__TREND__", _opts(TREND, default="steady"))
        .replace("__PREVIEWN__", str(PREVIEW_ROWS))
        # The worked example is opt-in via the "load the worked example" button
        # (it must NOT prefill the textarea, or it overrides the pills).
        .replace("__EXAMPLE_JSON__", json.dumps(EXAMPLE_PROMPT))
    )


app = create_app()


_INDEX_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pocsynth · synthetic data</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400..900;1,9..144,400..600&family=Hanken+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js"
 integrity="sha384-0895/pl2MU10Hqc6jd4RvrthNlDiE9U1tWmX7WRESftEDRosgxNsQG/Ze9YMRzHq"
 crossorigin="anonymous"></script>
<style>
 :root{
   --ink:#1c1a17; --ink-soft:#5c554b; --paper:#f4efe6; --card:#fbf8f2;
   --line:#ddd4c4; --teal:#1f6f63; --teal-soft:#e3efec; --vermilion:#c8451f;
   --vermilion-soft:#f6e3da; --gold:#b3902f; --shadow:rgba(28,26,23,.10);
 }
 *{box-sizing:border-box}
 html{-webkit-font-smoothing:antialiased}
 body{
   margin:0; color:var(--ink); background:var(--paper);
   font-family:'Hanken Grotesk',system-ui,sans-serif; line-height:1.5;
   background-image:radial-gradient(var(--line) .5px,transparent .5px);
   background-size:22px 22px;
 }
 .wrap{max-width:1080px; margin:0 auto; padding:clamp(1.5rem,4vw,3.5rem) clamp(1rem,4vw,2rem) 4rem;}
 /* masthead */
 .mast{display:flex; align-items:baseline; justify-content:space-between;
   border-bottom:2px solid var(--ink); padding-bottom:.9rem; margin-bottom:.5rem;
   flex-wrap:wrap; gap:.5rem;}
 .mast h1{font-family:'Fraunces',serif; font-optical-sizing:auto; font-weight:600;
   font-size:clamp(2rem,5vw,3.3rem); letter-spacing:-.02em; margin:0; line-height:.95;}
 .mast h1 em{font-style:italic; color:var(--vermilion);}
 .kicker{font-family:'JetBrains Mono',monospace; font-size:.72rem; letter-spacing:.18em;
   text-transform:uppercase; color:var(--ink-soft);}
 .tagline{font-size:1.02rem; color:var(--ink-soft); margin:.8rem 0 2.2rem; max-width:54ch;}
 .tagline b{color:var(--teal); font-weight:600;}
 .layout{display:grid; grid-template-columns:1fr; gap:2rem;}
 @media(min-width:900px){.layout{grid-template-columns:1.6fr 1fr;}}
 /* the spec card */
 .card{background:var(--card); border:1px solid var(--line); border-radius:18px;
   padding:clamp(1.4rem,3vw,2.4rem); box-shadow:0 18px 40px -28px var(--shadow);
   position:relative; overflow:hidden;}
 .card::before{content:""; position:absolute; inset:0 0 auto 0; height:5px;
   background:linear-gradient(90deg,var(--teal) 0 60%,var(--vermilion) 60% 100%);}
 .sentence{font-family:'Fraunces',serif; font-weight:400; font-size:clamp(1.35rem,2.6vw,1.85rem);
   line-height:2.1; letter-spacing:-.01em; color:var(--ink); margin:.6rem 0 0;}
 /* pills */
 .pill{display:inline-flex; align-items:center; gap:.35em; vertical-align:baseline;
   font-family:'Hanken Grotesk',sans-serif; font-weight:600; font-size:.62em;
   background:#fff; border:1.5px solid var(--ink); border-radius:999px;
   padding:.18em .7em; margin:0 .1em; cursor:pointer; position:relative;
   box-shadow:2px 2px 0 var(--ink); transition:transform .12s ease, box-shadow .12s ease;}
 .pill:hover{transform:translate(-1px,-1px); box-shadow:3px 3px 0 var(--ink);}
 .pill:active{transform:translate(1px,1px); box-shadow:1px 1px 0 var(--ink);}
 .pill select,.pill input{appearance:none; border:0; background:transparent; outline:none;
   font:inherit; color:inherit; cursor:pointer; padding:0 .9em 0 0; margin:0;}
 .pill::after{content:"▾"; position:absolute; right:.55em; font-size:.7em; color:var(--ink-soft);
   pointer-events:none;}
 .pill.num::after{content:none;}
 .pill.num input{width:4.5em; text-align:center; padding:0;}
 /* hide the native number spinners so the value isn't crowded/clipped */
 .pill.num input::-webkit-outer-spin-button,
 .pill.num input::-webkit-inner-spin-button{-webkit-appearance:none; margin:0;}
 .pill.num input[type=number]{-moz-appearance:textfield;}
 .series-clause[hidden]{display:none;}
 .pill.teal{background:var(--teal-soft); border-color:var(--teal); box-shadow:2px 2px 0 var(--teal);}
 .pill.teal:hover{box-shadow:3px 3px 0 var(--teal);}
 /* seed source tabs */
 .seeds{margin-top:1.8rem; border-top:1px dashed var(--line); padding-top:1.4rem;}
 .seedtabs{display:flex; gap:.4rem; flex-wrap:wrap; margin-bottom:1rem;}
 .seedtab{font-family:'JetBrains Mono',monospace; font-size:.7rem; letter-spacing:.05em;
   text-transform:uppercase; border:1px solid var(--line); background:#fff;
   border-radius:8px; padding:.5rem .8rem; cursor:pointer; color:var(--ink-soft);
   display:flex; align-items:center; gap:.4rem; transition:all .15s ease;}
 .seedtab .tag{font-size:.62rem; padding:.05rem .4rem; border-radius:5px;}
 .seedtab .tag.free{background:var(--teal-soft); color:var(--teal);}
 .seedtab .tag.paid{background:var(--vermilion-soft); color:var(--vermilion);}
 .seedtab[aria-selected="true"]{border-color:var(--ink); color:var(--ink);
   box-shadow:inset 0 -3px 0 var(--gold); background:#fff;}
 .seedpane{display:none;}
 .seedpane.on{display:block; animation:fade .25s ease;}
 @keyframes fade{from{opacity:0; transform:translateY(4px);}to{opacity:1; transform:none;}}
 .seedpane label{font-size:.85rem; color:var(--ink-soft); display:block; margin-bottom:.4rem;}
 .egress{font-size:.8rem; line-height:1.5; color:var(--ink); background:var(--vermilion-soft);
   border:1px solid var(--vermilion); border-radius:10px; padding:.7rem .9rem; margin-bottom:.7rem;}
 .egress b{color:var(--vermilion);}
 .field{width:100%; font-family:'Hanken Grotesk',sans-serif; font-size:1rem;
   border:1px solid var(--line); border-radius:10px; padding:.7rem .9rem; background:#fff;
   color:var(--ink);}
 .field:focus{outline:none; border-color:var(--teal); box-shadow:0 0 0 3px var(--teal-soft);}
 textarea.field{font-family:'JetBrains Mono',monospace; font-size:.82rem; line-height:1.6;
   resize:vertical; min-height:6rem;}
 .linkbtn{margin-top:.5rem; background:none; border:0; color:var(--teal); cursor:pointer;
   font-family:'JetBrains Mono',monospace; font-size:.74rem; padding:0; text-decoration:underline;}
 .full-pill{margin-top:.6rem;}
 /* run button */
 .run{margin-top:1.6rem; display:flex; align-items:center; gap:1rem; flex-wrap:wrap;}
 .run button{font-family:'Hanken Grotesk',sans-serif; font-weight:700; font-size:1rem;
   color:var(--card); background:var(--ink); border:0; border-radius:12px;
   padding:.85rem 1.6rem; cursor:pointer; display:inline-flex; align-items:center; gap:.5rem;
   box-shadow:0 8px 20px -10px var(--shadow); transition:transform .12s ease, background .2s;}
 .run button:hover{transform:translateY(-2px); background:var(--vermilion);}
 .run small{color:var(--ink-soft); font-size:.82rem;}
 .htmx-request .run button{opacity:.6; pointer-events:none;}
 .spin{display:none;} .htmx-request .spin{display:inline-block; animation:rot 1s linear infinite;}
 @keyframes rot{to{transform:rotate(360deg);}}
 /* aside: how it works */
 aside{font-size:.92rem;}
 aside h2{font-family:'Fraunces',serif; font-weight:600; font-size:1.3rem; margin:.2rem 0 1rem;}
 .step{display:flex; gap:.8rem; margin-bottom:1.1rem;}
 .step .n{flex:0 0 1.9rem; height:1.9rem; border-radius:50%; border:1.5px solid var(--ink);
   font-family:'Fraunces',serif; font-weight:600; display:flex; align-items:center;
   justify-content:center; font-size:.95rem;}
 .step p{margin:.15rem 0; color:var(--ink-soft);}
 .step b{color:var(--ink); font-weight:600;}
 .ledger{font-family:'JetBrains Mono',monospace; font-size:.74rem; color:var(--ink-soft);
   border:1px dashed var(--line); border-radius:10px; padding:.9rem 1rem; margin-top:1.4rem;
   line-height:1.9;}
 .ledger .free{color:var(--teal);} .ledger .paid{color:var(--vermilion);}
 /* preview */
 #preview{margin-top:2.4rem;}
 .placeholder{font-family:'Fraunces',serif; font-style:italic; font-size:1.15rem;
   color:var(--ink-soft); border:1px dashed var(--line); border-radius:16px;
   padding:2.4rem; text-align:center; background:var(--card);}
</style></head><body>
<div class="wrap">
 <header class="mast">
   <h1>Synthetic Data <em>Generator</em></h1>
   <span class="kicker">pocsynth · bedrock + faker</span>
 </header>
 <p class="tagline">A real data-generation utility. Compose a dataset like a sentence,
   preview the shape, then export the <b>full set at any row count</b> — row
   generation runs locally with Faker.</p>

 <div class="layout">
  <form class="card" hx-post="/preview" hx-target="#preview" hx-swap="outerHTML"
        hx-encoding="multipart/form-data" hx-indicator="this">
   <p class="sentence">
     Generate a
     <span class="pill num"><input type="number" name="rows" value="1000" min="1" step="1"></span>
     row dataset of
     <span class="pill"><select name="record_type">__RECORD__</select></span>
     for
     <span class="pill"><select name="scenario">__SCENARIO__</select></span>, as
     <span class="pill"><select name="shape">__SHAPE__</select></span>,
     with
     <span class="pill"><select name="variation">__VARIATION__</select></span> realism, shaped as
     <span class="pill"><select name="time_shape" id="time_shape" onchange="toggleSeries(this)">__TIMESHAPE__</select></span><span
       id="series-clause" class="series-clause" hidden>
     covering
     <span class="pill"><select name="period">__PERIOD__</select></span>
     at
     <span class="pill"><select name="granularity">__GRAN__</select></span> granularity
     with
     <span class="pill"><select name="trend">__TREND__</select></span> trend</span>.
   </p>

   <div class="seeds">
    <input type="hidden" name="seed_mode" id="seed_mode" value="pills">
    <div class="seedtabs" role="tablist">
      <button type="button" class="seedtab" role="tab" aria-selected="true"
        onclick="pickSeed(this,'pills')">▣ Compose with pills</button>
      <button type="button" class="seedtab" role="tab" aria-selected="false"
        onclick="pickSeed(this,'custom')">✎ Describe your own <span class="tag paid">custom</span></button>
      <button type="button" class="seedtab" role="tab" aria-selected="false"
        onclick="pickSeed(this,'upload')">⬆ Match a document <span class="tag paid">sent to Bedrock</span></button>
    </div>
    <div class="seedpane on" data-seed="pills">
      <label>The sentence above composes the prompt. Bedrock designs the schema;
        row generation runs locally with Faker.</label>
    </div>
    <div class="seedpane" data-seed="custom">
      <label>Describe exactly the dataset you need — columns, ranges, relationships.
        The more specific, the better the schema.</label>
      <textarea class="field" name="prompt" rows="5"
        placeholder="Describe your dataset…"></textarea>
      <button type="button" class="linkbtn" onclick="loadExample()">↻ load the worked example</button>
    </div>
    <div class="seedpane" data-seed="upload">
      <div class="egress">⚠ Your document is uploaded and its text is sent to
        <b>Amazon Comprehend</b> (PII detection); the field names
        <b>and a sample of their actual values</b> are sent to
        <b>Amazon Bedrock</b> (schema design) in your AWS account. Don't upload
        data you aren't authorized to send to those services.</div>
      <label>Upload a real document to mirror its shape. The values are audited
        with Comprehend and the generated output is scanned for leaks — best-effort,
        not a guarantee. Review the output before sharing.</label>
      <input class="field" type="file" name="seed_document" accept="application/pdf">
    </div>
   </div>

   <div class="run">
     <button type="submit"><span class="spin">◠</span> Preview&nbsp;↑</button>
     <small>Preview shows a __PREVIEWN__-row sample. Download generates the full count locally, streamed.</small>
   </div>
  </form>

  <aside>
   <h2>How it works</h2>
   <div class="step"><div class="n">1</div><div>
     <p><b>Compose or describe.</b> Pick a record type and the workload it's for,
     write your own spec, or upload a document to mirror.</p></div></div>
   <div class="step"><div class="n">2</div><div>
     <p><b>Preview the shape.</b> Bedrock designs the schema; see the columns and a
     sample of rows before committing.</p></div></div>
   <div class="step"><div class="n">3</div><div>
     <p><b>Export the full set.</b> Stream CSV or JSON at any row count — the schema
     is reused, so row generation stays local (Faker).</p></div></div>
   <div class="ledger">
     <div><span class="free">●</span> generate · stream · download — <span class="free">local Faker, unlimited</span></div>
     <div><span class="paid">●</span> schema design — <span class="paid">one Bedrock call</span></div>
     <div style="margin-top:.4rem">uploaded documents are sent to AWS (Comprehend + Bedrock), PII-audited &amp; output scanned for leaks</div>
   </div>
  </aside>
 </div>

 <div id="preview">
   <p class="placeholder">No data yet — compose the sentence and press Preview.</p>
 </div>
</div>

<script>
 function pickSeed(tab, which){
   document.querySelectorAll('.seedtab').forEach(t=>t.setAttribute('aria-selected', t===tab));
   document.querySelectorAll('.seedpane').forEach(p=>
     p.classList.toggle('on', p.dataset.seed===which));
   // Record the active seed source so the server routes on intent, not on
   // whichever field happens to be non-empty.
   const sm=document.getElementById('seed_mode'); if(sm)sm.value=which;
   // Clear competing inputs so the chosen source wins server-side.
   if(which!=='custom'){const e=document.querySelector('[name=prompt]'); if(e)e.value='';}
   if(which!=='upload'){const e=document.querySelector('[name=seed_document]'); if(e)e.value='';}
 }
 function loadExample(){
   const e=document.querySelector('[name=prompt]'); if(e) e.value=__EXAMPLE_JSON__;
 }
 function toggleSeries(sel){
   // Time-series sub-clause (period/granularity/trend) only applies to a time series.
   const series=document.getElementById('series-clause');
   if(series) series.hidden = (sel.value !== 'a time series');
 }
 function copyCmd(btn){
   // Copy the sibling <pre>'s command text to the clipboard.
   const pre = btn.closest('.cmd').querySelector('pre');
   const text = pre ? pre.textContent : '';
   const done = () => { btn.textContent='copied'; btn.classList.add('copied');
     setTimeout(()=>{ btn.textContent='copy'; btn.classList.remove('copied'); }, 1500); };
   if(navigator.clipboard && navigator.clipboard.writeText){
     navigator.clipboard.writeText(text).then(done).catch(done);
   } else { done(); }
 }
</script>
</body></html>
"""
