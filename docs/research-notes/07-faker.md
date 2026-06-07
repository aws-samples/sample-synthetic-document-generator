# Faker (joke2k/faker) — Review & Usage Notes

[`joke2k/faker`](https://github.com/joke2k/faker) — MIT, Python 3.8+. Generates
fake data for testing, seeding databases, anonymizing data, and stress tests.
Inspired by PHP/Perl/Ruby Faker.

> Why it matters here: Faker is the **free, offline row generator** in our plan
> ([`../plan/structured-data-support.md`](../plan/structured-data-support.md)) —
> the half that turns one extracted/authored schema into unlimited synthetic
> rows with no Bedrock call. These notes focus on the features that plan relies
> on: deterministic seeding, regex/enum constraints, locales, custom providers.

## Install

```bash
pip install Faker        # or: uv add "faker>=37,<38"  (pin tight — locale data shifts across minors)
```

## Basics — the `Faker()` proxy

```python
from faker import Faker
fake = Faker()

fake.name()       # 'Lucy Cechtelar'
fake.address()
fake.email()
fake.text()
```

Each call returns a new random value. Properties are grouped into **providers**;
calling `fake.<method>()` forwards to `Generator.format("<method>")` — which is
exactly how a schema-driven generator dispatches dynamically (see "Schema-driven
dispatch" below).

## Seeding — the key to reproducible datasets

| Call | Scope | Use when |
|---|---|---|
| `Faker.seed(4321)` | **Class-level / shared** RNG across all instances | Quick global determinism in a script |
| `fake.seed_instance(4321)` | **This instance only** (own `random.Random`) | **Preferred for us** — safe under concurrency, no global state |
| `fake.seed_locale('en_US', 0)` | One locale's generator | Multi-locale fine control |

```python
fake = Faker()
fake.seed_instance(42)
[fake.name() for _ in range(3)]   # identical on every run with seed 42
```

> ⚠️ Results are **not guaranteed across Faker versions** (locale data changes).
> Pin the version and prefer structural assertions (regex match, length, set
> membership) over hardcoded golden strings in tests. The plan keeps ≤1–2 golden
> values for this reason.
>
> Note: the instance method `fake.seed()` is **disabled** (raises `TypeError`) —
> use `Faker.seed()` (class) or `fake.seed_instance()` (instance).

## Constraints we need: enum and regex

```python
# enum  → weighted or uniform choice from a fixed set
fake.random_element(elements=["CA", "NY", "TX"])
fake.random_element(elements=OrderedDict([("CA", 0.5), ("NY", 0.3), ("TX", 0.2)]))  # weighted

# regex → values matching a pattern (great for IDs / formatted codes)
fake.regexify(r"MRN-[0-9]{6}")     # e.g. 'MRN-481923'
fake.bothify("???-####")           # letters/digits template
fake.numerify("###-###")
```

This maps directly onto the plan's schema fields: `enum` → `random_element`,
`regex` → `regexify`.

## Unique values

```python
names = [fake.unique.first_name() for _ in range(500)]
assert len(set(names)) == len(names)
fake.unique.clear()                # reset seen-values pool
```

- Different arg signatures keep separate uniqueness pools.
- Raises `UniquenessException` after too many failed attempts (the value space is
  exhausted) — relevant if `--rows` exceeds the cardinality of a unique field.
- Only hashable values; `fake.unique.profile()` (returns dict) raises `TypeError`.
- Multi-locale: subscript first → `fake.unique["en_US"].first_name()`.

Useful for primary-key-ish columns; watch the cardinality-vs-row-count trap.

## Locales

```python
fake = Faker("it_IT")                      # localized data
fake = Faker(["it_IT", "en_US", "ja_JP"])  # multi-locale (v3+)
fake.locales                                # ['it_IT', 'en_US', 'ja_JP']
fake["en_US"].name()                        # pin one locale; KeyError if not included
```

Constructor `locale` accepts a string, list/tuple/set, or an `OrderedDict` of
`locale -> weight` for weighted blending across locales. Defaults to `en_US`.

## Custom providers (extending the catalog)

```python
from faker.providers import BaseProvider

class MRNProvider(BaseProvider):
    def medical_record_number(self) -> str:
        return self.numerify("MRN-######")

fake.add_provider(MRNProvider)
fake.medical_record_number()
```

**Dynamic provider** — enum-style values from an external list without a class:

```python
from faker.providers import DynamicProvider

professions = DynamicProvider(
    provider_name="medical_profession",
    elements=["dr.", "doctor", "nurse", "surgeon", "clerk"],
)
fake.add_provider(professions)
fake.medical_profession()
```

For this project, domain-specific fields the schema can't express with built-in
providers become small `BaseProvider` subclasses — kept in code, registered once.

## Schema-driven dispatch (how `generate.py` will use it)

Because `fake.<method>()` is just `format(name)`, a schema can name the provider
as a string and the generator resolves a callable per field — **validating the
provider name up front** so a bad schema fails fast (the plan's `SchemaError`
listing valid providers) rather than blowing up mid-generation:

```python
def resolve(field, fake):
    name = field["faker"]                       # e.g. "name", "ssn", "date_of_birth"
    if not hasattr(fake, name):
        raise SchemaError(f"unknown faker provider: {name!r}")
    method = getattr(fake, name)
    args = field.get("faker_args", {})          # e.g. {"minimum_age": 18}
    if "enum" in field:
        return lambda: fake.random_element(elements=field["enum"])
    if "regex" in field:
        return lambda: fake.regexify(field["regex"])
    return lambda: method(**args)
```

Mirror the plan's field semantics: `faker` + `faker_args` for typed values,
`enum` for fixed sets, `regex` for formatted strings.

## Performance

- Constructor `use_weighting=True` (default) makes common values (e.g. frequent
  names) appear more often, matching real-world frequency. Set `False` for
  **uniform** selection and **faster** generation — useful for large bulk
  exports where realism of frequency doesn't matter.
- One shared `random.Random` by default; `seed_instance` isolates state per
  instance (safer for parallel generation).

## CLI (handy for spiking, not for the product)

```bash
faker address
faker -l de_DE address
faker profile ssn,birthdate
faker -r=3 -s=";" name           # repeat 3, ';' separator
faker -i my_pkg.my_provider my_method   # import a custom provider
```

## Fit for this project — summary

| Need (from the plan) | Faker feature |
|---|---|
| Free, offline row generation (no AWS) | Entire library is local/deterministic |
| Reproducible datasets (`--seed`) | `fake.seed_instance(seed)` |
| Typed field values | `name/address/ssn/date_of_birth/email/...` providers + `faker_args` |
| Fixed sets (`enum`) | `random_element(elements=...)` |
| Formatted IDs (`regex`) | `regexify(pattern)` / `bothify` / `numerify` |
| Locale-specific data | `Faker(locale)` / `Faker([...])` |
| Domain fields beyond the catalog | `BaseProvider` subclass / `DynamicProvider` |
| Unique key columns | `fake.unique.<method>()` (mind cardinality) |
| Bulk-export speed | `use_weighting=False` |

**Gap to design around:** Faker has **no cross-row / foreign-key awareness** —
each value is independent (the same gap seen in
[`../plan/review-metabase-dataset-generator.md`](../plan/review-metabase-dataset-generator.md)).
Referential integrity for future nested `tables` must be handled by *our*
generator (generate parents → reference real parent IDs in children), not Faker.

## Source

- Repo: <https://github.com/joke2k/faker>
- Docs: <https://faker.readthedocs.io> (Faker proxy class, Standard Providers)
