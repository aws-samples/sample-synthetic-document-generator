---
name: synth-data
description: >
  Generate synthetic tabular datasets (CSV/JSON) for demos, tests, and
  dashboards — from a bundled business preset, a natural-language description,
  or a reference document. Validate generated data against its schema. Use
  when the user asks for fake/test/sample/demo/dummy data, a synthetic dataset,
  "rows of customer/order/patient data", to mock a database table, or to turn a
  business description or a reference PDF into a reusable data schema. Wraps the
  self-contained `pocsynth` CLI; the generation half is free and offline.
---

# synth-data

Generate believable synthetic **tabular data** (not documents). This skill
drives the structured-data half of the `pocsynth` CLI. The generation and
validation steps are **free and offline** (Faker); only schema inference from a
reference document or a description spends Bedrock tokens.

> This is the data-generation companion to the `pocsynth` skill (which converts
> PDFs into synthetic *documents*). If the user wants a rewritten/redacted
> *document*, use `pocsynth`. If they want *rows of data*, use this skill.

## The CLI

Use the same bundled script the `pocsynth` skill ships:

    ./skills/pocsynth/pocsynth.py --json <subcommand> ...

It self-deploys via `uv run --script` (no `pip install`). Always pass `--json`
when driving it from the skill; `--json` comes BEFORE the subcommand.

## Decision tree (pick the seed source)

```
Does the user have a source artifact?
├─ No, and a common domain fits  → PRESET     (free, instant)   Recipe A
├─ No, but a specific business   → FROM-PROMPT (one small paid call) Recipe C
└─ Yes, a real sample PDF        → FULL PIPELINE (paid once)     Recipe D
Always finish by VALIDATING the data (Recipe B).
```

## Cost model

| Step | Spends Bedrock? | Notes |
|---|---|---|
| `presets`, `generate`, `test` | **No** | Free, offline, unlimited. No `doctor` needed. |
| `schema --from-schema` (lint) | **No** | Offline schema lint / document / `--fix`. |
| `schema --from-prompt` | Yes (small) | One call; typically pennies. |
| `extract`, `schema --from-sample` | Yes | Run `doctor` first; gate with `estimate --for`. |

**Cost gate:** before `extract`, run `./skills/pocsynth/pocsynth.py --json
estimate <pdf> --for extract`. If `result.total_cost_usd` > $0.10, surface it
via `AskUserQuestion` before proceeding. `schema --from-prompt` is pennies — just
mention it spends a little.

## Recipes

### Recipe A — Preset dataset (free)
1. `... --json presets` to list `b2b_saas`, `ecommerce_orders`, `healthcare_lite`.
2. `... --json generate --preset <name> --rows <N> --seed <S> --format csv -o ./out`
3. Validate (Recipe B). Use `--seed` whenever reproducibility matters.

### Recipe B — Validate (free; always do this)
`... --json test --rows ./out/rows.csv --schema ./out/schema.json`
Exit 0 ⇒ valid. Exit **7** (`DATA_INVALID`) ⇒ read
`result.context.report.violations` (row/field/rule) and surface a summary. **Do
not retry on exit 7** — the data or schema needs fixing.

### Recipe C — Describe a business → schema → data (one small paid call)
1. `doctor` if a fresh env.
2. `... --json schema --from-prompt "a B2B SaaS company's customer accounts with plan tier and MRR" -o ./out`
3. Review `./out/schema.md` (the data dictionary), then `generate` (A) + `test` (B).

### Recipe D — Real PDF → reusable schema → unlimited free rows (the cost-saver)
1. `doctor` if a fresh env.
2. `... --json estimate report.pdf --for extract` → cost gate (AskUserQuestion if >$0.10).
3. `... --json extract report.pdf -o ./out` (paid; PII-audited by default).
4. `... --json schema --from-sample ./out/sample.json -o ./out` (paid). Review
   `./out/schema.md` and `./out/lint_report.json`.
5. `... --json generate --schema ./out/schema.json --rows 10000 --seed 1` (free) + `test` (B).

## PII — the guarantee to communicate

`extract` audits the values it pulls (Amazon Comprehend, on by default), and
`schema --infer` **never** lets a real PII value become an `enum` — PII fields
are bound to Faker providers and the real values are discarded. So:

- ✅ the **generated dataset is safe to share**;
- ⚠️ the **extract sample** (`./out/sample.json`) and the **PII-audit CSV**
  contain real values — treat them as sensitive, don't hand them out.

When the source PDF holds real customer data, do **not** pass `--no-pii-audit`.

## Exit codes

Same contract as the `pocsynth` skill (see its SKILL.md). The one addition:
**exit 7 `DATA_INVALID`** from `test` — surface the violation report, don't retry.

## When NOT to use this skill

- The user wants a rewritten/redacted **document** (HTML/Markdown) → use `pocsynth`.
- The user wants to scan an existing file for PII → `pocsynth pii-audit`.
