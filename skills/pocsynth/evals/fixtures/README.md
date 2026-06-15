# Eval fixtures

Small, deterministic files used by `evals.json`. Regenerate via:

    uv run python scripts/generate-eval-fixtures.py

Files:

- `contract.pdf` — 2 pages of fitz-generated placeholder "contract" text.
  Used by `convert-confirm`, `fast-mode`.
- `120-page-contract.pdf` — fitz-generated 120-page filler document.
  Used by `large-doc-warn` to trigger the >50-page cost warning.
- `sample.pdf` — 1 page of fitz-generated placeholder text.
  Used by `redact-warn-on-share`, `auth-failure-routing`.
- `output.html` — small HTML file containing a synthetic name / email /
  phone so Comprehend has something to detect. Used by `pii-audit-standalone`.

All content is synthetic; no real PII.
