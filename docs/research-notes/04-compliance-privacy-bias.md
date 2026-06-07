# Compliance, Privacy & Bias

## Data generation process and compliance

If generation requires protected data subject to privacy laws, ensure the
process complies with applicable guidelines:

- Do not accidentally transmit or expose PHI / personal information; leaking or
  downloading sensitive data is a key security concern.
- It may be worthwhile to **scan the data for compliance** after generation.
- If a dataset is **partially synthetic**, all usual rules for protected
  information still apply.

## Bias & fairness

When synthetic data trains a production AI/ML application, watch for bias and
fairness:

- Synthetic data may **amplify existing bias** in the real records. Check
  demographic data and its correlations/associations with life outcomes before
  use.
- Inspect dataset **composition**. E.g. a healthcare dataset over-representing a
  race, gender, or income group will focus on the symptoms/outcomes of that
  group.
- **Amazon SageMaker Clarify** can examine specified attributes to detect bias.
  The AWS whitepaper on fairness and explainability describes metrics for
  quantifying bias and fairness.
- Customers may adopt synthetic data specifically to **reduce algorithmic
  bias**. Some jurisdictions also require a legally mandated level of
  transparency in how models are trained — keep clear documentation of the
  synthetic-data generation process.

## Differential privacy

If the motivation for synthetic data is privacy, use **differentially private
generation**:

- Ensures actual values in the original dataset are never revealed while
  carrying over the same statistical properties [7].
- Preferred over simple anonymization, since anonymized data can retain
  quasi-identifiers usable to re-identify individuals.
- **TensorFlow Privacy** enables differentially private training so the model
  doesn't memorize individual data points [8].
- DP generation processes (**DPGAN** [9], **DP-CTGAN**) can produce datasets
  that don't accidentally contain any original points [10] — such a dataset can
  be shared openly without privacy leaks.
- **Trade-off:** DP algorithms trade **privacy for utility** — these are often
  conflicting goals.

## Relevance to this repo

This project synthesizes documents (e.g. PDF → synthetic HTML/Markdown via
Bedrock) and audits for PII (e.g. Amazon Comprehend). The guidance above maps to
two recurring concerns here:

- **PII / PHI handling** — scan generated artifacts; treat partially-synthetic
  output as still subject to protection rules.
- **Realism vs. leakage** — generated documents must look realistic
  (format/range/order constraints) without reproducing real source values.
