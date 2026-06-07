# Demo UI: FastAPI + HTMX, thin layer over the core, optional `[ui]` extra

**Status:** accepted (2026-06-07)

A local demo web app (`src/pocsynth/ui/`, launched by `pocsynth ui`) built with
**FastAPI + HTMX**, inspired by Metabase's AI Data Generator: a fill-in-the-blank
sentence with inline dropdown pills + submit arrow, a 10-row preview, and CSV/JSON
download. The backend calls the core functions (`load_preset`, `run_schema`,
`run_generation`) **in-process** and returns the same envelopes the CLI uses.
Shipped behind an optional `pocsynth[ui]` extra (`fastapi`, `uvicorn`; HTMX is a
vendored script) — never a core or skill dependency.

**Why this stack:** the repo is 100% Python with a deliberately minimal dependency
story (the skill is one self-contained `uv run --script` file). A Next.js/React
frontend (what Metabase uses) would add a second language, a node/npm build
pipeline, and a JS↔Python serialization seam — for a *demo*. FastAPI+HTMX stays
in one language, needs no build step, and calls the core directly, so there's no
subprocess or second envelope serialization. Streamlit was considered but its
rerun-on-interaction model is awkward around a *paid* LLM call; explicit
endpoints make the cost gate cleaner.

**Scope:** three seed sources, matching the three SA demo-data scenarios the UI
must serve: (1) a bundled **preset** (free, instant); (2) **describe a business**
→ `schema --from-prompt` (paid); (3) **upload a seed document** → `extract` (+ PII
audit) → `schema --infer` (paid) — this covers a customer running it locally on
their own PII-bearing document and an SA seeding from public data. All paid paths
sit behind an explicit button + the ADR-0007 cost gate. The Scenario-1 guarantee
(real values never leak into preview or download) is enforced by ADR-0005 in the
shared core, so it holds identically on the web path. Paid Bedrock/Comprehend
clients are FastAPI dependencies (`get_bedrock_client` / `get_comprehend_client`)
so tests override them with stubs and no AWS is touched.

Metabase's growth/variation/granularity/year pills are **time-series** controls
the core doesn't model (ADR-0004 scope) — shown as "coming soon", not faked. No
auth, persistence, multi-user, Metabase handoff, or pixel-matched styling.
Depends on ADR-0003 (core verbs), ADR-0008 (the from-prompt "AI" mode), and
ADR-0005 (the PII guard that makes uploaded-document seeding safe).
