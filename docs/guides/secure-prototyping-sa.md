# Secure prototyping in 1 command — for the AWS SA (sandbox)

You're building a prototype **for** a customer, in **your** sandbox, and you must
never touch their real data. You need believable, domain-shaped data *now*.

## The one command

```bash
pocsynth run --preset crm_contacts --rows 1000 --seed 42 -o ./out
```

That's it. `run` chains the whole pipeline — load a preset schema → generate rows
— and writes `./out/rows.csv`. It is **free, offline, and instant**: no Bedrock
call, no AWS credentials, no cost gate to clear.

Pick the vertical that fits the engagement:

| Preset | Shape |
|---|---|
| `b2b_saas` | accounts: company, plan tier, MRR, seats, region |
| `ecommerce_orders` | orders: SKU, category, qty, amount, channel, status |
| `crm_contacts` | contacts: name, email, company, title, lead source, stage |
| `insurance_claims` | claims: policy, type, state, amount, status, channel |
| `utility_meter` | smart-meter reads: service, consumption, voltage, quality |
| `loyalty_pos` | retail POS: tier, department, basket, amount, points |
| `ad_campaign` | campaign-day: channel, impressions, clicks, conversions, spend |
| `knowledge_corpus` | KB articles (RAG seed): title, body, category, audience |
| `security_telemetry` | auth events: user, source IP, event type, geo, risk score |
| `healthcare_lite` | patient intake (synthetic): name, DOB, state, plan, MRN |

`pocsynth presets` lists them all.

## Don't see your domain? Describe it.

```bash
pocsynth run --prompt "a logistics company's shipment tracking events with \
carrier, origin/destination, weight, status, and delivery SLA" --rows 5000 -o ./out
```

This path makes **one small Bedrock call** (~pennies) to design the schema, then
generates for free. Because the seed is a *description* — not real data — there is
nothing real to leak: the output is **synthetic by construction.**

> A paid path is cost-gated. If the projected spend is above ~$0.10 the command
> stops and asks you to confirm with `--yes`. For a prompt it's well under that.

## Your guarantee

**Preset and prompt seeds never see customer data**, so the generated dataset is
safe to share by construction. There is no real source, so `run` reports the
verify verdict as `not_applicable` and the data is **cleared for sharing**.

Determinism: `--seed` makes the output byte-reproducible — hand a teammate the
same command and they get the same rows.

Prefer to click first? `pocsynth ui` lets you compose a dataset with pills, and
every preview prints the **equivalent `run` command** (CLI + agent-skill forms) —
a quick way to show a customer the exact command line and copy it into a demo.

## When you *do* have a real document

If the customer hands you a real sample and you need to mirror its exact shape,
that's the **Customer-runner** path — it adds extraction + a fail-closed safety
check. See [secure-prototyping-customer.md](./secure-prototyping-customer.md).

---
*Related: [ADR-0011 one-shot safe-by-default](../adr/0011-one-shot-safe-by-default.md),
[ADR-0005 PII guard](../adr/0005-pii-guard.md).*
