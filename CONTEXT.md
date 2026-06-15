# pocsynth

`pocsynth` is a PoC tool that turns real documents into safe, synthetic artifacts via Amazon
Bedrock. Two halves: **document synthesis** (`convert` — a PDF → a synthetic rendered HTML/Markdown
document) and the **structured-data pipeline** (`extract → schema → generate → test` — a PDF → a
reusable schema → unlimited synthetic data rows).

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

**Verify**:
Checks **Rows** (and the **Schema** artifact) against the real source values in a **Sample** for
PII leakage, offline and free. Emits an **Attestation** (pass/fail). A distinct subject from **Test**
(rules vs. real-value leakage); in the UI it auto-runs after a document-seeded generate.
_Avoid_: folding into **Test** — they check different things.

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

### Personas
Personas are split by **trust boundary** (where the tool runs and whose data seeds it), not by
job title — that split drives what each one needs.

**SA** (Solutions Architect, sandbox):
Runs pocsynth in an AWS sandbox/non-production account to build or demo a prototype. Seeds only
from a **Preset** or a **Prompt** — never real customer data (real data in a non-prod account is a
reportable security incident). Optimizes for speed, believability, cost control, and a
safe-to-share artifact.
_Avoid_: "internal user". A **Customer-runner** is not an SA even when they're an Amazon engineer.

**Customer-runner** (own account):
A customer engineer — or an SA working inside the customer's own account/VDI — who runs pocsynth
where real documents (leases, claims, grants) *may* be used as seeds because the data never leaves
the customer's trust boundary. Optimizes for a trivially-provable PII-never-leaks guarantee and
self-service simplicity. Subsumes the pure self-service customer (same needs, no SA present).
_Avoid_: "customer" alone (ambiguous: the runner vs. the end customer of the demo).

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

**Safety verification** (the affirmative non-leak check):
An offline check that scans the generated **Rows** for any real source value seen in the **Sample**
and returns a pass/fail. Turns "designed not to leak" into "checked this output, it didn't." The
**Customer-runner** needs this (real data was involved); for the **SA** it's reassurance only.
_Avoid_: conflating with **Test** — Test checks rows against the Schema's rules; Safety verification
checks rows against the *real source values* for leakage. Different subjects.

**Attestation** (the safety report artifact):
A structured, hashable record of a Safety verification — source-doc hash, rows hash, verdict, tool
version — that a Customer-runner can attach to a security review. The persistable output of Safety
verification.

**One-shot run** (the unifying orchestration verb):
A single command that chains the whole pipeline to a dataset so a persona never threads artifacts.
Two seed sources, one verb:
- prompt/preset seed → schema → generate (free; the **SA**'s fast path).
- document seed → extract → schema → generate → **verify** (paid; the **Customer-runner**'s path,
  with **Attestation** emitted and the cost gate shown before spend).
_Avoid_: treating "quickstart" (prompt/preset) and "fromdoc" (document) as two features — they are
one verb differing only by seed source.

**Command equivalent** (the web UI's teaching artifact):
What the web UI displays alongside a preview to show how to reproduce the dataset outside the
browser, two ways: the exact CLI **One-shot run** command (`pocsynth run …`), and a plain-language
**`/pocsynth` agent-skill request** (Kiro or Claude Code — the skill composes and runs the CLI). A
*teaching artifact*, not the code that ran: it mirrors the user's selections (record type/scenario →
composed prompt, rows, seed) so an **SA** can show a customer the command line, and a customer can
learn the workflow.
_Avoid_: showing `./pocsynth.py …` as the "skill call" — a skill is invoked in natural language
(a `/pocsynth` request), not by running its bundled script; that script is what the skill runs
internally. Also avoid implying a byte-faithful replay — the document `run --document` does a fuller
Bedrock extraction than the in-browser preview; the panel says so.

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
