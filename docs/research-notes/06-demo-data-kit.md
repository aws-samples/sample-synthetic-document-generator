# Multipurpose Demo Data Kit (schema-first)

For a **general multipurpose** demo dataset, prefer a **schema-first generator**
with a few reusable business domains over a realism-maximizing synthetic-data
pipeline. That gives fast iteration, clean joins, and believable stories across
dashboards, SQL, APIs, and agent demos.

> This is a newer note: a pragmatic, demo-oriented complement to the
> realism/representativeness guidance in
> [03-quality-and-representativeness.md](./03-quality-and-representativeness.md).
> Demos optimize for *narrative and speed*; production training data optimizes
> for *statistical fidelity*.

## Recommended pattern

Build one reusable "demo data kit" with a core schema that works across many
scenarios:

- `organizations`
- `users`
- `products`
- `orders`
- `subscriptions`
- `support_tickets`
- `events`
- `payments`

Then add **scenario overlays** — SaaS, e-commerce, marketplace, customer
support — by changing distributions and a few domain-specific columns instead of
inventing a whole new dataset each time. Schema-driven generators are well-suited
to dynamic generation from field definitions and types.

## Recommended stack

| Layer | Use | Best fit |
|---|---|---|
| Base row generation | Fast, controllable demo records | Faker / schema-based generators built on Faker.js / Python Faker |
| Database-scale mock data | Bulk rows for dev/demo databases | DBeaver mock data generator (major RDBMS, large volumes) |
| Statistical realism from seed data | Realistic look-alikes from real sample tables | SDV (single- and multi-table synthesis from learned patterns) |
| LLM-generated text fields | Descriptions, ticket summaries, notes, comments | An LLM layer on top of schema generation (e.g. Hugging Face Synthetic Data Generator — aimed more at text/chat/classification than generic business tables) |

## What makes it good for demos

Demo data should be **narrative-rich**, not just random. Good demo data has
consistent foreign keys, time progression, a few obvious outliers, and business
situations explainable in one sentence:

- "Enterprise accounts have higher ACV."
- "Refunds spike after a release."
- "Priority tickets correlate with churn risk."

Make it **parameterized** by:

- **Row counts** — 1k to 1M.
- **Industry flavor** — SaaS, retail, fintech, healthcare-lite.
- **Noise level** — clean vs. messy.
- **Time horizon** — 30 days, 12 months, 3 years.
- **Story mode** — normal, growth spike, incident week, fraud burst.

## Suggested starter schema

```
organizations(org_id, name, segment, region, created_at)
users(user_id, org_id, name, title, email, status, created_at)
products(product_id, category, sku, price, margin_pct)
orders(order_id, org_id, user_id, order_date, amount, status, channel)
subscriptions(sub_id, org_id, plan, mrr, start_date, renewal_date, status)
support_tickets(ticket_id, org_id, user_id, severity, category, opened_at, resolved_at, csat)
events(event_id, user_id, event_type, ts, device, source)
payments(payment_id, order_id, method, amount, success, processed_at)
```

Broad enough to demo BI, SQL joins, operational workflows, CRM-ish views, and
agent reasoning over customer/account context. SDV's multi-table support is
useful later for synthesizing more realistic related tables from seed data.

## Recommendation

Start with a **config-driven generator**: YAML/JSON schema in, CSV/JSON/Parquet
out, plus optional LLM text enrichment for a few columns (ticket summaries,
sales notes). Fastest path for polished demos, while leaving the door open to
add SDV later when you want learned distributions instead of handcrafted ones.

### Practical v1 scope

- Schema definitions
- Relationship rules
- Scenario presets
- Deterministic seeds
- Export to CSV, JSON, SQL inserts, and Parquet

## References

- DBeaver — Mock Data Generation: <https://dbeaver.com/docs/dbeaver/Mock-Data-Generation/>
- fake-data-generator-from-schema: <https://github.com/hasinhayder/fake-data-generator-from-schema>
- Generating datasets dynamically from schema: <https://stackoverflow.com/questions/53552983/how-to-generate-datasets-dynamically-based-on-schema>
- SDV: <https://github.com/sdv-dev/sdv>
- Hugging Face — Synthetic Data Generator: <https://huggingface.co/blog/synthetic-data-generator>
- K2view — What is synthetic data generation: <https://www.k2view.com/what-is-synthetic-data-generation/>
