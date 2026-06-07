# Overview & When to Use Synthetic Data

## What is synthetic data?

Synthetic data is data that has **not** been collected through direct
observation. It can be generated via:

- simulation,
- probability-distribution sampling, or
- an ML-based data-generation model.

For AI/ML applications it can **replace** or **supplement** data collected from
direct observation.

## Reasons to use it

- **Data privacy** — when data contains PHI/PII that must be removed,
  anonymized, or not used at all. Synthetic data replaces real data and helps
  guard against privacy attacks on exposed models. (For PHI, follow the relevant
  HIPAA compliance process; when in doubt, escalate to engagement security.)
- **Data scarcity / availability** — too little real data to train a performant
  model; synthetic data augments it to improve performance and generalization.
- **Data costs** — when collecting real data is difficult or costly, synthetic
  data augments it and reduces collection cost.
- **Data compliance** — when internal/external regulation (e.g. data ownership)
  forbids using real data. A common pattern: real data lives in a production
  account and cannot move; build and test in non-prod with synthetic data, then
  use real data once deployed to production.
- **Data sharing (consulting-specific)** — customers hesitant to share real data
  may engage more readily if synthetic data is used in place of it.

## When to use it (guardrails)

- **In demos / GenAI demos:** only input synthetic data or publicly available,
  non-copyright-protected information.
- **In non-production environments:** development, testing, and staging should
  use synthetic data, not real customer/business data.
- **Do NOT** use real customer data, PII, PHI, biometric data, or confidential
  information in demos or non-production environments.

## Best practices

- Validate that synthetic data does not contain real information.
- Implement quality checks for data utility.
- Use differential-privacy techniques where applicable.
- Leverage pre-approved synthetic datasets when possible.

## Important reminders

- Using production data outside approved production environments can constitute
  a serious (SEV-2) security incident.
- Synthetic data should be properly labeled with its data classification.
