# pocsynth

`pocsynth` is a PoC tool that turns real documents into safe, synthetic artifacts via Amazon
Bedrock. Two halves: **document synthesis** (`convert` — a PDF → a synthetic rendered HTML/Markdown
document) and the **structured-data pipeline** (`extract → schema → generate → test` — a PDF → a
reusable schema → unlimited synthetic data rows). See [`docs/plan/structured-data-support.md`](docs/plan/structured-data-support.md)
and [`docs/adr/`](docs/adr/).

## Language

### Pipeline stages
**Convert**:
The existing command. Renders a synthetic *document* (HTML/Markdown) from a PDF.
_Avoid_: "extract" for this — convert produces a document, not data.

**Extract**:
Pulls structured *records / field observations* out of a real source document (paid Bedrock call).
Produces a **Sample**, never a document or a schema.
_Avoid_: using "extract" to mean cleaning/rendering text (that's Convert).

**Schema** (the command):
Turns a **Sample** into a **Schema** (the artifact) + documentation + a **Lint report** (infer mode,
paid); or documents/lints/fixes a user-supplied **Schema** (lint mode, free, offline).

**Generate**:
Produces synthetic **Rows** from a **Schema**, offline and free, via Faker.

**Test**:
Validates **Rows** against a **Schema**, offline and free. Emits a **Validation report**.
_Avoid_: "validate" as a command name — the command is `test`; "validation" names its output.

### Artifacts
**Schema** (the artifact):
The JSON spine of the pipeline (`schema: 1`, a list of typed **Fields**). Produced by Schema,
consumed by Generate / Test / conform-Extract. An editable, reviewable file — not a black box.

**Sample**:
Extract's output. In **Discovery** mode: grouped per-field observations with `value_counts`. In
**Conform** mode: **Records** keyed by a known schema's fields.
_Avoid_: "dataset" (that's the generated Rows), "schema" (the Sample feeds schema inference).

**Field**:
One column definition in a Schema: `name`, `type` (closed set, ADR-0006), and optionally
`faker` / `enum` / `weights` / `regex` / `description`.

**Records / Rows**:
**Records** = real values pulled by Extract (conform mode). **Rows** = synthetic values made by
Generate. Same tabular shape; opposite provenance — keep the words distinct.

**Observation**:
One discovery-mode finding: a field name + `value_counts` (distinct values seen and their counts).
Merged across pages by field name.

**Lint report** / **Validation report**:
Lint report = Schema's findings about a Schema (issues, recommendations, fixes). Validation report =
Test's findings about Rows (violations). Different stages, different subjects.

### Modes & qualities
**Discovery vs. Conform** (Extract modes):
Discovery = no schema yet, generic tool, emits observations. Conform = a schema is supplied, emits
records matching it.

**Infer vs. Lint** (Schema modes):
Infer = Sample → Schema (paid). Lint = Schema → report/fixes (free, offline).

**Distribution** (`infer | synthetic | uniform | auto`):
How a Field's enum **weights** are set (ADR-0004). Infer = from real `value_counts`; synthetic =
model-invented; uniform = none; auto = infer-where-possible-else-synthetic.

**PII field**:
A Field whose values Comprehend flags, or whose `faker` binding is identifying. Such fields never
carry real-value enums (ADR-0005).

**Paid half vs. free half**:
Paid = Extract + Schema-infer (Bedrock, run once). Free = Generate + Test + Schema-lint (offline,
unlimited). The economic spine of the pipeline.

## Relationships

- **Extract** produces a **Sample**; **Schema-infer** consumes it to produce a **Schema**.
- A **Schema** is consumed by **Generate** (→ **Rows**), **Test** (validates **Rows**), and
  conform-**Extract** (shapes its **Records**).
- **Generate** then **Test** form the keystone round-trip: Rows generated from a Schema always
  validate against it (ADR-0006).
- A **PII field** in a **Sample** suppresses real-value enums in the resulting **Schema**.

## Example dialogue

> **Dev:** "Does `extract` give me the schema I pass to `generate`?"
> **Domain expert:** "No. Extract gives you a **Sample** — observed values from the PDF. **Schema**
> (infer mode) turns that Sample into the **Schema**. Generate reads the Schema, never the Sample."
>
> **Dev:** "So if a field only ever shows three values, it becomes an enum?"
> **Domain expert:** "If it's not a **PII field**, yes — and with `--distribution infer` it keeps the
> real **weights**. If it *is* PII, the real values are dropped and it's bound to a Faker provider."

## Flagged ambiguities

- "extract" was overloaded to mean both *pull structured records* and *produce clean content* —
  resolved: Extract = records (a **Sample**); clean rendered content is **Convert**'s job.
- "schema" names both a **command** and the **artifact** it produces — kept (context disambiguates),
  but never use "schema" for the **Sample** that feeds inference.
- "records" vs. "rows" — resolved: Records = extracted real values; Rows = generated synthetic values.
- "validate"/"test" — resolved: the command is **Test**; "validation report" is its output.
