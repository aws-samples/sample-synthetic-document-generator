# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — web UI: command-equivalent panel
- Every UI preview now shows how to **reproduce the dataset outside the browser** as two labeled, copy-to-clipboard blocks: the exact CLI command (`pocsynth run …`) and a plain-language **`/pocsynth` agent-skill request** (Kiro or Claude Code — the skill composes and runs the CLI for you, no flags to remember). A teaching artifact so SAs can demonstrate the command line and customers can learn the workflow (CONTEXT: *Command equivalent*).
- Mirrors the user's selections: the composed prompt (pills) or the user's own text (custom) is shell-quoted into `--prompt`; document mode shows `run --document <your-file.pdf> --yes` with a placeholder + a note that the CLI does a fuller Bedrock extraction than the in-browser preview. Flags shown: `--rows`, `--seed`, `-o ./out`, `--yes`; defaulted flags omitted; `--format json` mentioned in the caption.
- Prompts are quoted with `shlex.quote` (shell-injection-safe) and HTML-escaped for display. +4 UI tests (per-mode commands, flags, injection-safety). Docs updated (README, SKILL.md, both persona guides).

### Added — secure-prototyping: UI safety panel + per-persona guides (F4)
- **UI safety/attestation panel** wired into the *existing* upload→preview→download flow (no new screens; reuses the preview-badge palette). On a document-seeded preview it shows: PII entities found, fields suppressed by the guard, the **verify verdict** (✓ PASSED / ✗ FAILED), and a **Download attestation** link (`GET /attestation`). On FAILED it names the leak (masked preview, never the real value) and states "NOT cleared for sharing".
- **Fail-closed download** — `POST /download` returns **HTTP 409** when the session's safety verdict is `fail` (ADR-0010/0011), so a leaked dataset is never served as safe. Synthetic seeds (pills/prompt) get no panel and are never blocked.
- **`verify_values()`** factored out of `run_verify` as the shared scan core, so the UI panel and the CLI `verify` use exactly the same whole-value matching (no drift).
- **Two one-page persona guides** under `docs/guides/`: `secure-prototyping-sa.md` (preset/prompt fast path, $0, synthetic by construction) and `secure-prototyping-customer.md` (document path, cost gate, fail-closed attestation), linked from the README.
- **Tests (+6 + browser):** `tests/scenarios/test_ui_safety_panel.py` (PASS panel + attestation download, FAILED panel + 409 fail-closed download, masked leak, synthetic-seed has no panel) through the FastAPI TestClient; plus a Playwright browser smoke of the real upload→preview→download flow confirming the panel renders, the leak is masked, and the download is blocked. Full suite **407 passed**.

### Added — secure-prototyping: one-shot `run` verb (F2, safe-by-default)
- **`run`** — one command chains the whole pipeline so no persona threads artifacts by hand. Exactly one seed source: `--preset NAME` / `--prompt "…"` → schema → generate (the SA fast path; preset is free, prompt is one small paid call), or `--document FILE` → extract → schema → generate → **verify** (the Customer-runner path; emits the Attestation). Reuses the existing `run_*` functions verbatim — no new generation/extraction/verification logic — so the one-shot path can never drift from the individual verbs.
- **Safe-by-default (ADR-0011):**
  - **Code-enforced cost gate** — paid paths auto-estimate and refuse to spend above the gate (~$0.10) unless explicitly confirmed (`--yes` / `-y`). `--no-gate` disables it; `--cost-threshold` is configurable in code. The gate is enforced *before* any Bedrock call, protecting an AWS-naive runner from surprise spend (previously only a SKILL.md instruction an agent could skip).
  - **Fail-closed verify** — the document path always runs `verify`; a failed attestation aborts with exit **8** (`PII_LEAK_DETECTED`), names the leak, and marks the output **NOT cleared for sharing** (the dataset is still written for inspection). Override is explicit (`--share-anyway`) and recorded in the result (`override_acknowledged`). Synthetic seeds (preset/prompt) get a `not_applicable` verdict and are cleared by construction.
- **`CostGateError`** (exit 2) added to the error taxonomy; `src/pocsynth/run.py` new orchestrator module. **Tests (+15):** `tests/unit/test_run.py` (seed routing, free preset path, cost gate block/`--yes`/`--no-gate`, document pass, the fragment-hole leak that fails closed, `--share-anyway` override) + CLI `run` contract tests. Full suite **401 passed**.

### Added — secure-prototyping: expanded presets (F3)
- **Seven new bundled presets** covering common industry verticals, growing `presets` from 3 to **10**: `crm_contacts`, `insurance_claims`, `utility_meter`, `loyalty_pos`, `ad_campaign`, `knowledge_corpus`, `security_telemetry` (alongside the existing `b2b_saas` / `ecommerce_orders` / `healthcare_lite`). Pure bundled v1 schema JSON with faker / enum / weights / regex pre-set — **free, offline, instant**, and **synthetic by construction** (no real source → safe to share, no `verify` needed). No new code; the bundler inlines them into the skill script automatically.
- **Tests:** `test_presets.py` reworked to parametrize over the *entire* registry (adding a preset auto-extends coverage): every preset loads, validates, generates 50 rows, and round-trips through `test`; a static guard asserts no preset enum is a free-form identifier (the ADR-0005 invariant, so the fast path provably needs no `verify`).

### Added — secure-prototyping: safety verification (F1)
- **`verify --rows --sample [--schema]`** — offline, free affirmative proof that a generated dataset carries **no real source PII**. Answers the question the PII guard (ADR-0005) could only *design for*: it scans the generated **Rows** *and* the shared **Schema** artifact (enum values, regex patterns, descriptions) against the real Comprehend-flagged values recorded in the originating extract **Sample**, by exact whole-value match. Non-PII real values (state codes, plan tiers) are allowed to survive as enums by design. **Fail-closed:** exit **8** (`PII_LEAK_DETECTED`) if any real value leaked — the dataset is written but NOT cleared for sharing (ADR-0010). A `not_applicable` verdict covers public-data seeds and hand-authored schemas with no real source.
- **Attestation** (`attestation.json`, `-o`) — a hashable verdict record: `verdict`, `tool_version`, `source_hash`, `rows_sha256`, and any leaks with **masked** previews (the attestation never re-leaks the value it reports). Rides in the error context on failure so CI / agents can gate on it.
- **`LeakDetectedError`** (exit 8) added to the error taxonomy; `src/pocsynth/verify.py` new module. **Tests (+13):** `tests/unit/test_verify.py` (verdicts, schema/regex leak detection, masking, conform path) + CLI fail-closed exit-8 contract in `test_cli_structured.py`.

### Added — structured-data pipeline (extract → schema → generate → test)
- **Four new CLI commands** turning `pocsynth` into a synthetic *tabular data* generator alongside document synthesis:
  - **`generate --schema|--preset --rows --seed --format csv|json`** — offline Faker generation. Deterministic with `--seed`, typed serialization (ISO-8601 dates in both CSV and JSON), weighted `enum` distributions, and fail-fast on unknown Faker providers. Free; touches no AWS.
  - **`test --rows --schema`** — offline validation of rows against a schema (type / enum / regex). Exit code **7** (`DATA_INVALID`) with a structured violation report when rows don't conform. Free.
  - **`extract PDF [--schema]`** — Bedrock structured extraction via forced `toolConfig` (ADR-0002): discovery mode (field observations + value counts) or conform mode (records matching a schema). Audits extracted values with Comprehend by default and flags PII fields. Paid.
  - **`schema --from-sample|--from-prompt|--from-schema [--fix] [--distribution]`** — builds a generation-ready schema from an extract sample or a natural-language description (paid, forced `emit_schema`), or lints/documents/auto-fixes a user schema (free, offline). Emits a Markdown data dictionary + a lint report.
  - **`presets`** — lists the bundled schemas (`b2b_saas`, `ecommerce_orders`, `healthcare_lite`).
  - **`estimate <path> --for convert|extract|schema`** — the pre-flight estimator now covers the paid pipeline stages (ADR-0007), keeping one cost-gate idiom across every Bedrock-spending command.
- **PII guard (ADR-0005)** — `schema --infer` never lets a real PII value become an `enum`; PII fields (Comprehend-flagged or bound to an identifying Faker provider) are bound to Faker and the real values discarded, with the suppression surfaced in the lint report. An offline `lint_schema` rule backstops the same invariant for user-authored schemas. The generated dataset is safe to share; the extract sample and audit CSV are not.
- **Demo web UI** (`pocsynth ui`, optional `pocsynth[ui]` extra; ADR-0009) — FastAPI + HTMX, Metabase-style fill-in-the-blank sentence with three seed sources (preset / describe-a-business / upload-a-document), a 10-row preview, and CSV/JSON download. Calls the same core as the CLI; HTMX pinned with Subresource Integrity. Untrusted HTTP inputs are bounded (row cap, upload-size cap, session-cache eviction).
- **New modules:** `schema.py` (shared model: validation, typing, lint/fix/document, distribution helpers, toolspec builders), `generate.py`, `validate.py`, `schemagen.py`, `extract.py`, `presets/`, `ui/`. `bedrock.read_tool_use` shared by both paid stages. `faker` added as a core dependency (pinned, and inlined into the skill bundle).
- **Tests (+~90):** unit tests for schema/generate/validate/presets/extraction/schemagen/paid-estimates including the generate→test round-trip across every type in both formats; plus `tests/scenarios/` — three SA demo-data scenarios (customer-data-seeded with a PII non-leak guarantee, public-data-seeded, prompt-seeded) driven through both the CLI and the UI. Full suite **261 passed**.
- **9 ADRs + research notes + CONTEXT.md** under `docs/` capturing the design (native-Bedrock extraction, forced toolConfig, the four-stage pipeline, distributions, the PII guard, typed coercion, estimate coverage, schema-from-prompt, the UI stack).
- **SKILL.md + RECIPES.md** — the new commands, the paid/free split, the PII posture, exit code 7, and five new recipes (generate a dataset, validate it, the full pipeline cost-saver, describe-a-business, launch the UI).

### Added — cost estimation
- **`pocsynth estimate PDF --model --pages --pii-audit`** — offline pre-flight cost estimator. Heuristic-based (±30-50%): reads text chars from the PDF, applies a chars/token ratio + per-page image-token constant + output-token constant, looks up Bedrock + Comprehend rates from the bundled pricing file. Exit codes and envelope match the existing agent-friendly contract.
- **`result.cost` block in `convert` envelopes** — post-flight, exact cost from the Bedrock token usage returned by Converse plus Comprehend char count from the combined output. Non-breaking additive change at schema 1.
- **`src/pocsynth/pricing.json`** — committed pricing snapshot covering Claude Sonnet / Opus / Haiku 4.6 on-demand rates + Amazon Comprehend tiered PII pricing (Detect + Contains). Inlined into the bundled skill script so the skill works offline. `provenance` fields record whether each number came from the Pricing API or a hardcoded fallback.
- **`src/pocsynth/pricing.py`** — pure-function cost estimators (`estimate_bedrock_cost`, `estimate_comprehend_cost`, `estimate_convert_cost`, `actual_convert_cost`) with tiered Comprehend math, 300-char minimum, 100-char rounding, pricing-file staleness guard, and region coverage checks.
- **`src/pocsynth/aws.py::resolve_region`** — centralized region-resolution chain (CLI → env → profile → IMDS → default) replacing three hand-rolled fallbacks across `cli.py` and `core.py`. Doctor now reports `source` consistently.
- **`tests/unit/test_pricing.py`** (+52) — covers loading, Bedrock math, Comprehend 300-char minimum, 100-char rounding, tier boundaries, multi-tier spans, staleness, region coverage, key-drift guard against `MODELS`, heuristic-based estimate, actual-cost computation.
- **`tests/unit/test_estimate_cli.py`** (+6) — CLI surface tests: help, `--json` envelope, `--no-pii-audit`, missing file → exit 3, `--pages` cap reduces cost, `convert` envelope includes `result.cost`.
- **SKILL.md + RECIPES.md** — skill instructs Claude to call `estimate` before any non-trivial convert, surface `result.total_cost_usd` in AskUserQuestion for runs >$0.10, and use the cost block in the model-benchmarking recipe.
- **New eval case `cost-estimate-before-expensive-run`** in `evals.json` asserting Claude runs `estimate` first before committing to an Opus / large-doc convert.

Total stubbed tests now: **177** (was 119).

### Added — Claude Code skill
- **`skills/pocsynth/` Claude Code skill** with a bundled self-contained single-file Python CLI (`pocsynth.py`, ~56 KB) that carries [PEP 723 inline-script metadata](https://peps.python.org/pep-0723/) so it runs via `uv run --script` with no prior `pip install`. Feature-compatible with the installed `pocsynth` CLI by construction (generated from `src/pocsynth/` via stickytape).
- **`SKILL.md` playbook** documenting two interaction modes: default confirm mode (Claude calls `AskUserQuestion` bundling model / format / mode / pages / PII-audit / redact-values in one call) and fast mode (skip confirmation when user says "fast", "just do it", etc.). Warn-before-run rules for large docs, `--pii-audit` without redaction, and `--num-docs > 1`.
- **`scripts/generate-skill-script.py`** — the bundler. CI (`skill-script-drift` job) re-runs it and fails the MR if the committed artifact has drifted.
- **`scripts/generate-eval-fixtures.py`** — regenerates the deterministic fixtures (`contract.pdf`, `120-page-contract.pdf`, `sample.pdf`, `output.html`) used by the evals.
- **`scripts/run-skill-evals.py`** — deterministic half of the skill evaluation (exit codes, JSON shape, invocation argv, file-on-disk assertions). Behavioral half is graded by a subagent reading `skills/pocsynth/agents/grader.md`.
- **Skill behavioral evals** (`skills/pocsynth/evals/evals.json`) with 7 cases following the [anthropic/skills](https://github.com/anthropics/skills) skill-creator convention: `convert-confirm`, `fast-mode`, `doctor-first`, `large-doc-warn`, `pii-audit-standalone`, `redact-warn-on-share`, `auth-failure-routing`. Each case carries both deterministic and behavioral assertions.
- **New unit tests** (`tests/unit/test_skill_script.py`, +9): byte-equal JSON envelopes between the installed CLI and the bundled script across `models`, `version`, and error envelopes for `INPUT_NOT_FOUND` / `URL_REJECTED`; `--help` length-parity check; self-containment test (script runs outside the repo); PEP 723 header structure. Total stubbed suite now 95.
- **`stickytape`** added to the dev dependency group.

### Added — CLI refactor to `pocsynth`
- **New Typer-based CLI (`pocsynth`)** with four subcommands: `convert`, `pii-audit`, `models`, `doctor` (+ `version`). Non-interactive by default.
- **Stable JSON output contract (schema 1)** with `ok` / `schema` / `tool_version` / `command` / `event` envelope, nested `result` (`input` / `output` / `pii_audit`), structured `error` (`code` / `message` / `retryable` / `hint` / `context`), and full observability (`bedrock_usage`, `wall_time_seconds`, per-page paths).
- **NDJSON streaming** via `--json --stream`: each line carries the envelope; final `complete` event matches a non-stream `--json` call.
- **Classifiable exit codes**: 0 / 1 / 2 / 3 / 4 / 5 / 6 for OK / UNKNOWN / USAGE / INPUT / AUTH / UPSTREAM / PARTIAL.
- **`doctor` subcommand** — runs real minimal Bedrock Converse + Comprehend DetectPiiEntities probes; intended as the first command an agent calls in a new environment.
- **`pocsynth` entrypoint** via `pyproject.toml` (`[project.scripts]`). `pdf_synth_bedrock.py` becomes a permanent deprecation shim that re-exports the former public names and emits `DeprecationWarning`.
- **Package layout** under `src/pocsynth/` with focused modules (`cli`, `core`, `bedrock`, `comprehend`, `pdf`, `prompts`, `textutil`, `output`, `errors`).
- **`--profile` flag** for AWS profile selection (in addition to existing `--region`).
- **PyInstaller build scripts**: `scripts/build-macos.sh`, `build-linux.sh`, `build-windows.ps1`; produce both `--onedir` and `--onefile` artifacts. Committed `pocsynth.spec` for reproducible builds.
- **New unit tests** (+28): `test_output.py`, `test_cli.py` (stdout-purity sweep, exit-code coverage, stream invariant, doctor shape), `test_shim.py`. Total stubbed suite now 86.
- **Ruff `T201`** lint rule to prevent bare `print()` in `src/pocsynth/`; `output.emit()` is the only stdout writer.
- **`uv.lock`** for reproducible, hash-verified installs.
- **GitLab CI**: `build-macos` / `build-windows` / `build-linux` jobs (allow_failure initially, runner tags are placeholders).

### Added — previous (pre-CLI-refactor work)
- CLI flags: `--pdf`, `--num-docs`, `--model`, `--format`, `--mode`, `--system-prompt`, `--pages`, `--pii-audit`, `--max-tokens`, `--region`, `--log-level`. Interactive prompts remain as fallback.
- Support for Claude Sonnet 4.6 and Opus 4.6 (1M-token context on Bedrock) and Haiku 4.5 (200k) via global cross-Region inference profiles.
- `logging` based output (replaces ad-hoc `print` calls).
- Remote PDF download size cap (100 MB) via streaming `iter_content`.
- `csv.writer`-based PII audit output so values containing commas/quotes/newlines are escaped properly.
- `tests/unit/` — 41 stubbed tests (helpers, `get_pdf_file`, `scan_for_pii`, `process_page`, prompt construction). Runs in CI.
- `tests/live/` — 4 live AWS smoke tests (Bedrock Converse, Comprehend true-positive + true-negative, multimodal `process_page` with a fitz-rendered PDF). Gated by `@pytest.mark.live`, excluded by default.
- `pytest.ini`, `requirements-dev.txt`, and a `unit-tests` GitLab CI job.

### Changed
- **Migrated dep manager to `uv`.** `requirements.txt`, `requirements-dev.txt`, `requirements.lock`, `requirements-dev.lock` removed; replaced by `pyproject.toml` + `uv.lock`.
- **SSRF hardening behaviour now raises** (`UrlRejectedError` / `InputError` / `HttpError`) instead of returning `None` with a log line. Existing `validate_safe_url` return-signature preserved for tests.
- **`process_page()` now returns `{"text": str, "usage": {"input_tokens": int, "output_tokens": int}}`** — was a bare string. The old behavior is broken for existing direct callers; use `result["text"]` to migrate.
- Prompt rewrite: format-aware system prompt that suppresses preambles and code fences; user prompt split into structured task list, enumerated PII categories, faithfulness guardrails (preserve non-PII numerics, image-vs-text tie-break), explicit layout rules (headers/footers/multi-column), and anti-injection framing.
- `build_prompt()` now emits format-specific formatting rules (HTML tags for HTML; Markdown syntax and pipe tables for Markdown).
- Combined HTML output now produces a single well-formed document (one `<!DOCTYPE>`/`<html>`/`<body>`) with each page as a `<section>` (was: concatenating multiple full documents).
- Per-page output folders now use the sanitized full filename stem (was: `filename[:4]`, which collided on similarly-named PDFs).
- `requirements.txt` is now version-pinned; removed unrelated `fitz` and unused `chardet` and `pillow`.
- AWS region is configurable via `--region` / `AWS_REGION` (default `us-east-1`) instead of hard-coded.
- Example IAM policy in README updated to reference current (4.6 / 4.5) model ARNs and inference-profile ARNs.

### Fixed
- **verify no longer fails open on escaped values** — the safety scan now checks the rows' *decoded* cell values in addition to the raw serialized text, so a real PII value containing a quote/comma/newline can't hide behind CSV quote-doubling (`"a ""b"" c"`) or JSON string escaping (`a \"b\" c`). `run_verify` also **fails closed** (raises) on a JSON rows file that won't parse, rather than silently CSV-misdecoding it, and now reports a deduped `leaked_fields` list.
- **Deterministic clock is now concurrency-safe across modes** — the process-global Faker date-clock patch used for `--seed` reproducibility is a mode-exclusive group mutex: a concurrent *unseeded* generation can no longer read a seeded run's frozen anchor (it kept the live clock), while same-mode runs still run in parallel (the always-seeded web UI keeps full concurrency).
- **Provider sanitizer recovers whitespace-padded providers** — a model-emitted `faker: "  name  "` is trimmed and kept (was needlessly downgraded to the generic `word` fallback); non-string `faker` values are coerced without crashing.
- HTML attributes no longer stripped — removed `response.replace('"', '')` that destroyed every `class=""` / `href=""` in model output.
- First line of model output no longer blindly discarded; `strip_model_preamble()` now removes only actual preambles and code fences.
- Downloaded PDF always cleaned up via `tempfile` + `try/finally` (was leaking on errors).
- Output files written with explicit `encoding="utf-8"` (was using platform default).
- Removed redundant `fitz.Pixmap(img_bytes)` reconstruction; uses `pix.save()` directly.
- Grammar: `"Here is an text export"` → `"Here is a text export"`.

### Security
- Remote PDF download caps response size to prevent unbounded memory use.
- Anti-injection reinforcement in both user and system prompts: content inside `<raw_text>` is data to convert, never instructions to follow.
- Downloaded PDF written to an isolated `tempfile.NamedTemporaryFile` instead of a predictable path in the CWD.
