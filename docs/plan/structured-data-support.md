# Plan: Structured Synthetic Data for pocsynth (extract → schema → generate → test, + presets & demo UI)

## Context

`pocsynth` today converts a PDF into a **synthetic rendered document** (HTML/Markdown) via
Amazon Bedrock, with an optional Comprehend PII audit. It does **not** produce structured
*data* — no field extraction, no schema, no programmatic synthetic-data generation, no
data validation. (The only JSON it emits is the machine-readable result *envelope*; the only
CSV is the PII audit — neither is document content.)

This plan adds a **four-stage structured-data pipeline** as four new CLI subcommands. Each
stage has a clean artifact contract with the next, and the pipeline splits cleanly into a
**paid half** (run once) and a **free half** (run unlimited):

| Stage | Consumes | Produces | Bedrock? |
|---|---|---|---|
| **extract** | a real source doc (PDF) | a **data sample** — records / field observations pulled from the doc (forced `toolConfig`) | yes (paid) |
| **schema** | an extract sample **or** a user-provided schema | a **generation-ready schema** + **documentation** + a **lint report** (recommendations; `--fix` to apply) | **infer**: yes (paid) · **lint**: no (free) |
| **generate** | a schema | synthetic **rows** | no (free, Faker) |
| **test** | rows + a schema | a **validation report** (do rows conform to types/enums/regex?) | no (free, offline) |

**The cost story:** `extract` + `schema --infer` are the paid steps, run **once** on one real
document. `generate` + `test` (and `schema --lint`) are **free and offline** — an unlimited,
no-AWS iterate loop. One-or-two paid calls → unlimited free, validated rows.

`convert` (the existing product) stays distinct: it renders a *synthetic document*; `extract`
pulls *structured records*. Different output, no overlap (ADR-0003).

### Decisions taken
1. **Pipeline shape** → four stages `extract → schema → generate → test` (ADR-0003).
2. **Extract engine** → **native Bedrock** via the existing `converse` path; langextract deferred
   (ADR-0001, [`../research-notes/08-langextract.md`](../research-notes/08-langextract.md)).
3. **Extract output** → structured **records**, not cleaned content (cleaned content is `convert`'s
   job). Two modes: **discovery** (generic tool, no schema yet) and **conform** (field-specific tool
   from a known schema). Forced `toolConfig` in both (ADR-0002).
4. **schema** → **three sources**: **infer** (paid, Bedrock, from an extract sample), **from-prompt**
   (paid, Bedrock, from a natural-language business description — the Metabase-style "AI" path,
   ADR-0008), and **lint** (free, offline, on a user schema). All three paid paths share the same forced
   `emit_schema` toolConfig (ADR-0002); only the *input* differs (sample vs. prompt). Mirrors the
   `convert`/`estimate` paid/offline split.
5. **schema auto-fix** → **off by default**; recommendations are reported, fixes applied only under
   explicit `--fix` (and the change list is always surfaced). Matches the repo's "surface warnings,
   don't silently adjust" posture (`estimate`).
6. **generate** → offline **[Faker](https://faker.readthedocs.io)** (core dep, pinned).
7. **test** → offline, **no new dependency** (hand validation reusing the schema model).
8. **Value distributions are first-class** (ADR-0004). A field may carry `weights` so `enum`/choice
   generation is non-uniform. `schema` infers them via a `--distribution` mode, default **auto**:
   - **infer** — derive weights from the real document's observed value frequencies (most faithful;
     needs frequency data from `extract`).
   - **synthetic** — the model invents plausible weights (no real frequencies needed / available).
   - **uniform** — no weights; equal probability (the format-only baseline).
   - **auto** (default) — use `infer` when `extract` captured frequencies, else fall back to
     `synthetic`; record which was chosen in the lint report + envelope.
   v1 synthetic data is then **distribution-representative for low-cardinality `enum` fields**; it does
   **not** model continuous distributions, inter-field correlations, or time series (see Risks +
   [`../research-notes/03-quality-and-representativeness.md`](../research-notes/03-quality-and-representativeness.md)).
9. **PII is audited and barred from leaking into synthetic output** (ADR-0005). The paid half handles
   real values, so: (a) `extract` runs the existing Comprehend PII audit on its sample by default
   (`--pii-audit/--no-pii-audit`, mirroring `convert`); (b) `schema --infer` **never** emits a
   real-value `enum`/`weights` for a field flagged as PII (by Comprehend, or whose inferred `faker`
   binding is a PII provider) — such fields fall back to a Faker provider regardless of cardinality,
   real values discarded, with the suppression logged in the lint report. Non-PII low-cardinality
   fields (state, plan tier) keep their real enums. Closes the "partially synthetic data is still PII"
   trap from [`../research-notes/04-compliance-privacy-bias.md`](../research-notes/04-compliance-privacy-bias.md).
10. **Pre-flight estimate covers the paid stages** (ADR-0007). `estimate --for convert|extract|schema`
    gives agents one cost-gate idiom across every Bedrock-spending command.
11. **Demo UI** (ADR-0009) — a thin local web app over the core verbs, Metabase-style: pick a bundled
    **preset** schema (instant, free) **or** describe a business in natural language
    (`schema --from-prompt`, paid, ADR-0008) → preview 10 rows (free `generate`) → download CSV/JSON
    full set (free). Presets are the `06-demo-data-kit.md` schemas made real, shipped in the repo. The
    UI calls the same functions and reads the same envelopes as the CLI — not a separate product surface.

### Research findings (recorded so future sessions don't re-research)
- **Native extraction / forced toolConfig** reuses `bedrock.py`: `converse(modelId, messages,
  system, inferenceConfig={maxTokens, temperature:0}, toolConfig=..., )` with
  `toolChoice={"tool": {"name": ...}}` forcing the tool; read the `toolUse` block's `input`; boto
  errors via `translate_aws_error(exc, service="bedrock")`; token totals off `response["usage"]`.
  The contract lives in the tool's `inputSchema`, not in a prompt (ADR-0002). **One Bedrock code
  path total** — `extract` and `schema --infer` both use it.
- **Faker** (MIT, Python 3.8+): `Faker(locale)`, `fake.seed_instance(n)` (instance-scoped, safe for
  concurrent use), providers `name/address/phone_number/ssn/company/date_time/credit_card/email/...`,
  custom providers via `BaseProvider` subclass + `fake.add_provider()`, `fake.regexify(pattern)` and
  `fake.random_element(elements=[...])` for regex/enum constraints. Fully offline, deterministic.
- **langextract** — evaluated, **deferred** ([`../research-notes/08-langextract.md`](../research-notes/08-langextract.md)).

---

## Dependencies (pyproject.toml)

Add **one** core runtime dep (`[project.dependencies]`), tightly pinned:
- `faker>=37,<38` — pin tight: Faker's locale data shifts across minors, which would break
  seeded golden-value tests.

**No other new *core* dependency.** `extract` / `schema --infer` / `schema --from-prompt` reuse
`boto3`/`bedrock.py` (present). `test` is hand-rolled validation over the schema model. Keep
**jsonschema** and **pydantic** OUT — hand-validate reusing the existing
`pricing.py::_validate_pricing_shape` + `PricingDataError` idiom (house style;
`src/pocsynth/pricing.py:80-95`).

**Optional extra `[ui]` (ADR-0009):** `fastapi`, `uvicorn` under `[project.optional-dependencies]` —
installed only via `pip install 'pocsynth[ui]'`, **never** core or skill deps. HTMX is a vendored/CDN
script (no node/npm/build). The demo UI is the *only* thing that needs these; the CLI, the offline
pipeline, and the `uv run --script` skill bundle never import them.

**Skill bundle sync (mandatory):** update the PEP 723 dependency header in
`scripts/generate-skill-script.py:45-54` to add **faker** (one line), in lockstep with pyproject —
the `skill-script-drift` CI job fails otherwise. Faker is pure-Python and light; confirm the
`uv run --script` single-file skill still resolves and bump `tests/unit/test_skill_script_perf.py`
if needed.

---

## Artifacts & schema format (dependency-free, hand-validated)

### The schema file (the spine — produced by `schema`, consumed by `generate`/`test`/conform-`extract`)

One JSON format. `generate` reads `faker`/`enum`/`regex`/`faker_args`; `test` reads
`type`/`enum`/`regex` (the constraints); `extract --schema` (conform mode) reads `name`/`type`/`enum`;
`schema` documentation reads `description`.

```json
{
  "schema": 1,
  "name": "patient_record",
  "description": "One row per patient intake record.",
  "fields": [
    {"name": "full_name", "type": "string", "faker": "name", "description": "patient full name"},
    {"name": "dob", "type": "date", "faker": "date_of_birth", "faker_args": {"minimum_age": 18}},
    {"name": "ssn", "type": "string", "faker": "ssn", "description": "US SSN"},
    {"name": "state", "type": "string", "enum": ["CA", "NY", "TX"],
     "weights": {"CA": 0.7, "NY": 0.2, "TX": 0.1}, "weights_source": "infer"},
    {"name": "mrn", "type": "string", "regex": "MRN-[0-9]{6}"}
  ]
}
```

`weights` (optional) is an enum→probability map (need not sum to 1; normalized at generation);
`weights_source` records how it was derived (`infer`|`synthetic`|`uniform`) for transparency.
`tables`/nested records are an additive future extension under schema version 1
(`"type": "array", "items": {...}`); v1 ships flat fields only. Referential integrity for nested
tables is explicitly out of scope for v1 (see Risks).

#### `type` is a closed set with explicit semantics (ADR-0006)

`type` is **both** a generation hint and a validation rule, so it's a fixed enum — not free-form.
`_validate_schema_shape` rejects anything else.

| `type` | CSV cell | JSON value | validates (coerce-then-check) |
|---|---|---|---|
| `string` | text | string | always (any text) |
| `integer` | `42` | `42` (int) | parses as int, no fractional part |
| `number` | `3.14` | `3.14` (float) | parses as float |
| `boolean` | `true`/`false` | `true` (bool) | in {true,false,1,0,"true","false"} |
| `date` | `1990-05-01` | `"1990-05-01"` (string) | ISO-8601 date |
| `datetime` | `1990-05-01T09:30:00Z` | string | ISO-8601 datetime |

JSON has no native date/datetime → dates are **ISO-8601 strings in both formats** (canonical).
**Null convention:** an empty CSV cell = `null`; in JSON, `null`. v1 treats all fields as nullable
(no `required`), so `null`/empty always validates; a future `required: true` is additive.

### The extract sample (produced by `extract`, consumed by `schema --infer`)

Discovery mode emits **grouped, multi-value field observations with value counts** (not flat
one-row-per-observation): each field carries example values *and how often each was seen*, so
`schema --infer` can see both **cardinality** (enum vs. regex vs. free text) and **frequency** (the
weights for distribution-`infer` mode, ADR-0004). Per-page observations are **merged by field name**
(distinct values + summed counts, distinct values capped at `MAX_EXAMPLES_PER_FIELD = 20`), not
flat-appended — so "one field seen on 10 pages" and "10 distinct values of one field" are no longer
conflated, and value frequencies survive the merge.

```json
{
  "schema": 1,
  "source": "intake_form.pdf",
  "fields": [
    {"name": "full_name", "type_hint": "string", "pii": true,  "value_counts": {"Jane Doe": 1, "John Roe": 1, "Mary Poe": 1}},
    {"name": "state",     "type_hint": "string", "pii": false, "value_counts": {"CA": 7, "NY": 2, "TX": 1}},
    {"name": "mrn",       "type_hint": "string", "pii": true,  "value_counts": {"MRN-481923": 1, "MRN-002214": 1}}
  ]
}
```

`state.value_counts` → enum **and** weights `{CA:0.7, NY:0.2, TX:0.1}`; `mrn` is PII → regex with **no
real-value enum** (ADR-0005); `full_name` is PII → Faker provider, real values discarded. The `pii`
flag per field is set by `extract`'s audit and is what `schema --infer` uses to bar real-value enums.
Conform mode (a schema was supplied) emits records keyed by the schema's field names instead (the
per-record extract from ADR-0002).

### The validation report (produced by `test`)

```json
{
  "valid": false,
  "rows_checked": 1000,
  "violations": [
    {"row": 42, "field": "mrn", "rule": "regex", "expected": "MRN-[0-9]{6}", "got": "X-12"}
  ],
  "summary": {"by_field": {"mrn": 3}, "by_rule": {"regex": 3}}
}
```

---

## New modules & signatures

### `src/pocsynth/schema.py` (shared model — offline, no AWS)
- `load_schema(path) -> dict` — read + `json.loads` + `_validate_schema_shape`; raises `SchemaError`.
- `_validate_schema_shape(data) -> None` — require `schema == 1`, non-empty `fields`, each field has
  `name` + a `type` in `FIELD_TYPES` (the closed set, ADR-0006); mirrors `pricing._validate_pricing_shape`.
- `FIELD_TYPES: frozenset = {"string","integer","number","boolean","date","datetime"}`.
- `field_names(schema) -> list[str]` — ordered column list (CSV headers; used by generate/test).
- `serialize(value, type, fmt) -> Any` — canonical serialization shared by `generate` + `extract`:
  dates/datetimes → ISO-8601 strings in **both** formats; in `csv` everything → `str()` (None → ""),
  in `json` integer/number/boolean stay native, None → `null`. **One writer for both commands.**
- `coerce_and_check(cell, type) -> tuple[bool, Any]` — the **single** validation primitive used by
  `test` for both CSV and JSON: empty/None → `(True, None)` (nullable, ADR-0006); else try to parse the
  cell to `type` per the table (ISO date/datetime, int with no fractional part, float, bool set) →
  `(ok, parsed)`. Coercion is a no-op for already-typed JSON values, so CSV and JSON take the **same
  path** — the round-trip can't flake on format.
- `schema_to_toolspec(schema) -> dict` — **conform-mode** extract tool (`extract_records`):
  `inputSchema.json` = object with a `records` array of objects, one property per field (`type` →
  JSON-schema type; `enum` carried; required = all field names). Pure transform over valid input.
- `discovery_toolspec() -> dict` — **discovery-mode** extract tool (`observe_fields`): generic
  `fields` array of `{name, type_hint, value_counts: {value: count}}` — the model reports the distinct
  values seen per field *and how many times each occurred on the page*. Schema-independent.
- `merge_observations(per_page: list[dict], cap=MAX_EXAMPLES_PER_FIELD) -> dict` — merge per-page
  discovery results by field name: union distinct values (capped), **sum `value_counts`**, so both
  cardinality and frequency survive across pages rather than being flat-appended.
- `weights_from_counts(value_counts: dict) -> dict` — normalize summed counts to a probability map
  (the `infer` distribution path; offline, pure).
- `schema_infer_toolspec() -> dict` — **schema-infer** tool (`emit_schema`): `inputSchema.json`
  describes a generation-ready schema (fields with `name`/`type`/`faker`/`enum`/`regex`/`description`).
  Forces Bedrock to return a well-formed schema (ADR-0002 philosophy, applied to the `schema` step).
- `lint_schema(schema) -> list[dict]` — offline checks → list of `{field, issue, severity,
  recommendation, autofixable: bool, fixed?: <value>}`. Examples: unknown `faker` provider (suggest
  nearest valid), `enum` + `faker` both set (ambiguous), `regex` that doesn't compile, `type`/`faker`
  mismatch, missing `description`, and **PII-provider + real `enum`/`weights`** (a `name`/`ssn`/`email`/
  … binding alongside a literal-value enum) → high-severity, autofixable by dropping the enum/weights
  (ADR-0005). This is the offline backstop that catches leaked real values even in `schema --from-schema`
  lint mode, where no `extract` `pii` flag is present.
- `PII_FAKER_PROVIDERS: frozenset` — the Faker provider names treated as identifying
  (`name/first_name/last_name/ssn/email/phone_number/address/credit_card_number/...`), shared by the
  infer guard and the lint rule.
- `apply_fixes(schema, lint) -> tuple[dict, list[dict]]` — return a new schema with autofixable
  issues applied + the list of changes made. Only called under `--fix`.
- `document_schema(schema) -> str` — render a Markdown data dictionary (one table: field, type,
  source/faker, constraints, description).

### `src/pocsynth/presets/` (bundled preset schemas — free, offline)
- A package-data directory of ready-made schema JSON files (the `06-demo-data-kit.md` domains: B2B
  SaaS, e-commerce, healthcare-lite, …), each a valid v1 schema with `faker`/`enum`/`weights` already
  set. Loaded via `importlib.resources` (same idiom as `pricing.json`, `pricing.py:53`).
- `list_presets() -> list[dict]` (name + description) and `load_preset(name) -> dict` (→ `load_schema`
  validation). Surfaced by a `pocsynth presets` command and the UI's preset dropdown. Zero AWS.

### `src/pocsynth/extract.py` (Bedrock — paid)
- `@dataclass ExtractConfig` — `pdf_url`, `schema: dict | None = None` (None ⇒ discovery mode),
  `model_key="sonnet"`, `export_format="json"` (`json`|`csv`|`jsonl`), `num_pages`, `max_tokens=8000`,
  `pii_audit=True`, `region`, `profile`, `output_dir`, `bedrock_client=None`,
  `comprehend_client=None` (test injection for both clients).
- `run_extraction(cfg, on_event=None) -> dict` — PDF bytes via `pdf.get_pdf_file`, open with `fitz`,
  iterate pages (page-iteration shape from `core.run_conversion`). Tool = `schema_to_toolspec(cfg.schema)`
  if a schema is given, else `discovery_toolspec()`. Per page: build prompt
  (`prompts.build_extract_prompt(...)` + page `get_text("text")` via the `str.replace("{page_text}", …)`
  brace-safe trick, `bedrock.py:131`), `converse` with the forced `toolConfig`; read `toolUse.input`;
  accumulate `response["usage"]`. **Conform mode** flat-appends `records` across pages; **discovery
  mode** collects each page's `fields` then `merge_observations(...)` to union distinct values and sum
  `value_counts` by field name (cardinality + frequency preserving). Write the sample to `output_dir`.
  No-`toolUse` page → `page_failures` (mirrors `process_page` no-text guard); all pages empty →
  `PartialError`. Boto errors → `translate_aws_error`. **PII audit (ADR-0005):** when `cfg.pii_audit`,
  run `scan_for_pii` (reused verbatim) over the full extracted text → the same audit CSV as `convert`
  + `entities_found`. For the **per-field `pii` flag** (what bars real-value enums downstream), scan
  **each field's own values** (`_pii_fields(fields_or_records, comprehend) -> set[str]`: a field is
  PII if Comprehend detects any entity within its concatenated values) and stamp `pii: bool` onto each
  field/record key in the written sample. Same shared session builds both bedrock + comprehend clients
  (mirror `core.run_conversion`); tests inject both via `bedrock_client`/`comprehend_client`. Conform
  records written as CSV/JSONL go through `schema.serialize(...)` (ADR-0006) so an extracted sample is
  itself a valid `test` input against the same schema.

### `src/pocsynth/schemagen.py` (the `schema` command — hybrid)
- `@dataclass SchemaConfig` — `sample_path: str | None`, `in_schema_path: str | None`,
  `prompt: str | None` (ADR-0008), `mode` derived (`infer` if a sample, `from_prompt` if a prompt,
  `lint` if a schema), `distribution="auto"` (`auto`|`infer`|`synthetic`|`uniform`, ADR-0004),
  `model_key="sonnet"`, `fix=False`, `output_dir`, `region`, `profile`, `max_tokens=8000`,
  `bedrock_client=None`.
- `run_schema(cfg, on_event=None) -> dict`:
  - **from-prompt mode** (NL business description → schema, paid, ADR-0008): `converse` once with the
    same forced `schema_infer_toolspec()` tool and `build_schema_from_prompt_prompt(cfg.prompt)`; read
    `toolUse.input` as the draft schema, `_validate_schema_shape`, `lint_schema`. No `value_counts`
    exist, so distribution is `synthetic`/`uniform` only (`infer`/`auto`→`synthetic`, noted). The PII
    guard still runs on the *inferred* `faker` bindings (no `pii` flags from a prompt, so the
    provider-name heuristic + the offline `lint_schema` rule are the guard). Write schema + doc + lint.
  - **infer mode** (sample → schema, paid): load the extract sample, `converse` once with
    `toolConfig=schema_infer_toolspec()` forced (the tool's `inputSchema` includes optional per-field
    `weights`), read `toolUse.input` as the draft schema, run `_validate_schema_shape`, then
    `lint_schema`. **PII guard (ADR-0005), applied before distribution resolution:** for any field
    marked `pii: true` in the sample (or whose inferred `faker` is a PII provider —
    `name/ssn/email/phone_number/address/credit_card/...`), strip any `enum`/`weights`, ensure a Faker
    provider binding (regex kept if format-only and non-identifying), and add a lint note
    (`"<field> is PII → real-value enum suppressed, using faker.<x>; real values discarded"`). Then
    resolve `weights` per the distribution mode (PII fields already have none):
    - `infer` → `weights_from_counts(field.value_counts)` from the sample (offline, exact);
      `weights_source="infer"`.
    - `synthetic` → keep the model's invented `weights`; `weights_source="synthetic"`.
    - `uniform` → drop `weights`; `weights_source="uniform"`.
    - `auto` → `infer` for fields whose sample carries `value_counts`, else `synthetic`; record which
      per field. Emit a lint note when a field falls back to `synthetic` (no real frequencies).
    Write schema JSON + `document_schema` Markdown + lint report. (`apply_fixes` if `--fix`.)
  - **lint mode** (schema → report, free, offline): `load_schema`, `lint_schema`, write the report +
    `document_schema` Markdown; if `--fix`, write a fixed schema and list changes. **Never** calls
    `make_session` (offline like `estimate`). (`--distribution` is ignored in lint mode — no sample to
    infer from — and a warning is surfaced if the user passes it.)
  - Exactly one of `sample_path`/`in_schema_path`/`prompt` required → else `SchemaError`.

### `src/pocsynth/generate.py` (Faker — free, offline)
- `@dataclass GenerateConfig` — `schema: dict`, `rows=100`, `export_format="csv"` (`csv`|`json`),
  `seed: int | None = None`, `locale="en_US"`, `output_dir=None`.
- `run_generation(cfg, on_event=None) -> dict` — `Faker(cfg.locale)`; `fake.seed_instance(cfg.seed)`
  when set; resolve a generator callable per field (validate Faker provider names up front →
  `SchemaError` listing valid providers, fail-fast like `_bedrock_entry`); apply constraints — `enum`
  **with** `weights` → `fake.random_element(OrderedDict(normalized weights))`, `enum` without weights →
  uniform `fake.random_element`, `regex` → `fake.regexify`; serialize every value via
  `schema.serialize(value, field.type, cfg.export_format)` (canonical, ADR-0006) before writing
  CSV/JSON; envelope `cost: null` (no AWS touched). Mirror `estimate`'s offline path: never call
  `make_session`.
- `_resolve_field_generators(schema, fake) -> list[Callable[[], Any]]`.

### `src/pocsynth/validate.py` (the `test` command — free, offline)
- `@dataclass ValidateConfig` — `rows_path: str`, `schema: dict`, `output_dir=None`.
- `run_validation(cfg, on_event=None) -> dict` — read rows (CSV/JSON), for each row + field run
  `schema.coerce_and_check(cell, field.type)` (the single coercion path, ADR-0006 — identical for CSV
  and JSON), then on the coerced value check `enum` membership and `regex` match; collect violations;
  return the report (`valid`, `rows_checked`, `violations[]` with `{row, field, rule:
  "type"|"enum"|"regex", expected, got}`, `summary`). No AWS. Pure function over loaded data — the
  cleanest tests in the repo alongside generate. Format inferred from extension or an explicit
  `--in-format`.

### `src/pocsynth/pricing.py` (additive — pre-flight estimate for the paid stages, ADR-0007)
The cost-safety idiom is "`estimate` first, gate on a threshold, then spend." Extend it to the two
paid stages so agents keep one pre-flight contract (the existing `convert` machinery is reused):
- `EXTRACT_OUTPUT_TOKENS_PER_INPUT_TOKEN_RATIO` — extract's structured-records output is smaller than
  convert's HTML/MD; a separate, lower ratio constant (calibrated like the existing convert ratio,
  `pricing.py:38`). New `--for` target picks which ratio `_estimate_output_tokens` uses.
- `estimate_extract_cost(pdf_path, model_key, pricing, *, pages, pii_audit, region) -> dict` — mirrors
  `estimate_convert_cost` (same per-page image+text input heuristic, same `_finalize_envelope`,
  stale/region warnings), swapping the output ratio. PII-audit cost included like convert when on.
- `estimate_schema_infer_cost(sample_path, model_key, pricing, *, region) -> dict` — **offline, no
  PDF**: token-count the sample file (`chars/DEFAULT_CHARS_PER_TOKEN` for input; small fixed output
  budget for a schema) → `estimate_bedrock_cost`. One small call; the envelope notes "schema-infer is
  typically pennies."
- Both return the same envelope shape as `estimate_convert_cost` (`confidence: "low"`, heuristic
  assumptions recorded) so the agent reads `total_cost_usd` identically across all paid commands.

### `src/pocsynth/errors.py` (additive)
- `class SchemaError(DocSynthError): code="SCHEMA_INVALID"; exit_code=2` (invalid-args class, like
  `PricingDataError`).
- `class ExtractionError(DocSynthError): code="EXTRACTION_FAILED"; exit_code=5; retryable=True`.
- `class DataInvalidError(DocSynthError): code="DATA_INVALID"; exit_code=7` — raised by `test` when
  rows violate the schema, so CI/agents can gate on the exit code (mirrors `doctor` mapping a failed
  result to a coded error). The report is still emitted in the envelope first.

### `src/pocsynth/cli.py` (four new commands + extended `estimate`; mirror `convert`/`estimate`)
```
pocsynth extract  <PDF> [--schema PATH] [--format json|csv|jsonl] [--model …] [--pages N]
                        [--max-tokens N] [--pii-audit/--no-pii-audit] [-o DIR]   # no --schema ⇒ discovery
                                                                                  # --pii-audit default on
pocsynth schema   (--from-sample PATH | --from-prompt TEXT | --from-schema PATH) [--model …] [--fix]
                  [--distribution auto|infer|synthetic|uniform] [-o DIR]   # default auto
pocsynth generate (--schema PATH | --preset NAME) [--rows N] [--format csv|json] [--seed N]
                  [--locale TEXT] [-o DIR]
pocsynth test     --rows PATH --schema PATH [--in-format csv|json] [-o DIR]   # default: infer from ext
pocsynth presets  # list bundled preset schemas (free, offline)

# extended (existing command gains a target, default convert — back-compatible):
pocsynth estimate <PDF|SAMPLE> [--for convert|extract|schema] [--model …] [--pages N] [--pii-audit/…]
```
- Each: build the config dataclass, define `on_event` for `--stream`, wrap in `_wrap(ctx, name, fn)`.
- `generate`, `test`, `schema --from-schema` (lint), **and every `estimate` target** must short-circuit
  any AWS/session path (offline like `estimate` today, `cli.py:300+`). `estimate --for schema` takes a
  sample path, not a PDF, and is offline token-counting.
- `estimate --for` dispatches to `estimate_extract_cost` / `estimate_schema_infer_cost` /
  `estimate_convert_cost` (ADR-0007); the envelope shape is identical so the skill's `total_cost_usd`
  threshold gate is unchanged.
- Add human-mode branches to `_emit_human` (mirror `convert`/`estimate`, `cli.py:159-192`): extract →
  records/pages/tokens/cost; schema → fields/lint-issue count/distribution mode/(infer cost or
  "offline"); generate → rows + "cost: $0.00 (offline Faker)"; test → valid? + violation count.
- (`--visualize` deferred with langextract — ADR-0001; not in v1.)

---

## Result envelopes (additive, envelope schema stays version 1)

- **extract** `result`: `input{path, mode: "discovery"|"conform", schema?}`, `output{dir, sample_path,
  format, records_extracted, pages_processed, pages_attempted, wall_time_seconds,
  bedrock_usage{input_tokens, output_tokens}, combined_path}`, `pii_audit{enabled, path,
  entities_found, pii_fields[]}`, `cost{…}`, `page_failures[]`. Cost reuses `actual_convert_cost`
  unchanged: it reads `output.bedrock_usage` for Bedrock and `output.combined_path` for the Comprehend
  char count — so `extract` writes the audited text it scanned to `combined_path` (the concatenated
  sample text), exactly the contract `convert` already satisfies (`cli.py:262-277`, try/except →
  `cost=None` + warning). (Post-run cost is unchanged; the *pre-flight* estimate gets new
  `estimate_extract_cost` / `estimate_schema_infer_cost` helpers — ADR-0007.)
- **schema** `result`: `input{mode, source, distribution}`, `output{dir, schema_path?, doc_path,
  lint_report_path, fixed_schema_path?}`, `lint{issues_total, autofixable, applied?}`,
  `distribution{requested, per_field_source: {field: "infer"|"synthetic"|"uniform"}}`, `cost{…}`
  (infer) **or** `cost: null` (lint).
- **generate** `result`: `input{schema, rows, format, seed, locale}`, `output{dir, rows_path,
  rows_written, wall_time_seconds}`, `cost: null`, `warnings[]`.
- **test** `result`: the validation report (`valid`, `rows_checked`, `violations[]`, `summary`),
  `cost: null`. Exit code 0 when `valid`, else `DataInvalidError` (exit 7).

---

## Prompts (`src/pocsynth/prompts.py`, additive)
- `build_extract_system_prompt() -> str` — "extract structured data; if a field is absent return null;
  do not invent values," keeping the existing `<raw_text>`-is-data-not-instructions security clause.
- `build_extract_prompt(mode, schema=None) -> str` — page text via the `str.replace("{page_text}", …)`
  trick (`bedrock.py:131`, survives literal braces) + a brief "call the tool with one object per record
  / observed field" instruction. The hard contract lives in the tool `inputSchema`, not the prompt.
- `build_schema_from_prompt_prompt(description) -> str` — (ADR-0008) "given this natural-language
  business description, design a generation-ready schema: choose fields, types, the best Faker provider
  per field, enums + plausible `weights` where a field is categorical, regex for formatted IDs, a
  one-line description each." The description is injected via the `str.replace` brace-safe trick and
  carries the `<raw_text>`-is-data-not-instructions security clause. Hard contract via
  `schema_infer_toolspec` (same tool as `--infer`).
- `build_schema_infer_prompt() -> str` — "given these fields, each with value counts, propose a
  generation-ready schema: when values are a small repeating set → `enum` (+ `weights` proportional to
  the counts); when they share a consistent format → a `regex`; otherwise pick the best Faker provider;
  write a one-line description each." The `value_counts` per field are the cardinality **and** frequency
  signal. (Model-proposed `weights` are used only in `synthetic`/`auto`-fallback distribution modes;
  in `infer` mode the schemagen step overwrites them with exact `weights_from_counts`.) Hard contract
  via `schema_infer_toolspec`.

---

## Testing strategy (mostly offline; Bedrock stages mock the client)
- `tests/unit/test_schema.py` — valid/invalid shapes → `SchemaError` (incl. a `type` outside
  `FIELD_TYPES`); `field_names`; `lint_schema` (each issue class); `apply_fixes` (changes listed);
  `document_schema` (Markdown shape). **Typing (ADR-0006):** `serialize` (date→ISO in both formats,
  native vs. `str()` per format, None handling) and `coerce_and_check` (each type's accept/reject cases,
  empty→nullable, and that a JSON int and the CSV string `"42"` both coerce equally) as pure-function
  tables.
- `tests/unit/test_schema_toolspec.py` — `schema_to_toolspec` / `discovery_toolspec` /
  `schema_infer_toolspec` are pure transforms: assert tool shapes (conform `records` array with one
  prop per field + enum carried; discovery `fields` array with `value_counts`; infer `emit_schema`
  shape incl. optional `weights`). Test `merge_observations` — distinct values unioned + `value_counts`
  summed by field name, capped, cardinality + frequency preserved across pages. Test
  `weights_from_counts` — counts normalize to a probability map.
- `tests/unit/test_generate.py` — **no mocks**: `seed=42, locale="en_US"` → determinism (two runs
  identical), row count, regex match, enum membership; **weighted enum** → over a large seeded sample
  the observed frequencies track `weights` within tolerance (and uniform when no weights); ≤1–2 literal
  golden values (Faker drift) + pin.
- `tests/unit/test_validate.py` — **no mocks**: hand-built rows vs. schema → conforming set is `valid`,
  seeded violations are caught with correct row/field/rule; `DataInvalidError` exit mapping.
- `tests/unit/test_estimate_paid_stages.py` — **no AWS** (extend the existing pure-pricing tests):
  `estimate_extract_cost` over a `fitz`-built PDF → envelope shape matches `estimate_convert_cost`,
  uses the extract output ratio (lower total than convert for the same input), PII cost included when
  on; `estimate_schema_infer_cost` over a sample file → small offline token-count estimate, no PDF
  opened. Both surface stale-pricing/region warnings via the shared `_finalize_envelope`.
- `tests/unit/test_run_extraction.py` — real PDF via `fitz` (`_make_pdf`, `test_run_conversion.py:25`),
  `MagicMock` boto client (like `_bedrock_stub`, `test_run_conversion.py:37`) through
  `ExtractConfig.bedrock_client` returning a canned `toolUse` payload
  (`output.message.content[].toolUse.input.{records|fields}`); assert conform flat-appends `records`
  and discovery merges `fields` examples by name across pages, output + envelope shape, token
  accounting, discovery vs. conform tool selection, no-`toolUse` → `page_failures`, all-empty →
  `PartialError`, boto errors → `translate_aws_error`. **PII (ADR-0005):** inject a mock comprehend
  client; assert the audit CSV + `entities_found` + per-field `pii` flags are written, that
  `combined_path` holds the scanned text (so cost wiring works), and that `--no-pii-audit` skips it.
- `tests/unit/test_run_schema.py` — infer mode with a mock `converse` returning a canned `emit_schema`
  `toolUse`; lint mode fully offline (no client). Assert schema validated, lint report + doc written,
  `--fix` applies and lists changes. **Distribution modes** (offline, no extra calls): `infer` →
  weights equal `weights_from_counts` of the sample; `synthetic` → model's weights kept; `uniform` →
  weights dropped; `auto` → per-field infer-or-synthetic with the chosen `weights_source` recorded and
  a lint note on synthetic fallback. **PII guard (ADR-0005):** a sample field marked `pii: true` (or a
  model-inferred PII `faker` binding) → no real-value `enum`/`weights` in the output schema, a Faker
  binding instead, real values absent, lint note present. `lint_schema` flags a user schema that pairs
  a PII provider with a literal `enum`, and `--fix` drops it.
- Extend `tests/unit/test_skill_script.py` + `test_skill_script_perf.py` for the four new commands and
  the larger bundle.
- **Round-trip property test** (the pipeline's keystone): for a schema exercising **every** `FIELD_TYPE`,
  `generate` rows then `test` them against the same schema → always `valid`, **in both `csv` and `json`**
  (ADR-0006 — the shared serialize/coerce path means format must not matter). Pure offline; proves
  generate and test agree across types and formats.

---

## Skill / docs impact
- **Regenerate** the bundle after any `src/` change: `uv run python scripts/generate-skill-script.py`.
- Update PEP 723 header deps (`scripts/generate-skill-script.py:45-54`) — add faker (one line).
- `skills/pocsynth/RECIPES.md` — recipes in the existing "When to use / Steps / Anti-patterns" format:
  **Generate a synthetic dataset** (free), **Validate generated data** (free), **Document/lint a
  schema** (free, offline), **Extract a sample from a real PDF** (paid), **Full pipeline** (the cost
  saver: `extract` → `schema --infer` once, paid → `generate --rows 1000` → `test`, free). Anti-pattern:
  looping paid `convert`/`extract` over thousands of docs when `generate` is free.
- `skills/pocsynth/SKILL.md` — add the four subcommands + cost guidance + the PII posture (ADR-0005):
  extract audits its sample by default; real values never become enums for PII fields; the generated
  dataset is safe to share, the extract sample/audit CSV is not. **Cost gate (ADR-0007):** extend the
  existing "estimate first, gate at $0.10, then spend" rule to the paid stages — `estimate --for extract`
  before `extract`, `estimate --for schema` before `schema --from-sample` (note it's typically pennies);
  generate/test/schema-lint are free and need no gate. Add the new exit code 7 (`DATA_INVALID`) to the
  exit-code table: `test` returns it when rows violate the schema — an agent should surface the report,
  not retry.
- `README.md` — add a "Structured data pipeline" section; `CHANGELOG.md` — note the four commands +
  Faker dep.

---

## Demo UI (`src/pocsynth/ui/`, optional `[ui]` extra — ADR-0009)

A thin **FastAPI + HTMX** web app over the core verbs, inspired by Metabase's AI Data Generator: a
fill-in-the-blank sentence with inline dropdown **pills** + a submit arrow, a generated-data preview,
and download buttons. **Backend is Python calling the core functions in-process** — same envelopes as
the CLI, no subprocess, no second serialization. HTMX (one `<script>`, no node/npm/build step) keeps
it in the repo's single-language story.

### Dependency boundary
- New optional extra `pocsynth[ui]` → `fastapi`, `uvicorn` (HTMX is a CDN/vendored script, no build).
  **Never** core or skill deps — the `uv run --script` bundle and `pricing`/`generate` offline paths
  stay untouched. `pocsynth ui` (a Typer command) launches `uvicorn`; if the extra isn't installed it
  raises a `SchemaError`-style hint to `pip install 'pocsynth[ui]'`.

### The sentence builder (maps Metabase's pills to what our core actually supports)
> "I want to generate a **[100]** row dataset for a **[B2B SaaS ▾]** business, output as **[CSV ▾]**,
> with seed **[42]**."

- **Row count** pill → `generate --rows`.
- **Business type** pill → either a **preset** (`load_preset`, instant/free) **or** "✏️ describe my
  own…" which reveals a text box → `schema --from-prompt` (paid, ADR-0008).
- **Format** pill → `csv`|`json`. **Seed** pill → deterministic `--seed`.
- *Deliberately omitted from v1* (Metabase has them, our core doesn't yet): growth / variation /
  granularity / year — these are **time-series** controls and v1 has no temporal/distribution-over-time
  model (Risks, ADR-0004 scope). The pills are listed in a "coming soon" affordance, not faked.

### Flow (mirrors Metabase's 3 steps)
1. Pick pills (preset) or describe a business. Preview button.
2. **Preview** = `schema` (preset load = free; from-prompt = one paid call, **behind the explicit
   button + the ADR-0007 cost gate**, never on keystroke) → `generate --rows 10` (free) → render the
   inferred **schema** (the data dictionary from `document_schema`) + a **10-row table**.
3. **Download** = `generate --rows <N>` at the chosen format → file. Free, instant, reuses the
   previewed schema (no second model call — same "spec cached, export is free" property as Metabase).

### Endpoints (all call core functions directly)
- `GET /` → the sentence-builder page. `GET /presets` → pills (`list_presets`).
- `POST /preview` → `{schema, rows[10], cost}` (HTMX swaps the preview pane). `POST /download` →
  streamed CSV/JSON. `GET /healthz`.
- A from-prompt preview surfaces the estimated/actual **cost** in the UI before/after the call,
  honoring the same $0.10-gate guidance as the skill.

### Out of scope for the demo
Auth, multi-user, persistence, the Metabase "launch & explore" handoff, time-series controls, and
pixel-matching ShadCN styling. It's a local demo of the free pipeline, not a hosted product.

---

## Phasing (tracer-bullet first; ship the free, offline core before the paid stages)
1. **Slice 1 — the offline triad + presets (zero AWS).** `schema.py` (model + validate + `field_names`
   + `lint` incl. the ADR-0005 PII-provider+enum rule + `PII_FAKER_PROVIDERS` + `apply_fixes` +
   `document_schema` + `weights_from_counts`), `generate.py` (weighted enums), `validate.py`,
   `presets/` (+ `list_presets`/`load_preset`), plus `schema --from-schema` (lint mode). CLI:
   `generate` (+ `--preset`), `test`, `schema` (lint only), `presets`. `SchemaError`, `DataInvalidError`.
   Pure offline tests incl. the generate→test round-trip. Ships the free headline value + validation
   loop + presets + the offline PII backstop first, lowest risk. Recipes; regenerate skill.
2. **Slice 2 — the paid stages + PII guard.** `extract.py` (discovery + conform, forced `toolConfig`,
   Comprehend PII audit reusing `scan_for_pii`, per-field `pii` flagging), `schemagen.py` infer **and
   from-prompt** modes (ADR-0008) with the ADR-0005 PII guard, the toolspec builders, `schema
   --from-sample`/`--from-prompt`, `extract` CLI (`--pii-audit`), `ExtractionError`, post-run cost via
   `actual_convert_cost`, **pre-flight estimate via `estimate --for extract|schema` (ADR-0007)**.
   Mock-based tests (canned `toolUse` + mock comprehend) + pure-pricing estimate tests. The
   `lint_schema` PII rule ships in Slice 1 (offline) so the backstop exists before any real values flow.
3. **Slice 3 — full-pipeline recipe + docs + human-mode branches + perf-budget bump.**
4. **Slice 4 — demo UI (`[ui]` extra, ADR-0009).** FastAPI + HTMX sentence-builder over the core:
   presets (free) + from-prompt (paid, cost-gated), 10-row preview, CSV/JSON download. `pocsynth ui`
   launcher. Depends on slices 1–2. Optional extra — never touches core/skill deps. Smoke tests via
   FastAPI `TestClient` (preset preview fully offline; from-prompt mocks the bedrock client).

---

## Verification
- `uv run pytest tests/unit` — all green (offline; no AWS).
- Offline generate→test round-trip:
  `uv run pocsynth generate --schema example_schema.json --rows 5 --seed 42 --format csv -o /tmp/out`
  → deterministic CSV (rerun byte-identical); then
  `uv run pocsynth test --rows /tmp/out/rows.csv --schema example_schema.json` → `valid: true`, exit 0.
- Offline schema lint: `uv run pocsynth schema --from-schema messy.json --fix -o /tmp/out` → lint
  report + fixed schema + change list, no AWS.
- Pre-flight estimate (offline, no AWS): `uv run pocsynth --json estimate sample.pdf --for extract`
  → `total_cost_usd` (lower than `--for convert` for the same input); `… estimate /tmp/out/sample.json
  --for schema` → small offline estimate, no PDF opened.
- Extract (real AWS, manual): `uv run pocsynth --json extract sample.pdf -o /tmp/out` (discovery) and
  `… extract sample.pdf --schema example_schema.json` (conform) → sample + `result.cost.total_cost_usd`.
- Schema infer (real AWS, manual): `uv run pocsynth --json schema --from-sample /tmp/out/sample.json
  -o /tmp/out` → generation-ready schema + doc + lint report + cost.
- PII guard (ADR-0005): extract a PDF with real PII → assert the audit CSV exists and the inferred
  schema contains **no** real PII values (PII fields bound to Faker providers, lint notes present);
  grep the schema + generated rows for a known real value → absent.
- `uv run ruff check .` clean.
- `uv run python scripts/generate-skill-script.py` then `uv run pytest tests/unit/test_skill_script.py`
  — bundle regenerated, drift passes, `./skills/pocsynth/pocsynth.py --json generate …` runs.
- Full-pipeline proof: `extract` + `schema --infer` one PDF (paid, once) → `generate --rows 1000` →
  `test` (both `$0.00`, `valid: true`).

## Risks
- **LLM output reliability** — handled structurally by forced `toolConfig` (ADR-0002) in both `extract`
  and `schema --infer`: the model must call the tool, so output conforms to `inputSchema` rather than
  being free-text JSON we parse/repair. Residual risk is a response with **no `toolUse` block**
  (guardrail / empty page) → `page_failures`, all-empty → `PartialError`.
- **schema-infer quality** — the model may pick a wrong Faker provider or miss an enum/regex. Mitigated
  by always running `lint_schema` on the inferred schema and by the human reviewing the doc + lint
  report before `generate`. The schema is an editable artifact, not a black box.
- **Per-page records → sample assembly** — pages yield 0..N records; flat-append for v1; keep
  `pages_processed`/`pages_attempted` honest (mirrors `core.run_conversion`).
- **Distribution fidelity is partial (ADR-0004)** — v1 preserves real-world frequencies only for
  low-cardinality `enum` fields (via `weights`). It does **not** model continuous numeric
  distributions, inter-field correlations, or time series — so synthetic data is *distribution-
  representative for categoricals*, not training-grade. Frequencies are also only as good as the
  page sample size (small docs → noisy weights); `infer` weights from few observations are flagged in
  the lint report. See [`../research-notes/03-quality-and-representativeness.md`](../research-notes/03-quality-and-representativeness.md).
- **Faker version drift** breaking seeded golden tests — tight pin + structural assertions.
- **Referential integrity** — Faker has no cross-row/foreign-key awareness; flat fields only in v1, so
  no FK problem yet. When nested `tables` arrive, parents-then-reference-real-IDs lives in *our*
  generator, not Faker (see [`../research-notes/07-faker.md`](../research-notes/07-faker.md),
  [`./review-metabase-dataset-generator.md`](./review-metabase-dataset-generator.md)).
- **PII handling in the paid half (ADR-0005)** — `extract` writes real values to disk (sample +
  audit CSV); these are **not** safe to share even though the final generated dataset is. The PII
  guard depends on Comprehend detection (en-only, entity-type coverage gaps) + the `faker`-provider
  heuristic; a real value in a field Comprehend misses *could* survive as an enum. Mitigations: the
  offline `lint_schema` PII rule as a second line, the lint report surfacing every suppression, and
  documenting that the extract sample/audit are sensitive artifacts. Residual detection risk is
  inherent to Comprehend and shared with the existing `convert` audit.
- **(Deferred) langextract** — grounding/viz reversal trigger tracked in
  [`../research-notes/08-langextract.md`](../research-notes/08-langextract.md).
- **Schema-format bikeshedding** — kept minimal/flat for v1; tables additive later.
