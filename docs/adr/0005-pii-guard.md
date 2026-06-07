# PII is audited on extraction and barred from leaking into synthetic output

**Status:** accepted (2026-06-07)

The paid half of the pipeline handles real values pulled from a real document, so
two guards are mandatory:

1. **Audit the extract sample.** `extract` runs the existing Comprehend PII audit
   (`scan_for_pii`, reused verbatim) over its output by default
   (`--pii-audit/--no-pii-audit`, mirroring `convert`), writing the same audit CSV
   and reporting `entities_found`. It also flags **per field** whether Comprehend
   detected PII in that field's values (`pii: bool` stamped onto the sample).
2. **Bar real values from the schema.** `schema --infer` never emits a real-value
   `enum`/`weights` for a field flagged `pii: true` (or whose inferred `faker`
   binding is in `PII_FAKER_PROVIDERS`). Such fields are bound to a Faker provider
   regardless of cardinality, the real values discarded, and the suppression logged
   in the lint report. Non-PII low-cardinality fields (state, plan tier) keep their
   real enums and inferred weights. An offline `lint_schema` rule (PII provider +
   literal enum → high severity, autofixable) backstops the same invariant for
   user-authored schemas where no `extract` flag exists.

**Why:** the project's premise is that synthetic data exists *to avoid* handling
real PII; a pipeline that silently baked real names/SSNs/account-numbers into
"synthetic" output as enums would be the "partially synthetic data is still PII"
trap our own research notes warn about
(`docs/research-notes/04-compliance-privacy-bias.md`). Auditing reuses
infrastructure already in the repo, so the cost is low.

**Consequences:** `extract` builds a Comprehend client (shared session, like
`core.run_conversion`) and writes the scanned text to `output.combined_path` so
`actual_convert_cost` prices the audit unchanged. Detection is only as good as
Comprehend (English, entity-type coverage gaps) — a missed value could survive;
the offline lint rule and lint-report transparency are the second line. The
extract sample and audit CSV are **sensitive artifacts**; only the final generated
dataset is safe to share. Depends on ADR-0003 (pipeline) and ADR-0004 (the enum
weights this guard strips for PII fields).
