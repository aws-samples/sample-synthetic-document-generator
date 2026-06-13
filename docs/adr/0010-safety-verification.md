# 0010 — Affirmative safety verification of generated output

**Status:** accepted

The PII guarantee was *enforced* by the pipeline (ADR-0005) but never *proven* about a
given output: `generate`/`test` did no leak-checking. We add a `verify` step that affirmatively
checks a generated dataset for leakage of real source values, producing a pass/fail **Attestation**.

## What it checks

- **Both surfaces:** the generated **Rows** *and* the **Schema** artifact (a real value can leak
  into an `enum`, a `regex` pattern, or a model-written `description`, and the Schema is shared too).
- **Against the Comprehend-flagged PII values** from the originating **Sample** — exact whole-value
  matching, not blanket substring scanning. Rationale: those are the values the guarantee is about;
  they are long enough that whole-value matching avoids false positives; and non-PII real values
  (state codes, plan tiers) are *allowed* to survive as enums by design (ADR-0005), so scanning for
  them would be wrong.

## Why a distinct `verify` verb (not folded into `test` or `generate`)

`test` checks Rows against the *Schema's rules*; `verify` checks Rows against *real source values*
for leakage — a different subject (see CONTEXT.md). Folding into `test` re-opens the ambiguity the
glossary closed. `generate` is the free/offline stage run repeatedly at scale and a hand-written
schema has no Sample, so coupling it to a Sample is wrong. `verify` is its own offline, free verb;
in the UI it auto-runs after a document-seeded generate.

## Consequences

- Closes the regex/enum-fragment leak hole (a real prefix in a model-emitted `regex` is caught
  because the Schema is scanned).
- Requires the originating Sample to verify; a dataset generated from a hand-authored schema (no
  real source) has nothing to verify against and `verify` reports "not applicable".
- Pairs with ADR-0011 (fail-closed default).
