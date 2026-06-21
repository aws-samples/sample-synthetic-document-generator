# pocsynth recipes

Multi-step workflows that combine pocsynth subcommands with other tools.
Each recipe is a playbook Claude should follow when the user's intent
matches the "when to use" line. Use these **in addition to** the baseline
guidance in `SKILL.md` — recipes don't replace the confirm/fast-mode
distinction or the warn-before-run rules.

Load this file when the user's request implies a multi-step document
workflow (corpus building, benchmarking, customer handover, live demo).
For single-document "just convert this" requests, stay in SKILL.md's
default flow.

---

## Recipe 1 — Build a synthetic corpus for a RAG PoC

**When to use.** User says something like: "I need a corpus of synthetic
contracts for our RAG PoC", "generate 20 synthetic documents from this
sample", "build me a test dataset", "make variations of this PDF".

**Steps.**

1. Run `./pocsynth.py --json doctor` if this is the first pocsynth call
   in the conversation. Proceed only if `result.all_ok` is true.
2. Confirm with the user via **one** AskUserQuestion call:
   - How many variations? (recommend 10-20; warn if >25 — that's real
     money.)
   - Starting from one source PDF or several?
   - Output format: HTML (layout-faithful) or Markdown (diff-friendly)?
3. For each variation, invoke `./pocsynth.py --json convert <source> --mode
   synthetic --num-docs 1 --format <fmt>`. Prefer `--num-docs N` in a
   single call over N separate calls when the source is the same — it's
   slightly faster and the output folders are already enumerated.
4. Collect the `result.output.combined_path` values. Report to the user
   as a list, and summarize:
   - Total pages processed across all variations
   - Total `bedrock_usage.input_tokens + output_tokens`
   - An approximate dollar cost (see the README cost table)
5. Remind the user: **synthetic is PG-grade, not legally reviewed.** If
   this corpus is going into a customer-facing demo, spot-read at least
   one doc before showing it.

**Anti-patterns.** Don't loop 20 separate `convert` calls with
`--num-docs 1` if the source is the same; use `--num-docs 20`. Don't run
without a page cap on the first variation — cap at 5 or 10, validate
quality, then scale up.

---

## Recipe 2 — Benchmark three models on the same document

**When to use.** User says: "which model should I use", "benchmark
Sonnet vs Opus vs Haiku", "compare models on this doc", "cost/quality
tradeoff".

**Steps.**

1. Run doctor if new env.
2. Run `./pocsynth.py --json models` to show the user the context-window
   and description for each available model.
3. Confirm: which subset of models? (Default: all three. If the user
   insists on all three for a >50-page doc, warn about Opus cost.)
4. For each model in the confirmed set, run `convert --model <name>
   --pages <same cap> --no-pii-audit` on the same source. Use a small
   page cap for benchmarking (5 is usually enough to show quality
   differences).
5. Parse the three envelopes. Compare in a table for the user. Read `result.cost.total_cost_usd` directly from each convert envelope (no more "approx" — it's computed from actual token usage):

   | model | pages_processed | input_tokens | output_tokens | wall_time_seconds | cost_usd |
   |---|---|---|---|---|---|

   For customers evaluating on cost alone, also call `pocsynth --json estimate` against the source PDF for each model *before* running convert — that lets you show a pre-flight price in the confirmation prompt and reduces the blast radius of picking the wrong model.

6. Quality comparison: open the three `combined_path` files and let the
   user see one page of each side by side. Do NOT judge quality yourself
   — show the output, let the user decide.
7. Recommend based on data:
   - Small docs (<50 pages), cost-sensitive → Haiku.
   - Default → Sonnet.
   - >200k input tokens or high-quality required → Opus.

**Anti-patterns.** Don't benchmark across different page caps; the
tokens-per-page will dominate and you'll measure noise. Don't benchmark
with PII audit on — it adds Comprehend latency unrelated to the model
choice.

---

## Recipe 3 — Produce a customer handover package

**When to use.** User says: "create a handover for customer X", "package
this for external sharing", "produce a deliverable", "make a redacted
version for the customer".

**Steps.**

1. Run doctor if new env.
2. Confirm via AskUserQuestion:
   - Source PDF or PDFs.
   - Output format (HTML for layout, Markdown for text-diff-friendly
     handovers).
   - Target location — where should the deliverable dir go?
3. Invoke `convert --mode synthetic --format <fmt> --pii-audit
   --redact-values --output-dir <target>`. **Always** pass
   `--redact-values` for external sharing so the PII audit CSV doesn't
   leak raw PII even if the audit dir is included in the deliverable.
4. Read `result.pii_audit.entities_found`:
   - 0 → tell the user "Comprehend found no PII; still recommend a spot
     read before sending."
   - \>0 → surface the number, explain that the originals were redacted
     in the audit and replaced with synthetic equivalents in the output,
     recommend the user review `result.output.combined_path` before
     sending.
5. Create a handover summary the user can paste into an email or JIRA:
   - Source doc metadata (page count, input size)
   - Model used + approximate cost
   - Output path(s)
   - Any PII detection statistics
   - **Explicit disclaimer**: "Synthetic output is suitable for demos
     and shape-of-data preview; not legally reviewed; do not use as a
     real contract or compliance sample."

**Anti-patterns.** Don't send the handover without `--redact-values` on
the audit. Don't skip the PII audit — even for synthetic output, the
audit step is proof that no originals leaked through the rewrite.

---

## Recipe 4 — Live customer demo of Bedrock + Comprehend

**When to use.** User is running a live demo and says: "show the customer
what Bedrock can do with this sample", "let's demo PII detection live",
"walk through the tool".

**Steps.**

1. Run doctor **first**, out loud if the screen is shared. The checks[]
   array is itself the demo: "here's Bedrock reachable, here's Comprehend
   reachable, here's my STS identity."
2. Run `./pocsynth.py --json models` and display the table — sets the
   context for the model choice.
3. Run one `convert` on a small sample (1-2 pages). Use human mode
   (omit `--json`) so the Rich progress bar is visible to the audience.
   Do NOT use `--stream` — the NDJSON lines are noisier than the Rich
   spinner.
4. Show the output file in a browser (HTML) or editor (Markdown).
5. If time allows, run `pii-audit` on an adversarial example (output.html
   with obvious PII) and show the audit CSV.
6. Summarize cost and latency from the envelope, reinforce the selling
   points (stable contract, structured error handling, 1M context on
   Sonnet/Opus).

**Anti-patterns.** Don't run convert on a many-page doc live — audience
patience runs out past 15-20 seconds. Don't use `--json` mode on a
projected demo; the human mode is more readable.

---

## Recipe 5 — Generate a synthetic dataset (free, offline)

**When to use.** User says: "generate test data", "make me 1000 rows of
fake customer data", "I need a sample dataset for a demo", "synthetic data
for a B2B SaaS / e-commerce / healthcare demo".

**Steps.**

1. **No `doctor` needed** — this path never touches AWS. List the bundled
   schemas: `./pocsynth.py --json presets`.
2. If a preset fits, generate directly:
   `./pocsynth.py --json generate --preset b2b_saas --rows 1000 --seed 42 --format csv -o ./out`
   Use `--seed` whenever the user wants a reproducible dataset.
3. If the user needs a custom shape, author a schema file (or use Recipe 7),
   then `generate --schema my_schema.json`.
4. Validate before handing it over (Recipe 6).

**Anti-patterns.** Don't call a paid `convert`/`extract` to produce *data* —
those produce documents/records from a real PDF. For made-up rows, `generate`
is free and instant. Don't omit `--seed` if the user expects to reproduce the
file later.

---

## Recipe 6 — Validate generated data against its schema (free, offline)

**When to use.** After any `generate`, or when the user asks "does this data
match the schema", "validate this CSV", "check the dataset".

**Steps.**

1. `./pocsynth.py --json test --rows ./out/rows.csv --schema my_schema.json`
2. Exit code 0 ⇒ valid. Exit code 7 (`DATA_INVALID`) ⇒ rows violate the
   schema; read `result.context.report.violations` (each has row/field/rule)
   and surface a summary. **Do not retry** — the data or schema needs fixing,
   not a re-run.

**Anti-patterns.** Don't treat exit 7 as a transient error. It's a definitive
"these rows don't conform" verdict.

---

## Recipe 7 — Full pipeline: real PDF → reusable schema → unlimited free rows

**When to use.** User says: "make synthetic data that looks like this real
document", "I have a sample report, generate 10k fake ones", "extract the
shape of this PDF and generate data from it".

**The cost story.** `extract` + `schema --infer` are the only paid steps and
run **once**; `generate` + `test` are free and unlimited. One-or-two paid
calls → unlimited free, validated rows.

**Steps.**

1. Run `doctor` if new env (the extract/schema-infer steps call Bedrock).
2. **Estimate first** (cost gate): `./pocsynth.py --json estimate <pdf> --for extract`.
   If `result.total_cost_usd` > $0.10, surface it via AskUserQuestion before
   proceeding.
3. `./pocsynth.py --json extract <pdf> -o ./out` (discovery mode). This also
   PII-audits the extracted values by default. Note `result.pii_audit`.
4. `./pocsynth.py --json schema --from-sample ./out/sample.json -o ./out`.
   Review `./out/schema.md` (the data dictionary) and `./out/lint_report.json`.
   **PII fields never carry real values** — they're bound to Faker providers
   (ADR-0005), so the generated data is safe to share.
5. `./pocsynth.py --json generate --schema ./out/schema.json --rows 10000 --seed 1`
   (free), then `test` (Recipe 6).

**Anti-patterns.** Don't loop paid `convert`/`extract` over thousands of
documents to "make a dataset" — extract the schema once, then `generate` for
free. Don't skip the PII audit (`--no-pii-audit`) when the source PDF contains
real customer data.

---

## Recipe 8 — Describe a business → schema (no document)

**When to use.** User has no document: "generate data for a B2B SaaS company",
"I need fake marketplace orders", "synthetic data for a fintech demo".

**Steps.**

1. `doctor` if new env. `estimate <anything> --for schema` is pennies — you can
   skip the gate, but mention it spends a small amount of Bedrock.
2. `./pocsynth.py --json schema --from-prompt "a B2B SaaS company's customer accounts with plan tier and MRR" -o ./out`
3. Review `./out/schema.md`, then `generate` (Recipe 5) + `test` (Recipe 6).

**Note.** With no source document there are no real value frequencies, so
distribution weights are model-invented (`synthetic`), not inferred. That's
fine for demo data.

---

## Recipe 9 — Launch the demo UI

**When to use.** User says: "show me a UI", "is there a web interface", "demo
the data generator in a browser".

**Steps.**

1. Requires the optional extra: `uv tool install '.[ui]'`.
2. `pocsynth ui` (serves on http://127.0.0.1:8000). The page offers three seed
   sources — preset (free), describe-a-business (paid), upload-a-document
   (paid, PII-audited) — a 10-row preview, and CSV/JSON download of any size.
3. The preview's paid paths surface a cost figure; downloads reuse the
   previewed schema and are free.

**Anti-patterns.** Don't suggest the UI for headless/agent automation — use the
`--json` CLI for that. The UI is a local demo surface.

---

## When a recipe does not apply

Most single-doc requests don't need any of these. If the user says
"convert this PDF" without modifying context (no corpus, no handover, no
benchmark), stay in SKILL.md's confirm or fast mode. Don't force a
recipe where one doesn't fit — recipes are for multi-step workflows,
not for every single call.
