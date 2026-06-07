# Four-stage structured-data pipeline: extract → schema → generate → test

**Status:** accepted (2026-06-07)

Structured-data support is four CLI subcommands with clean artifact contracts,
not one mega-command:

- **extract** (paid) — a real source doc → a **data sample** (records / field
  observations) via forced `toolConfig`.
- **schema** (hybrid) — a sample → an inferred **generation-ready schema** + docs
  + lint report (paid, Bedrock); **or** a user schema → docs + lint + optional
  `--fix` (free, offline).
- **generate** (free) — a schema → synthetic **rows** (Faker, offline).
- **test** (free) — rows + schema → a **validation report** (offline).

**Why four:** inferring a Faker recipe from a real document is a genuinely
different job from pulling records out of it, so it gets its own (`schema`)
stage rather than being smuggled into `extract`. Splitting `test` out makes
"do the generated rows actually satisfy the schema?" a first-class, gateable
step. The split also draws the cost line exactly where it belongs: **extract +
schema --infer are paid and run once; generate + test + schema --lint are free
and unlimited** — a no-AWS iterate loop. `convert` (synthetic *document*
rendering) stays distinct from `extract` (structured *records*); no overlap.

**Considered:** (a) two commands `extract`+`generate` with a hand-authored schema
carrying both extract and Faker halves — rejected: makes the human the bridge and
never infers the recipe. (b) `extract` emitting a generation-ready schema directly
— rejected: conflates record extraction with recipe inference and hides the lint
step. (c) folding validation into `generate` — rejected: validation must run on
arbitrary rows (incl. externally produced) and gate independently.

**Consequences:** `extract` needs two modes (discovery before a schema exists;
conform once one does) — see ADR-0002. `schema --infer` is the second paid
Bedrock call. v1 ships flat fields only; nested tables / referential integrity
deferred. Depends on ADR-0001 and ADR-0002.
