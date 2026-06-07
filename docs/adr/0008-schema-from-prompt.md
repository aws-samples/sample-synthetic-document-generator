# `schema --from-prompt`: infer a schema from a natural-language business description

**Status:** accepted (2026-06-07)

`schema` gains a third source mode, `--from-prompt TEXT`, that turns a
natural-language business description (e.g. "a B2B SaaS company's subscription
records") directly into a generation-ready schema — no source document. It joins
`--from-sample` (from an extract sample) and `--from-schema` (offline lint) and
uses the **same** forced `emit_schema` toolConfig (ADR-0002); only the input
differs (prompt vs. sample).

**Why:** the Metabase-style demo UI (ADR-0009) and the broader "AI data
generator" use case need a path from *intent* to *schema*, which the
document-only pipeline (ADR-0003) lacked. Adding it as a `schema` source — rather
than a separate command or letting the UI own an LLM call — keeps schema
generation in one place, reuses the existing tool/prompt machinery, and means the
UI stays a thin layer over core verbs that emit the same envelopes as the CLI.

**Consequences:** with no document there are no `value_counts`, so distribution
(ADR-0004) can only be `synthetic`/`uniform` (model-invented weights), not
`infer`. With no Comprehend pass over real values, the PII guard (ADR-0005) relies
on the inferred-`faker`-provider heuristic plus the offline `lint_schema` rule —
acceptable because a prompt contains no real PII to leak in the first place. It's
a paid call, gated by `estimate --for schema` (ADR-0007). Depends on ADR-0002.
