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

## When a recipe does not apply

Most single-doc requests don't need any of these. If the user says
"convert this PDF" without modifying context (no corpus, no handover, no
benchmark), stay in SKILL.md's confirm or fast mode. Don't force a
recipe where one doesn't fit — recipes are for multi-step workflows,
not for every single call.
