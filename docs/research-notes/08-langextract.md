# langextract — deferred (what it could offer later)

[`google/langextract`](https://github.com/google/langextract) — Apache-2.0,
Python 3.10/3.11. LLM structured extraction with **source grounding** (char
offsets back into the source text) + an **interactive HTML visualization**.

> **Decision (2026-06-07): NOT used in v1.** `extract` is implemented natively
> through the existing `bedrock.py` `converse` path (see
> [`../plan/structured-data-support.md`](../plan/structured-data-support.md) and
> ADR [`../adr/0001-native-bedrock-extraction.md`](../adr/0001-native-bedrock-extraction.md)).
> This note records what langextract *would* buy us so a future maintainer can
> reverse the decision deliberately rather than re-research it.

## What it offers

- **Source grounding** — each extraction carries a `char_interval` (e.g. "this
  `full_name` came from chars 1240–1248"). Useful for auditability/provenance,
  in the same spirit as the PII-audit CSV.
  - *Caveat for us:* it grounds into the **text you pass it**. Our pipeline feeds
    Bedrock a page **image + `page.get_text("text")`**, so offsets index the
    flattened text layer, not visual PDF coordinates — weaker grounding than it
    appears for scanned/visual docs.
- **Interactive HTML viz** — `lx.visualize()` writes a self-contained HTML file
  highlighting extractions over the source. A nice demo artifact.
- **Extraction discipline for free** — few-shot `examples=[ExampleData(...)]`,
  multi-pass extraction (`extraction_passes`), large-doc chunking
  (`max_char_buffer`), parallelism (`max_workers`).

## Why deferred

- **Second Bedrock code path.** We already have exactly one
  (`bedrock.py::process_page` → `converse` → defensive text extraction →
  `translate_aws_error` → token accounting). langextract would wrap the *same*
  `converse` call inside a `BaseLanguageModel.infer()` subclass registered via
  `@router.register` — two paths to keep behaving identically.
- **Young API risk.** No native Bedrock provider exists; we'd own an in-process
  custom provider and pin a fast-moving v1.x whose `infer()` / `router` API could
  shift under us.
- **Skill-bundle weight.** The skill ships as a single file via stickytape +
  PEP 723 (`generate-skill-script.py`), run by `uv run --script`, guarded by a
  CI drift check and a perf-budget test. langextract's heavy transitive tree
  strains that — the only viable shape was an optional `[grounding]` extra, which
  the *core skill* couldn't rely on anyway.
- **Adapter tax.** Our flat schema would need `schema_to_langextract()` to
  translate into `prompt_description` + `ExampleData` — a layer existing only to
  feed the dependency.
- **Grounding is decorative for our headline use case.** The cost-saver flow is
  *extract the schema/shape from one PDF → feed field names to `generate`*. That
  needs values + field names, not char offsets.

## When to reverse this

Add langextract (as an optional `pocsynth[grounding]` extra, keeping native
extraction as the floor) **only if** one of these becomes real:

1. A **demo or compliance scenario** that actually displays highlighted source
   offsets / provenance to an end user.
2. **Documents larger than one Bedrock call** where hand-rolled chunking/multi-
   pass becomes a burden (note: today we're page-by-page on a 1M-context model,
   so a single page never overflows — this cost is ~zero for the current shape).
3. The **HTML visualizer** becomes a wanted deliverable in its own right.

### How we'd wire it (recorded so it isn't re-researched)

Subclass `langextract.core.base_model.BaseLanguageModel`, decorate
`@router.register(r'^bedrock')` (from `langextract.providers import router`),
implement `infer(self, batch_prompts, **kwargs) -> Iterator[Sequence[types.ScoredOutput]]`
yielding `[types.ScoredOutput(score=1.0, output=text)]` per prompt. Registration
fires in-process on import (no pip entry point). Route explicitly with
`lx.factory.ModelConfig(model_id="bedrock/<id>", provider="<ClassName>",
provider_kwargs={"client": bedrock_runtime_client})` then
`lx.factory.create_model(config)`. Reuse our `make_session` for the client and
`translate_aws_error` inside `infer`.

## Source

- Repo: <https://github.com/google/langextract>
- Related: [`07-faker.md`](./07-faker.md) (the generation half),
  [`../plan/structured-data-support.md`](../plan/structured-data-support.md).
