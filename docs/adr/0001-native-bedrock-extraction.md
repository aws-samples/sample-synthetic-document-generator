# Native Bedrock extraction instead of langextract

**Status:** accepted (2026-06-07)

The `extract` command performs structured-data extraction natively through the
existing `bedrock.py` `converse` path (JSON output, optionally constrained via
Bedrock `toolConfig`), rather than adopting `google/langextract` with a custom
in-process Bedrock provider.

**Why:** langextract's real value-adds — source grounding (char offsets) and an
interactive HTML visualizer — are decorative for our headline use case
(extract the schema/shape from one PDF → feed field names to `generate`), where
the extraction output is feedstock for Faker, not an end-user-facing grounded
document. Going native keeps a **single Bedrock code path** (one place for error
translation via `translate_aws_error` and token accounting), avoids a **second
`converse` path** to keep in sync, removes a **young-API version bet** (no native
Bedrock provider exists; we'd own and pin it), and keeps the **single-file
`uv run --script` skill bundle light** — langextract's heavy transitive tree
only fit as an optional extra the core skill couldn't depend on.

**Considered:** langextract as a core dep (rejected: bundle weight, API risk);
langextract as an optional `[grounding]` extra (deferred, not adopted).

**Reversal:** documented in
[`../research-notes/08-langextract.md`](../research-notes/08-langextract.md) —
add as `pocsynth[grounding]` if a demo/compliance scenario needs displayed
source offsets, if docs exceed a single Bedrock call, or if the HTML visualizer
becomes a wanted deliverable. Native extraction stays the floor regardless.
