# Quality, Representativeness, Realism & Coverage

Synthetic data should be statistically similar to real data. Deviation of the
synthetic distribution from the real one is a major risk to model performance.
A careful sampling process helps, but representativeness should still be
verified after generation. Raise this with the customer early if real data is
being replaced. For high-dimensional datasets, verification gets complex —
consult customer SMEs before choosing an approach.

## Quality metrics for representativeness

The ultimate test is **model performance**: train on synthetic data, test on
real data, evaluate against jointly-agreed metrics. This is expensive (requires
training the full model), so cheaper statistical-similarity measures are useful:

1. **Histogram & cross-plot similarity** — for tabular data, plot feature
   histograms and cross plots; visible overlap between real and synthetic ⇒
   good representation.
2. **Kullback–Leibler (KL) divergence** — measure between empirical
   distributions of real and synthetic data. Zero ⇒ matching distributions.
   For discrete P, Q: `D_KL(P||Q) = Σ_x P(x) · log(P(x)/Q(x))`.
3. **Pairwise Correlation Difference (PCD)** — how well synthetic data captures
   inter-feature correlation. Frobenius norm of the difference of the Pearson
   correlation matrices: `PCD(X_R, X_S) = ||C_R − C_S||_F`.
4. **Log Clustering Metric (LCM)** — how well synthetic data captures cluster
   structure [6]. Combine both datasets, cluster with k-means (k components):
   `LCM = log( (1/k) · Σ_j (n_{j,R}/n_j − c)^2 )`, where `c = n_R/(n_R + n_S)`
   is the real fraction, `n_j` is total points in cluster j, and `n_{j,R}` is
   real points in cluster j.
5. **Autocorrelation structure** — for sequential data, correlation of a signal
   with a lagged version of itself; should match between real and synthetic.
   `R(k) = Σ (X_t − μ)(X_{t+k} − μ) / Σ (X_t − μ)^2`.

## Validation at inference

Monitor whether incoming production data stays representative of the synthetic
training data (related to data drift, but can be worsened by wrong assumptions
about training distributions):

- **Bounds checks** — log whether production data falls within synthetic-data
  bounds (e.g. via CloudWatch Log filtering). Simple and effective first check
  for root-causing bad predictions; ignores feature relationships and varying
  feature importance.
- **Outlier detection** — flag production points as outliers vs. synthetic
  training data (e.g. SageMaker Random Cut Forest).
- **Euclidean distance** — measure dissimilarity of a production point relative
  to inter-point distances in the synthetic set (e.g. via SageMaker Model
  Monitor data-quality jobs).
- **Predicted-probability consistency** — for classifiers, near-tied class
  probabilities can indicate uncertainty / dissimilar input.
- **Training vs. production accuracy** — given similar distributions, production
  test accuracy should be within a margin of training/validation accuracy; a gap
  warrants a deep dive into the synthetic distributions.

## Data realism

Check that generated data doesn't violate real-world constraints on format,
range, and order relationships:

- No negative ages; valid latitude/longitude ranges (applies to labels too).
- Correct character length and formatting for credit-card numbers, dates,
  street addresses.
- Respect known order relationships (e.g. min ≤ max selling price of a house).
- Realism also matters for textual and audio data.

## Coverage

Generally better to have synthetic data cover the range of real data. But too
wide a range yields records unlikely in reality that add little utility; as
those points become influential to the model fit, production performance can
degrade.

## Overfitting

When training on synthetic data and testing on real data, a performance drop
can come from **overfitting** as well as distributional deviation.
Cross-validate to minimize overfitting risk. If the customer won't share real
test data, request the **test error** — the gap between training and test error
indicates overfitting.

## Speed

Generation and validation can be time-consuming. Discuss expectations on scale,
quality, fairness, and time up front so these costs are in the engagement
budget. Estimate the average cost to generate one new sample early. After a
first round, analyze the impact of training-data size on performance, estimate
the value of additional samples, and weigh that against their cost.

## Publicly available datasets

When using public datasets, the data scientist doesn't control synthesis (the
customer may also supply public/synthetic sets), so run **data-quality checks
before training**. When several public datasets exist, assess fitness for the
use case before choosing. A repository of open data across verticals is
available on AWS.
