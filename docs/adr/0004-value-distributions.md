# Value distributions are first-class, with a three-mode user option (default auto)

**Status:** accepted (2026-06-07)

Schema fields may carry `weights` (an enum→probability map) so categorical
generation is non-uniform, preserving real-world value frequencies rather than
defaulting to equal probability. The `schema` command derives them via a
`--distribution` option:

- **infer** — exact weights from the real document's observed value frequencies
  (`extract` discovery now reports `value_counts`; `weights_from_counts`
  normalizes them). Most faithful; needs frequency data.
- **synthetic** — the model invents plausible weights (no real frequencies
  needed/available).
- **uniform** — no weights; equal probability (the format-only baseline).
- **auto** (default) — per field: `infer` when the sample carries `value_counts`,
  else `synthetic`; the chosen source is recorded per field (`weights_source`)
  and a synthetic fallback is noted in the lint report.

`generate` applies weights through Faker's `random_element(OrderedDict(...))`.

**Why now, not later:** `extract` discovery is already at the document; capturing
value frequencies there is cheap, but retrofitting it after the sample shape
ships would be a reshape (the sample → schema seam would have to change). The
weighting itself is a small, additive Faker change. Distribution drift is the
representativeness risk our own research notes flag first
(`docs/research-notes/03-quality-and-representativeness.md`), so making it a
default-on, user-controllable behavior — rather than a deferred feature — is
worth the modest v1 cost.

**Scope / consequences:** v1 models distribution **only for low-cardinality
`enum` fields**. Continuous numeric distributions, inter-field correlations, and
time series are explicitly out of scope (documented as a limitation so the output
isn't mistaken for training-grade data). Weights are only as good as the page
sample size; sparse-evidence `infer` weights are flagged in the lint report.
Depends on ADR-0002 (the `emit_schema` tool gains optional per-field `weights`)
and ADR-0003.
