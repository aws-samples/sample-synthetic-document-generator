# Generation Methods & Sampling

All methods — even those needing no existing data — require an understanding of
how the real data behaves. Methods fall into two broad categories, useful for
framing customer conversations.

## A. Business knowledge as a prerequisite (no data required)

Random sampling from probability distributions. Existing data isn't required,
but using it as a starting point helps ensure the assumed distributions mimic
reality. Customer SMEs should define the distributions and any feature
interactions.

- No fixed rule for which distribution is "right." Common choices:
  - **Poisson / Negative Binomial** — discrete and event-count data
  - **Weibull** — time-to-failure problems
  - **Bernoulli** — binary outcomes
  - **Gaussian / Uniform** — general continuous cases
- Prefer comparison to real data whenever it is or becomes available.
- Run a **sensitivity analysis** on distribution attributes (e.g. larger/smaller
  variance or expected value than assumed) to expose model weaknesses before
  production use.
- **Open-source data** can substitute for customer-owned data if it meets needs;
  lets the team build pipelines without production data.

## B. Example data as a prerequisite (existing data required)

- **Data augmentation** — real data exists but is insufficient for a
  generalizable model. Augment training data; commonly train on real + synthetic
  simultaneously.
- **Data replacement** — real data must not be used (PII/compliance), so replace
  the real dataset entirely with synthetic data.

### Generation techniques

- Image augmentation: rotate, crop, flip, shear, change brightness
- SMOTE (Synthetic Minority Oversampling Technique)
- Generative Adversarial Networks (GANs)
- Variational Auto-encoders (VAEs)
- Bootstrap sampling

## Sampling process

No one-size-fits-all; choose by use case, application, and data type.

- **Tabular data:** empirically estimate the PDF of observed data and sample new
  points. Consult the customer on the correct family of parameterized
  distributions; consider heavy-tailedness, variance, etc.
  - Multidimensional data is rarely sampled per-column independently unless the
    features are known to be independent. Capture relationships via **joint
    density estimation** — but beware the **curse of dimensionality** when real
    data is scarce.
  - **Bayesian networks** can learn complex relationships involving categorical
    data.
  - Structured generation often needs simultaneous sampling of continuous +
    discrete variables with multimodality and class imbalance — use advanced
    methods like **CTGAN** (Conditional GAN for tabular data) [1].
- **Sequential data:** HMMs, Autoregressive models (AR/ARIMA), RNNs. For a
  GAN-based sequential generator, **DoppelGANger** [2].
- **Open-source libraries:** the **Synthetic Data Vault (SDV)** [3] generates
  synthetic tabular and sequential data from real data without the data leaving
  AWS systems. Supports single-table, multi-table relational databases, and
  multivariate time series.
- **Image / text:** sampling from simple statistical distributions is usually
  infeasible. Use VAEs and GANs (training GANs can be expensive). 3D image data
  can be generated with **Blender** [4].
- **Numerical simulators:** when inputs measure natural phenomena with known
  mathematical models — e.g. FEM simulators for material behavior, seasonal
  forecast models for weather. The customer supplies the simulation models.

### Checklist when generating data (non-exhaustive)

- Does the synthetic data cover the range of values of the real data?
- Does generation preserve heavy-tailedness, variance, etc.?
- Are there real-data outliers that should be excluded from synthetic data?
- Are there rare events under-sampled in real data that should be represented?
- Should the dataset preserve class-membership probabilities?
- Are there constraints the data must satisfy?
- What approach measures similarity of synthetic vs. true data?

## Data size

Choose target size carefully based on use case, training-data variance, number
of features, minority-class representation, fairness considerations (if
demographic variables are present), and model capacity. Low-capacity models
(e.g. logistic regression) saturate quickly as data grows.

If generation requires real data, also determine the **minimum real-data size**
the customer must provide — dependent on variance, number of classes,
multimodality, imbalance, and use case. As feature count rises, required size
rises; density estimation in high dimensions suffers the curse of
dimensionality (MSE convergence rate for multivariate KDE in d dimensions is of
order O(n^(-4/(4+d)))) [5].

### Estimating coverage of the feature space

1. For each feature, determine min and max possible values.
2. For each continuous feature, pick a reasonable interval to discretize it
   (e.g. home value in $5,000 buckets).
3. With all features discrete, compute the permutation of all feature
   combinations → an estimate of how much data covers the space.

**Risks:** includes combinations unlikely in reality (wasted training time —
prune them); assuming equal bin likelihood loses the real-world distribution.
