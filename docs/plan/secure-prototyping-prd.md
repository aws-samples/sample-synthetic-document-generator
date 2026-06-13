# PRD — Secure Prototyping for pocsynth (persona-ranked, minimal + high-value)

**Status:** proposed · **Scope:** minimal, security-led · **Pairs with:** ADR-0005 (PII guard),
ADR-0010 (safety verification), ADR-0011 (one-shot safe-by-default), ADR-0007 (estimate),
ADR-0009 (demo UI). Personas + vocabulary in [`../../CONTEXT.md`](../../CONTEXT.md).

## Why this PRD exists

AWS SAs and their customers use pocsynth to build and demo on **synthetic** data so real data
never crosses a trust boundary it shouldn't. The tool already *enforces* that (the PII guard strips
real values from PII fields), but two things hold back adoption for the workflows seen across the
CDE engagement portfolio (document extraction, mail/claims intake, lease abstraction, grant
processing, telemetry/meter analytics, loyalty/POS, knowledge corpora):

1. The safety guarantee is **enforced but never proven** about a given output — there's no artifact
   a Customer-runner can hand a security reviewer.
2. Reaching a usable dataset still takes **multiple threaded steps** and AWS setup that an
   AWS-naive customer engineer shouldn't need to learn.

This PRD ranks a **minimal** set of changes to fix both, with **security as the tiebreaker**.

## Personas (by trust boundary — see CONTEXT.md)

- **SA** — Solutions Architect in an AWS **sandbox**. Seeds only from **preset or prompt** (never
  real data). Wants: speed, believability, cost control, a safe-to-share artifact.
- **Customer-runner** — customer engineer (or SA in the customer's own account/VDI), may seed from
  **real documents** (data stays in-boundary). Wants: a **provable** PII-never-leaks guarantee and
  self-service simplicity. Subsumes the pure self-service customer.

## Success bar (ranking principle)

> **Provably-safe secure prototyping, faster — security wins ties.**
> Order: (1) make the safety guarantee provable/visible → (2) cut steps to first dataset →
> (3) breadth. Big-surface work (multi-table/relational, new modalities, weakening the Comprehend
> dependency, auth/multi-user UI) is **out of scope** this pass.

---

## The four features (cross-persona ranked)

The work collapses to **four features**. They are ranked here across both personas by the success
bar; per-persona ordering follows in the sections below.

### F1 — `verify` + Attestation  ·  surface: CLI (+ UI panel)  ·  effort: M  ·  ADR-0010
**The global #1.** A dedicated offline verb that affirmatively checks generated **Rows** *and* the
**Schema** artifact against the Comprehend-flagged PII values in the **Sample** (exact whole-value
match; non-PII enums are allowed to survive by design). Emits an **Attestation** (pass/fail, leaked
fields, source-doc hash, rows hash, verdict, tool version).

- **Why #1:** it is the literal differentiator vs. "just use Faker" — it turns *designed not to
  leak* into *checked this output, it didn't*. The Customer-runner needs it to clear a security
  review; the SA gets reassurance.
- **Closes the fragment hole:** scanning the Schema (not just Rows) catches a real value leaked into
  a model-emitted `regex`, `enum`, or `description` — the shared artifact, not just the data.
- **Minimal v1:** whole-value scan of the Sample's flagged PII against Rows + Schema; plaintext +
  JSON Attestation; exit 7 on fail (mirrors `test`). **Fast-follow (same feature):** signed/hashed
  attestation. **Deferred:** external audit-system integration.

### F2 — One-shot `run` verb (two seed sources, safe-by-default)  ·  CLI (+ UI wiring)  ·  M–L  ·  ADR-0011
One verb chains the whole pipeline so no persona threads artifacts:
- `--preset NAME` / `--prompt "…"` → schema → generate  (**free**, the SA fast path)
- `--document FILE` → extract → schema → generate → **verify**  (**paid**, the Customer-runner path,
  emits the Attestation)

**Safe-by-default (ADR-0011):** code-enforced cost gate (auto-estimate; require `--yes` / UI confirm
above the $0.10 threshold) + **fail-closed verify** (a failed attestation → non-zero exit, output
marked **NOT cleared for sharing**, leaked field named; dataset still written for inspection;
override is explicit and logged).

- **Minimal v1:** route on seed source; chain existing verbs (no new generation logic); cost gate
  only on paid paths; one output dir. **Deferred:** multi-page batching, schema caching, streaming
  progress.

### F3 — Expanded presets (SIM verticals)  ·  data (CLI `--preset` + UI pills)  ·  S–M
Grow the bundled presets from 3 to cover the engagement verticals seen across the portfolio:
**CRM contacts, insurance/claims mail, utility meter/telemetry, loyalty/POS, ad-campaign,
knowledge-corpus** (plus the existing b2b_saas / ecommerce / healthcare-lite). Pure bundled JSON —
**free, offline, instant**; lands in CLI and UI pills at once; zero PII risk by construction.

- **Minimal v1:** ~7 new valid v1 schema files (faker/enum/weights pre-set) + metadata; no new code.
  **Deferred:** preset versioning, a UI schema builder, a "marketplace".

### F4 — Surfaces & docs (leverage-based allocation)  ·  UI panel + docs  ·  S
- **UI:** one **safety/attestation panel** wired into the *existing* upload→preview→download flow —
  **no new screens.** Shows: PII entities found, fields suppressed, the **verify verdict** (✓ PASSED
  / ✗ FAILED + leaked field), and a **Download attestation** button. On fail, no download-as-safe;
  the panel says "NOT cleared for sharing."
- **Docs:** two 1-page **"secure prototyping in 1 command"** guides — SA (`run --preset … → done`,
  $0) and Customer-runner (`run --document … --yes → attestation for your reviewer`). State each
  persona's guarantee plainly (preset = synthetic by construction; document = real values never
  reach output, proven by the Attestation).
- **Minimal v1:** inline HTML panel reusing existing preview-badge CSS; a `/attestation` download;
  two Markdown guides linked from README + CLI `--help`. **Deferred:** auth, multi-user, persistence,
  modals beyond the cost confirm.

---

## Ranked PRD — Persona A: **SA** (sandbox, preset/prompt-seeded)

Optimizes speed + believability + cost control; security still breaks ties.

| # | Item | Surface | Why high-value (anonymized SIM) | Simplicity win | Effort |
|---|------|---------|----------------------------------|----------------|--------|
| 1 | **Expanded presets** (F3) | data | SAs demo across verticals under time pressure (CRM/service/sales assistants, security-signal POCs, agentic hospitality). A preset is zero-prompt, zero-Bedrock, instant. | `generate --preset crm_contacts --rows 1000` → seconds, $0, safe to share. | S–M |
| 2 | **One-shot `run`** (F2, free path) | cli | "I want a dataset for `<domain>`" in one command, no artifact threading. | `run --preset … --rows N` or `run --prompt "…"`. | M |
| 3 | **`verify` + Attestation** (F1) | cli | Even sandbox demos get handed to review; an attestation pre-empts "did any real data get in?" (answer: none seeded — provable). | One offline command; mirrors `test`. | M |
| 4 | **Cost pre-flight on the prompt path** (within F2/ADR-0007) | cli | Shared sandbox budgets need no-surprise spend before a paid schema-infer. | Auto-estimate + `--yes`; reuse `estimate`. | S |
| 5 | **UI preset pills + instant preview** (F4) | ui | Non-CLI SAs demo in-browser: pick a vertical → 10-row table → download. | Pill → preview → download, one pane, offline. | S |

**SA non-goals:** interactive in-UI schema editing/refine loop (higher-effort UI; defer), new
modalities.

---

## Ranked PRD — Persona B: **Customer-runner** (own account, real-document-seeded)

Optimizes provable safety + self-service; security leads outright.

| # | Item | Surface | Why high-value (anonymized SIM) | Simplicity win | Effort |
|---|------|---------|----------------------------------|----------------|--------|
| 1 | **`verify` + Attestation** (F1) | cli (+ UI) | A team seeding from real **insurance intake mail**, **commercial leases**, **grant forms**, or **regulatory records** must prove no real SSN/policy/tenant/PI value leaked into rows *or* schema before sharing. The Attestation attaches to the security review. | One verb → one hashable artifact. | M |
| 2 | **One-shot `run --document`** (F2, paid path) | cli | The full extract→schema→generate→verify chain in one call, for an AWS-naive engineer who shouldn't learn 4 verbs. | `run --document lease.pdf --yes` → dataset + attestation. | M–L |
| 3 | **UI safety panel + attestation download** (F4) | ui | The self-service customer works in the browser; the panel *shows* the guarantee (entities found, fields suppressed, verdict) and yields the downloadable proof. | Auto-runs on the existing upload→download flow; no new screen. | S |
| 4 | **Cost gate, code-enforced** (F2/ADR-0011) | cli+ui | Protects a non-AWS-native from surprise Bedrock spend on a large document. | Auto-estimate + confirm; fail-safe default. | S |
| 5 | **Extract + schema-infer PII guard made *visible*** (ADR-0005 surfacing) | cli | The guard already strips real-value enums; surfacing the suppression (per-field PII flag + lint note) builds confidence it ran. | No new command; richer `pii_audit` + lint output. | S |
| 6 | **Per-persona docs** (F4) | docs | A customer engineer with no CDE context needs the safe path in a 2-minute read. | One worked example: doc → attestation. | XS |

**Customer-runner non-goals:** weakening/removing the Comprehend dependency for a "no-AWS" PII scan
(would make the guarantee advisory — rejected; security tiebreaker). Multi-table/relational output.

---

## Shared mechanics (the cross-cutting spine)

- **One verb, two seed sources, one safety contract** — seed source selects free (preset/prompt) vs.
  paid+verified (document); the cost gate and fail-closed verify are the *defaults*, overridable only
  by an explicit, logged flag.
- **The UI change is one panel, not a redesign** — it rides the existing `/preview`→`/download`
  flow, reusing the PII data already in the result envelope plus the new verify verdict.
- **Presets ride every surface for free** — bundled JSON appears in CLI `--preset` and UI pills with
  no per-surface work.
- **Docs make the guarantee legible** — each persona's safety story stated plainly so the artifact
  is trusted without reading the ADR stack.

## Out of scope (explicit non-goals, this pass)

- Multi-table / relational / foreign-key output.
- New input modalities (CAD, video, images) or output formats beyond CSV/JSON.
- Removing or weakening the Comprehend dependency (a regex-only PII fallback would make the
  guarantee advisory — against the security tiebreaker).
- Auth, multi-user, or persistence in the demo UI.
- A cryptographically *signed* attestation (the hashed-JSON v1 is in scope; signing is a fast-follow).

## Suggested build order (minimal first, security-led)

1. **F1 `verify` + plain Attestation** (CLI) — unblocks the headline guarantee; pure offline, reuses
   the Sample. *(Highest value, both personas.)*
2. **F3 expanded presets** — pure data, no code; widens the SA fast path immediately.
3. **F2 one-shot `run`** with cost gate + fail-closed verify — glue over existing verbs.
4. **F4 UI safety panel + per-persona docs** — surfaces F1 where the Customer-runner sees it.
5. **Fast-follows:** signed attestation; visible PII-guard surfacing polish.
