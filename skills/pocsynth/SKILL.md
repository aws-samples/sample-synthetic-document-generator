---
name: pocsynth
description: >
  Convert PDFs to synthetic HTML/Markdown via Amazon Bedrock, audit
  documents for PII with Amazon Comprehend, and generate synthetic
  tabular datasets (CSV/JSON) from a schema, a preset, a natural-language
  business description, or a real document. Use when the user asks to
  synthesize a document, redact PII, scan a file for sensitive data,
  generate fake/test/demo data, build a synthetic dataset, or extract a
  reusable data schema from a sample PDF. The skill ships a self-contained
  Python CLI (`./pocsynth.py`) that returns a stable JSON envelope with
  classifiable exit codes.
---

# pocsynth

This skill bundles a single-file Python CLI (`./pocsynth.py`) that wraps
Amazon Bedrock + Amazon Comprehend for PDF → synthetic-document workflows.
The script is self-deploying via [uv](https://docs.astral.sh/uv/) script
mode: on first invocation uv resolves its dependencies into an ephemeral
cached venv and runs; subsequent runs are instant.

## Multi-step workflows

For multi-step workflows — building a synthetic corpus, benchmarking
three models on the same doc, producing a customer handover package,
running a live demo — read `./RECIPES.md` (in this skill directory) and
follow the matching recipe. Recipes layer on top of the baseline
confirm/fast-mode rules below; they don't replace them.

For single-doc "just convert this" requests, skip recipes and use the
baseline flow.

## When to use this skill

Trigger phrases:
- "Convert this PDF to synthetic HTML" / "redact the PII in <file>"
- "What PII is in this document?"
- "Which Bedrock model should I use for a 500-page contract?"
- "Scan <file> for sensitive data"
- Any PDF → structured-text conversion intended for sharing outside the
  original trust boundary.

## Prerequisites

1. **uv** on PATH (`which uv`). If missing, ask the user to install via
   `curl -LsSf https://astral.sh/uv/install.sh | sh` or direct them to
   <https://docs.astral.sh/uv/>.
2. AWS credentials with Bedrock and Comprehend access (see exit code 4
   below).

If `uv` is not available, fall back to `python -m pip install boto3
pymupdf typer rich beautifulsoup4 html2text requests` and invoke with
`python ./pocsynth.py ...` — but prefer the uv path.

## Invocation

Always use `--json` when driving the script from a skill. Canonical form:

    ./pocsynth.py --json <global-flags...> <subcommand> <subcommand-flags...>

The `--json` flag MUST come **before** the subcommand; the script rejects
it otherwise.

Subcommands:

| Command | Purpose | AWS? |
|---|---|---|
| `convert PDF` | Convert a PDF to synthetic HTML/Markdown. `result.cost` has post-run Bedrock + Comprehend $. | paid |
| `extract PDF` | Pull structured **records** (with `--schema`) or field **observations** (discovery) from a PDF via forced tool use. PII-audits the extracted values by default. | paid |
| `schema` | Build a generation-ready schema: `--from-sample` (from an extract), `--from-prompt` (from a description), or `--from-schema` (lint/document/`--fix` an existing one, offline). | infer/prompt paid; lint free |
| `generate` | Produce synthetic **rows** from `--schema` or `--preset`. `--rows`, `--seed`, `--format csv\|json`. | **free** |
| `test` | Validate generated rows against a schema. Exit **7** (`DATA_INVALID`) if rows violate it. | **free** |
| `presets` | List bundled preset schemas — 10 verticals (b2b_saas, ecommerce_orders, healthcare_lite, crm_contacts, insurance_claims, utility_meter, loyalty_pos, ad_campaign, knowledge_corpus, security_telemetry). All synthetic by construction → safe to share, no `verify` needed. | **free** |
| `verify` | Prove generated rows + schema carry no real PII from the seed Sample. Exit **8** (`PII_LEAK_DETECTED`), emits an Attestation. Use after `generate`/`extract` on a real-document seed. | **free** |
| `run` | **One-shot pipeline.** One seed source — `--preset` / `--prompt` (free/paid) or `--document` (paid: extract→schema→generate→**verify**). Safe-by-default (ADR-0011): paid paths need `--yes` above the ~$0.10 cost gate; the document path fails closed (exit **8**, NOT cleared for sharing) if a real value leaked. | preset free; prompt/document paid |
| `pii-audit FILE` | Re-scan a local text/HTML/MD file with Comprehend. | paid |
| `estimate <path>` | **Pre-flight cost estimate** (offline, ±30-50%). `--for convert\|extract\|schema`. Use before any paid run >20 pages or with Opus. | free |
| `ui` | Launch the demo web UI (requires `pip install 'pocsynth[ui]'`). Each preview shows the equivalent `run` command in both CLI and `--json` agent-skill form, so the UI doubles as a way to learn these commands. | — |
| `models` | List available Bedrock models + default. | free |
| `doctor` | Probe Python + boto3 + pymupdf + AWS creds + Bedrock + Comprehend. | paid (tiny) |
| `version` | Print script version. | free |

## Structured-data pipeline (extract → schema → generate → test)

`pocsynth` also generates **synthetic tabular data**, not just documents. The
pipeline splits into a **paid half** (run once) and a **free half** (unlimited):

- **Free, no AWS, no `doctor` needed:** `generate`, `test`, `presets`, and
  `schema --from-schema` (lint). Start here for "make me a demo dataset" — see
  RECIPES Recipe 5/6.
- **Paid (Bedrock), gate with `estimate --for`:** `extract` (PDF → sample) and
  `schema --from-sample`/`--from-prompt` (sample/description → schema). The
  cost-saver flow (RECIPES Recipe 7): extract a schema from ONE real PDF, then
  `generate` thousands of rows for free.

**PII posture (important):** `extract` audits the extracted values for PII by
default, and `schema --infer` does not let a real PII value become an enum —
PII fields are bound to Faker providers and the real values are discarded.
`verify` then scans the generated rows + schema for any real value that slipped
through (best-effort: Comprehend + an exact-value scan, may miss reformatted or
unflagged values). So the **generated dataset is intended to be shareable —
review it (or run `verify`) before sharing** — but the **extract sample and the
PII-audit CSV contain real values and must not be shared**. Processing a real
document sends its contents to Amazon Bedrock + Comprehend in your AWS account.

## Cost awareness

**Call `estimate` before any non-trivial `convert`.** Rough rule: if `estimate` says the run will cost more than **$0.10**, surface the number to the user via `AskUserQuestion` before proceeding — even in fast mode. Read `result.total_cost_usd` from the estimate envelope.

The `estimate` envelope contains `pricing_retrieved` (snapshot date) and `pricing_stale_days`. If the snapshot is >90 days old the CLI adds a `warnings` entry — relay that to the user verbatim; they may want to verify current AWS rates.

`convert`'s envelope also includes a top-level `result.cost` block after the run completes. Surface `result.cost.total_cost_usd` in your human-mode summary of the conversion.

## First call in a new environment

**Always run `doctor` first** when the user has not used the skill before
in this shell, or after an AWS credential change:

    ./pocsynth.py --json doctor

This takes ~6s and makes one real call each to STS, Bedrock, and Comprehend.
It returns `result.checks[]` with per-check `ok`/`detail` and a top-level
`result.all_ok`. If `all_ok: false`, do not proceed — surface the failing
checks to the user.

`doctor` is NOT skipped in fast mode. It's cheap, it catches auth /
model-access problems before they burn tokens on a large `convert`.

## Interaction modes

### Default — confirm mode

Before running `convert`, call `AskUserQuestion` with the outstanding
choices bundled into a single call (questions the user hasn't already
specified in the request). Include **only** the ones that are ambiguous —
don't ask about parameters the user already named:

- **Model** — Sonnet 4.6 (recommended default), Opus 4.6 (large docs,
  >200k input tokens), Haiku 4.5 (quick/cheap).
- **Output format** — HTML (default, good for layout fidelity) or
  Markdown (better for diff/readability).
- **Mode** — `synthetic` (rewrite prose + replace PII with realistic fake
  values — default for sharing) or `real` (preserve original text).
- **Page cap** — integer, or "all". Recommend capping a first run at ≤10
  to validate output before committing to a full doc.
- **PII audit** — on (recommended) vs off. The audit runs Comprehend over
  the output.
- **Redact PII values** — on (recommended if the output will be shared)
  vs off. When off, the PII audit CSV contains the raw matched PII
  values; when on, it contains `[REDACTED]` with the offsets preserved
  so findings are still locatable.

Skip confirmation entirely for:
- `doctor`, `models`, `version` (no user choices).
- `pii-audit` when the file path is supplied and `--redact-values` intent
  is already clear from context.

### Fast mode

If the user's request includes "just do it", "use defaults", "fast mode",
"no questions", "auto", or equivalent language, **skip `AskUserQuestion`
entirely** and invoke `convert` with:

    --model sonnet --format html --mode synthetic --pii-audit

Honor explicit flags the user gave (e.g., "fast mode but Markdown" =
defaults above with `--format markdown`). Still run `doctor` first on a
fresh environment.

### Warn before risky runs

**Even in fast mode**, call `AskUserQuestion` to confirm before:

- A run with **>50 pages** — costs real money on Sonnet, more on Opus.
- `--pii-audit` without `--redact-values` when the user's stated goal is
  external sharing — the audit CSV ships raw PII otherwise.
- `--num-docs > 1` — generates N independent synthetic copies; clarify
  intent (common mistake: user wants one doc; `--num-docs 5` produces
  five, multiplying cost).

## Exit codes and error routing

| Exit | Meaning | Action |
|---|---|---|
| 0 | Success | Read `result.output.combined_path` and hand it off. |
| 1 | Unknown / internal | Surface `error.message` to the user. |
| 2 | Bad arguments | Fix the invocation and retry. |
| 3 | Input problem (file missing, non-PDF, SSRF-rejected URL) | Surface `error.hint` verbatim. |
| 4 | AWS auth failed or expired | Tell the user to refresh credentials (`aws sso login`, `aws configure`, etc.). Do not retry blindly. |
| 5 | Upstream (Bedrock / Comprehend / HTTP) | If `error.retryable: true`, retry with exponential backoff (2s, 4s, 8s, abort). Otherwise surface. |
| 6 | Partial success | Some pages processed, some failed. Check `result.output.pages_processed` vs `pages_attempted` and `result.page_failures[]`. Surface the summary; the partial output is still on disk. |
| 7 | Data invalid (`test` only) | Generated rows violate the schema. Read `result.context.report.violations` (row/field/rule each) and surface a summary. **Do not retry** — the data or schema needs fixing. |

Always read and surface `error.hint` to the user — it's written to be
actionable for both humans and agents.

## Example invocations (for pattern-matching)

    # First call in a new env
    ./pocsynth.py --json doctor

    # Pick a model for a big doc
    ./pocsynth.py --json models

    # Typical convert (interactive-confirmed flags shown)
    ./pocsynth.py --json convert contract.pdf \
        --model sonnet --format html --mode synthetic \
        --pages 10 --pii-audit --redact-values

    # Fast-mode convert
    ./pocsynth.py --json convert contract.pdf

    # Standalone PII audit of an existing file
    ./pocsynth.py --json pii-audit converted_doc.html --redact-values

    # Synthetic dataset from a preset (free, offline)
    ./pocsynth.py --json generate --preset b2b_saas --rows 1000 --seed 42 -o ./out
    ./pocsynth.py --json test --rows ./out/rows.csv --schema ./out/schema.json

    # Full pipeline: real PDF -> reusable schema -> unlimited free rows
    ./pocsynth.py --json estimate report.pdf --for extract          # cost gate
    ./pocsynth.py --json extract report.pdf -o ./out                # paid, PII-audited
    ./pocsynth.py --json schema --from-sample ./out/sample.json -o ./out  # paid
    ./pocsynth.py --json generate --schema ./out/schema.json --rows 10000 --seed 1  # free

    # Describe a business -> schema (no document)
    ./pocsynth.py --json schema --from-prompt "a B2B SaaS company's accounts" -o ./out

## Installing the skill system-wide

This skill follows the [Agent Skills specification](https://agentskills.io/specification)
so the same files work in any spec-compliant client. Pick the install
path that matches your client:

**Claude Code** (project-scoped):

    cp -r ./skills/pocsynth .claude/skills/pocsynth

**Claude Code** (user-scoped, available in every session):

    cp -r ./skills/pocsynth ~/.claude/skills/pocsynth

**Kiro** (workspace-scoped):

    cp -r ./skills/pocsynth .kiro/skills/pocsynth

**Kiro** (user-scoped):

    cp -r ./skills/pocsynth ~/.kiro/skills/pocsynth

**Cross-client** (Agent Skills convention):

    cp -r ./skills/pocsynth ~/.agents/skills/pocsynth

The bundled `./pocsynth.py` is self-deploying (via `uv run --script`), so
no additional install step is required at the destination.

## Tests & evaluation

Two layers:

- **Deterministic feature-compat tests** (pytest): live in
  `tests/unit/test_skill_script.py` in this repo. They subprocess both
  `pocsynth` (the installed package) and `./skills/pocsynth/pocsynth.py`
  (this bundled script) and assert byte-equal JSON envelopes. Run with:

      uv run pytest tests/unit/test_skill_script.py

- **Skill behavior evals** (manual, following the
  [anthropic/skills](https://github.com/anthropics/skills) skill-creator
  convention): defined in `./evals/evals.json`, graded by
  `./agents/grader.md`. Run via `uv run python
  scripts/run-skill-evals.py` at the repo root. Evals compare Claude's
  behavior with and without the skill installed and grade on
  deterministic (exit code / JSON parse / flag presence) + behavioral
  (AskUserQuestion called, error.hint surfaced, fast-mode skips
  confirmation) assertions.
