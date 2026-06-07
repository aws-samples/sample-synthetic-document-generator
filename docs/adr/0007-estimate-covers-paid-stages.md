# `estimate` covers the paid pipeline stages (extract, schema --infer)

**Status:** accepted (2026-06-07)

The `estimate` command gains a `--for convert|extract|schema` target (default
`convert`, so existing usage is unchanged) and two new helpers,
`estimate_extract_cost` and `estimate_schema_infer_cost`, alongside the existing
`estimate_convert_cost`. All three return the identical estimate envelope.

**Why:** the skill teaches agents one cost-safety idiom — "`estimate` first, gate
at $0.10, then spend" — but the pipeline's two paid stages (`extract`,
`schema --infer`) had no pre-flight path, so an agent driving a pipeline billed
as "run once, then free" was flying blind on the *once*. Extending `estimate`
(rather than adding a second cost mechanism) keeps that single idiom intact.
`extract` shares `convert`'s per-page image+text Bedrock shape, so it reuses the
existing heuristic machinery, `_finalize_envelope`, and stale/region warnings —
differing only in a lower output-token ratio (structured records are smaller than
rendered HTML/MD). `schema --infer` is one small call over the sample, estimated
offline by token-counting the sample file (no PDF).

**Considered:** post-hoc `cost` only + leaning on `--pages` as the control
(rejected: breaks the "always estimate first" muscle the skill trained); a
per-command `--dry-run` (rejected: folds estimation into each paid command
instead of keeping one `estimate` entry point).

**Consequences:** a new `EXTRACT_OUTPUT_TOKENS_PER_INPUT_TOKEN_RATIO` constant
needs the same kind of live-run calibration the convert ratio got
(`pricing.py:38`); until calibrated it's a documented guess. `estimate --for schema`
takes a sample path, not a PDF, and stays offline. Depends on ADR-0003 (the paid
stages) and ADR-0005 (PII-audit cost is included in the extract estimate when on).
