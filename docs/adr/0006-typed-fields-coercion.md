# Field `type` is a closed set; validation is coercion-based; dates are ISO-8601 in both formats

**Status:** accepted (2026-06-07)

`type` is a fixed enum — `string | integer | number | boolean | date | datetime`
— enforced by `_validate_schema_shape`, not a free-form hint. It does double duty
(generation hint + validation rule), so it must have precise semantics.

Three coupled decisions make the generate→test round-trip robust **by
construction** rather than by luck:

1. **Coercion-based validation, one path.** `test` validates every value with a
   single `coerce_and_check(cell, type)` primitive: attempt to parse the value to
   its declared type (ISO date/datetime, int with no fractional part, float, bool
   set; string accepts anything; empty/None is nullable). Coercion is a no-op for
   already-typed JSON values, so **CSV and JSON take the identical path** — a CSV
   `"42"` and a JSON `42` both coerce to integer and agree.
2. **Canonical serialization, one writer.** `generate` and `extract` both emit
   values through `serialize(value, type, fmt)`: dates/datetimes as ISO-8601
   strings in **both** CSV and JSON (JSON has no date type); integer/number/boolean
   native in JSON, `str()` in CSV; None → `""` in CSV, `null` in JSON.
3. **Null convention.** Empty CSV cell = `null`; v1 treats all fields as nullable
   (no `required`), so null/empty always validates. A future `required: true` is
   additive.

**Why:** CSV (the `generate` default) erases the int-vs-string distinction JSON
preserves, so naive per-format validation would need two code paths that can
silently diverge and flake the keystone round-trip test. A closed type set with
shared serialize/coerce helpers collapses that to one path and one writer.

**Consequences:** the round-trip property test runs across every type in **both**
formats. Adding a type means extending `FIELD_TYPES` + the serialize/coerce tables
together. Depends on ADR-0003 (the pipeline whose generate/test stages share these
helpers).
