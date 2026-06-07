# Forced toolConfig for structured Bedrock output, no prompt-and-parse fallback

**Status:** accepted (2026-06-07)

Every stage that asks Bedrock for structured output forces a `toolConfig` tool
(`toolChoice` pinned to it) and reads the returned `toolUse.input`. There is
**no** secondary "ask for JSON in the prompt and parse/repair the text" path.
This applies to all three structured Bedrock calls in the pipeline:

- `extract` **discovery** mode → a generic `observe_fields` tool (no schema yet).
- `extract` **conform** mode → a field-specific `extract_records` tool whose
  `inputSchema` is built from a known schema (`schema_to_toolspec`).
- `schema --infer` → an `emit_schema` tool that forces a well-formed,
  generation-ready schema back from the model.

The tool's `inputSchema` is generic in discovery mode and field-derived in the
other two; the "forced tool, no prompt-parse" rule is identical across all.

**Why:** the schema contract is then enforced by Bedrock rather than hoped-for in
a prompt — the structural fix for the "unvalidated LLM JSON" weakness we flagged
reviewing metabase/dataset-generator. A single path is deterministically testable
with a canned `toolUse` payload; a dual path doubles the test/branch surface for
no real resilience (a prompt-parse of the same model on the same page won't
rescue a failed `toolUse`). `_validate_schema_shape` runs first, so
`schema_to_toolspec` is a pure transform over already-valid input.

**Consequences:** the only residual failure mode is a response with no `toolUse`
block (guardrail intervention / empty page) — that page is recorded in
`page_failures`; all-pages-empty raises `PartialError`, mirroring
`core.run_conversion`. Depends on ADR-0001 (native Bedrock extraction); applies
across the pipeline of ADR-0003.
