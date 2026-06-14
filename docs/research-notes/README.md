# Research Notes — Synthetic Data

Working notes on the generation and use of synthetic data, compiled to inform
the structured-data support work in this repo. A mix of older prescriptive
guidance and newer observations.

> Note: information here is for general informational purposes and is not
> intended to be complete.

## Index

- [Overview & When to Use](./01-overview.md) — what synthetic data is, why and when to use it, guardrails.
- [Generation Methods](./02-generation-methods.md) — business-knowledge vs. example-data approaches, sampling.
- [Quality & Representativeness](./03-quality-and-representativeness.md) — metrics, validation at inference, realism, coverage.
- [Compliance, Privacy & Bias](./04-compliance-privacy-bias.md) — data handling, differential privacy, fairness.
- [References](./05-references.md) — papers, tools, datasets.
- [Multipurpose Demo Data Kit](./06-demo-data-kit.md) — newer note: schema-first generator for demo datasets.
- [Faker (joke2k/faker)](./07-faker.md) — review + usage notes for the offline row-generation library.
- [langextract — deferred](./08-langextract.md) — what it could offer a future `extract`; why it's not in v1.

## Source

Primary guidance adapted from internal AWS ProServe AI/ML Practice synthetic-data
guidance, supplemented with notes relevant to this project (Bedrock-based PDF
synthesis, PII redaction).
