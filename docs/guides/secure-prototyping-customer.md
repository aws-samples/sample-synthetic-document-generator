# Secure prototyping in 1 command — for the Customer-runner (your own account)

You're running this in **your own AWS account**, against **your own** document,
to produce synthetic data you can hand to a vendor, a model, or a teammate. You
need a dataset shaped like your real one — and **proof** that no real value
leaked into it.

## The one command

```bash
pocsynth run --document intake.pdf --rows 10000 --yes -o ./out
```

`run` chains the full pipeline on the document seed:

1. **extract** — pull field observations from the PDF (Bedrock), audited for PII
   with Amazon Comprehend.
2. **schema** — design a generation-ready schema; the **PII guard** binds any
   PII-flagged field to a synthetic generator and discards the real values.
3. **generate** — produce `./out/rows.csv` (free, offline, deterministic).
4. **verify** — scan the generated rows **and** the schema artifact against the
   real PII values from your document. Emits `./out/attestation.json`.

## Safe-by-default

This path spends on Bedrock, so it is **cost-gated**: if the projected cost is
above ~$0.10, `run` stops and shows the estimate. Add `--yes` to proceed (or
`pocsynth estimate intake.pdf --for extract` first to see the number).

It is also **fail-closed**. If verify finds a real value in the output, `run`:

- exits non-zero (code **8**, `PII_LEAK_DETECTED`),
- names the leaked field and where it leaked (rows / schema),
- marks the dataset **NOT cleared for sharing** — the file is still written so
  you can inspect it, but it is never presented as safe.

```text
$ pocsynth run --document intake.pdf --rows 10000 --yes -o ./out
… extract → schema → generate → verify
ERROR PII_LEAK_DETECTED: 1 real source value(s) leaked — NOT cleared for sharing
      see ./out/attestation.json
```

A clean run instead reports `verdict: pass` and writes an attestation you can
hand to your reviewer:

```json
{
  "verdict": "pass",
  "tool_version": "0.1.0",
  "source_hash": "…",
  "rows_sha256": "…",
  "candidate_pii_values": 4,
  "leaks": []
}
```

The attestation is hashable and **never contains a real value** — leaks (if any)
are masked (`55*******88`), so the proof can't itself re-leak.

## Override (deliberate, and logged)

If you've reviewed a failed run and understand the finding, you can force the
output through with `--share-anyway`. The verdict still says `fail` and the
result records `override_acknowledged: true` — the default protects; the override
is explicit.

## In the web UI

`pocsynth ui` → the **"Match a document"** tab does the same flow. After preview
a **safety panel** shows the PII entities found, the verdict (✓ PASSED / ✗
FAILED), and a **Download attestation** button. On a failed verdict the download
is blocked — "NOT cleared for sharing" — until you regenerate or fix the schema.

---
*Related: [ADR-0010 safety verification](../adr/0010-safety-verification.md),
[ADR-0011 one-shot safe-by-default](../adr/0011-one-shot-safe-by-default.md).*
