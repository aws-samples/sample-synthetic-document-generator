# 0011 — One-shot run is safe-by-default (cost gate + fail-closed verify)

**Status:** accepted

The one-shot orchestration verb (prompt/preset → schema → generate; or document → extract →
schema → generate → verify) targets personas who may be AWS-naive (the self-service
**Customer-runner**). Two defaults make it safe rather than surprising:

## (a) Code-enforced cost gate

The document/prompt path spends on Bedrock. Previously the cost gate was an *instruction* in
SKILL.md ("run `estimate` first; confirm if > $0.10") that an agent could skip. In the one-shot
verb it becomes **code-enforced**: the verb auto-estimates and requires explicit confirmation
(`--yes` on the CLI, a confirm in the UI) when projected cost exceeds the threshold. Protects the
naive runner from surprise spend.

## (b) Fail-closed verify

When the document path's **verify** step fails (a real value leaked), the command **exits non-zero
and marks the output NOT cleared for sharing**, naming the leaked field. The dataset is still
written so the user can inspect it, but it is never *presented* as safe. Rationale: a fail-open
verify would make the headline safety guarantee advisory — re-opening the "designed not to leak but
never checked" gap (ADR-0010). Security is the PRD's tiebreaker, so the default protects.

## Consequences

- A failed verify blocking a "successful" generation is surprising to someone who doesn't know why —
  hence this ADR. The error message names the leaked field and points at the Attestation.
- Power users can override both (`--no-gate` / an explicit "share anyway") — the *default* protects;
  the override is deliberate and logged in the Attestation.
- Depends on ADR-0007 (estimate covers the paid stages) and ADR-0010 (verify).
