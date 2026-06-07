# Review: metabase/dataset-generator (read through a Bedrock-only lens)

Prior-art review of [`metabase/dataset-generator`](https://github.com/metabase/dataset-generator),
evaluated **as if the LLM layer were Amazon Bedrock and nothing else** — because
that is the constraint for this project (see
[`./structured-data-support.md`](./structured-data-support.md), which already
commits to Bedrock via `converse`).

> TL;DR — the *architecture* (LLM writes a spec, local code generates the rows)
> is exactly right and matches our plan's "one paid call → unlimited free rows"
> story. The *execution* has gaps we should not copy: no real foreign keys, no
> deterministic seeding, and unvalidated LLM output driving the pipeline.

---

## 1. What it is

A Next.js / TypeScript web app that generates realistic synthetic **business
datasets** for demos, dashboards, and learning, with one-click exploration in a
Dockerized Metabase. MIT licensed.

## 2. The core idea worth stealing — two-stage generation

```
user params ──► LLM ──► JSON spec ──► DataFactory ──► Faker ──► CSV / SQL
              (once, cached)          (local, free, instant, offline)
```

1. **LLM produces a *spec*, not data** — entities, attributes, an event-stream
   fact table, and a simulation model (recurring / random / churn events).
2. **Faker generates the *rows* locally** from that spec — no LLM call.
3. **Export never calls the LLM again** — exporting 1M rows is free and offline;
   the spec is cached per unique config.

This is the same economics as our plan: pay once to learn the *shape*, then
generate unlimited rows for free. Their direction is reversed from ours —
**they synthesize a spec from a prompt; we extract a spec from one real PDF** —
but the downstream "free local Faker generation" half is identical, which
validates the plan's headline cost story.

## 3. Architecture (the good part)

- `DataFactory` is a clean **facade / pipeline**: generate entities → simulate
  events → enforce business rules → format table → (optional) star-schema
  dimensions.
- It composes single-responsibility modules: `EntityGenerator`,
  `EventSimulator`, `TableFormatter`, `DataValidator`, `DataEnforcer`, domain
  `*Enforcer`s.
- The **event-simulation model** (recurring by frequency, random by avg/month,
  churn by monthly rate) is the most interesting element — it produces
  *narratives* (time progression, churn correlations) rather than random rows,
  which is exactly the "narrative-rich" property our
  [`../research-notes/06-demo-data-kit.md`](../research-notes/06-demo-data-kit.md)
  calls out as what makes demo data good.

---

## 4. The Bedrock-only re-evaluation

Their LLM layer is **multi-provider via LiteLLM** (OpenAI default; Anthropic /
Google optional), with a per-request probe to auto-detect a running LiteLLM
gateway. Under a Bedrock-only constraint, most of that machinery is dead weight
and several of their choices become anti-patterns:

| Their choice | Under Bedrock-only | Implication for us |
|---|---|---|
| LiteLLM gateway + Docker service | Not needed — `boto3` `bedrock-runtime.converse` talks to all Bedrock models through one API. | Our plan's `lx_bedrock.py` calling `client.converse(...)` is the right, simpler shape. Drop the gateway concept entirely. |
| Per-request synchronous probe GET to detect LiteLLM | Pure latency + a failure dependency on every cache miss. | Bedrock has no such probe — model selection is a `modelId` string. Avoid runtime provider auto-detection. |
| Direct-OpenAI vs. gateway client selection branch | Collapses to a single boto3 `bedrock-runtime` client (built via our `make_session(profile, region)`). | One client, one code path, region/profile from session — cleaner and testable (inject a mock client). |
| `OPENAI_API_KEY` env, hardcoded `"sk-1234"` master-key fallback | N/A — Bedrock uses the AWS credential chain (IAM role / profile / SSO), no app-managed API keys or secret fallbacks. | Strictly better security posture; no secrets in app config. Our plan already relies on the AWS session. |
| `response_format: { type: "json_object" }` (OpenAI JSON mode) | Bedrock has no universal "JSON mode"; for Anthropic models you constrain output via the prompt and/or **tool use / `toolConfig`** in `converse`, or parse a fenced block. | Don't assume a portable `json_object` flag. Use prompt-enforced JSON (the plan's existing `<raw_text>`-is-data prompt discipline) or `toolConfig` for a hard schema. |
| `LLM_MODEL` defaults to `gpt-4o` | Replace with a Bedrock model id, e.g. `global.anthropic.claude-sonnet-4-6`, selectable sonnet/opus/haiku. | Matches our plan's `--model sonnet\|opus\|haiku` and `model_id="bedrock/..."` convention. |
| Token-usage logging from OpenAI `usage` | Read `response["usage"]` from the Bedrock `converse` response instead. | Our plan already accumulates `input_tokens`/`output_tokens` off the converse response onto `self.usage` and costs via `actual_convert_cost`. |
| Ineffective 90s `AbortController` (signal never passed) | boto3 timeouts are set on the client (`botocore.config.Config(read_timeout=..., retries=...)`), not an abort signal. | Configure timeouts/retries on the boto3 client up front; don't port their broken abort pattern. |

**Net:** the Bedrock constraint *removes* an entire category of complexity they
carry (multi-provider routing, gateway Docker service, app-managed keys, probe
latency). The plan's `lx_bedrock.py` custom provider is the correct minimal
shape; this review reinforces *not* reintroducing a provider-abstraction layer.

---

## 5. Weaknesses we must NOT copy

**Correctness / robustness**
- **No real foreign-key / cross-entity relationships.** Entities are generated
  independently; `context` only resolves *within* a record. Despite `_id`
  columns and star-schema talk, referential integrity across tables isn't
  enforced at generation time — a serious gap for a tool selling "clean joins."
  → Our v1 ships flat fields (no FK problem yet); when we add `tables`/nested
  records (plan §"Schema file format", additive), generate parents first and
  reference real parent IDs in children.
- **No deterministic seeding.** No `faker.seed(...)`, so runs aren't
  reproducible. → Our plan makes this first-class: `fake.seed_instance(cfg.seed)`
  and determinism is the primary `test_generate.py` assertion.
- **Unvalidated LLM JSON.** The spec is `JSON.parse`'d and fed to `DataFactory`
  with only a light "spec repair" step — untrusted model output drives the whole
  pipeline. → Our plan hand-validates via `_validate_schema_shape` (the
  `pricing._validate_pricing_shape` idiom) before any generation; keep that gate
  strict for any Bedrock-produced schema too.
- **Fallback bug:** unrecognized business type falls back to
  `businessTypeInstructions["SaaS"]`, but no `"SaaS"` key exists → `undefined`.
- **Comment/code mismatch** in entity counts (comment says 5–200, code does
  10–100); the count heuristic ignores the requested row range.

**Security (mostly moot under Bedrock, noted for completeness)**
- 500 handler returns raw `error.message` to clients (info leakage). → Our
  `errors.py` taxonomy returns coded, sanitized errors.
- Unbounded `rowCount` / `timeRange` → resource-exhaustion risk. → Validate and
  bound `--rows` in the CLI.
- Hardcoded `"sk-1234"` master-key fallback — irrelevant once on the AWS
  credential chain.

**Maintainability**
- **Heavily hardcoded per-business-type prompts** (12 types: pricing tiers,
  formulas, required/forbidden fields) *plus* redundant reinforcement blocks
  restating the same constraints. Adding a type means editing 4 files. → Our
  domain shape comes from the **schema file**, not hardcoded prompt templates;
  keep it that way.
- **Pervasive `as any`**, including the dynamic `faker[namespace][method]()`
  dispatch — bypasses type safety on exactly the untrusted boundary that matters.
  → Our `generate.py` validates Faker provider names up front (`SchemaError`
  listing valid providers, fail-fast) before dispatch.
- Apparent **dead code** (`extractForeignKeyIds`, `findForeignKeyName` unused by
  `generate`); a hardcoded entity→FK map that duplicates its own `${name}_id`
  fallback and mixes singular/plural names.

---

## 6. Verdict & what to borrow

**Borrow:**
1. The **two-stage architecture** — LLM authors the spec, local Faker generates
   rows, aggressive caching, export never re-calls the model. (Already the spine
   of our plan.)
2. The **event-simulation model** for narrative-rich output (recurring / random
   / churn) — a strong *future* extension once flat-field `generate` ships, to
   move beyond independent rows toward believable time-series and lifecycle data.
3. The **facade + single-responsibility module** decomposition of `DataFactory`.

**Reject / fix:**
1. Multi-provider gateway, runtime probe, app-managed keys — **collapse to one
   boto3 Bedrock client** (the plan's `lx_bedrock.py`).
2. No referential integrity — **generate parents → reference real IDs** when we
   add nested tables.
3. No seeding — **deterministic seeds by default**.
4. Unvalidated LLM spec — **hand-validate the schema before generation**.
5. Over-hardcoded domain config — **keep domains in the schema file, not code**.

**Bottom line:** the Metabase project is strong confirmation that
[`structured-data-support.md`](./structured-data-support.md) is pointed the right
way. Under a Bedrock-only constraint our design is actually *simpler* than
theirs (no provider routing), and our plan already pre-empts their three biggest
execution gaps (seeding, schema validation, config-over-code). The one idea
worth scheduling as a later slice is their **event-simulation engine**.

---

## Sources

- Repo: <https://github.com/metabase/dataset-generator>
- Files reviewed: `lib/data-factory.ts`, `lib/spec-prompts.ts`,
  `lib/generators/entity-generator.ts`, `app/api/generate/route.ts`, README.
- Related notes:
  [`../research-notes/06-demo-data-kit.md`](../research-notes/06-demo-data-kit.md),
  this project's plan [`./structured-data-support.md`](./structured-data-support.md).
