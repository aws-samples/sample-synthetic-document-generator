---
name: pocsynth
description: >
  Convert PDFs to synthetic HTML/Markdown via Amazon Bedrock, or audit
  existing documents for PII with Amazon Comprehend. Use when the user
  asks to synthesize a document, redact PII, scan a file for sensitive
  data, or convert a PDF for safe sharing. The skill ships a
  self-contained Python CLI (`./pocsynth.py`) that returns a stable JSON
  envelope with classifiable exit codes.
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

| Command | Purpose |
|---|---|
| `convert PDF` | Convert a PDF to synthetic HTML/Markdown. Envelope now includes `result.cost` with post-run Bedrock + Comprehend $ figures. |
| `pii-audit FILE` | Re-scan a local text/HTML/MD file with Comprehend. |
| `estimate PDF` | **Pre-flight cost estimate** (offline, heuristic, ±30-50%). Takes `--model`, `--pages`, `--pii-audit`. Use before running `convert` on anything >20 pages or with Opus. |
| `models` | List available Bedrock models + default. |
| `doctor` | Probe Python + boto3 + pymupdf + AWS creds + Bedrock + Comprehend. |
| `version` | Print script version. |

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
