# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline synthetic-row generation via Faker.

`run_generation(cfg)` turns a validated schema into N rows, deterministically
when seeded, touching no AWS. This is the free half of the pipeline.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from faker import Faker

from pocsynth import schema as schema_mod
from pocsynth.errors import SchemaError

EventCallback = Callable[..., None] | None


def _noop(*_a, **_k) -> None:
    pass


# A compact, deterministic regex->string generator (Faker 37 has no regexify).
# Supports the common ID-pattern subset: literals, escapes (\d \w \s \. etc.),
# char classes [...] with ranges and negation, the `.` wildcard, alternation at
# the top level, and quantifiers {n}, {n,m}, ?, +, *. Uses the supplied seeded
# `rng` (Faker's random.Random) so output stays deterministic.
_RX_CLASS = {
    "d": "0123456789",
    "w": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    "s": " ",
    "D": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_",
    "W": " -",
    "S": "abcdefghijklmnopqrstuvwxyz0123456789",
}
_RX_ANY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _resolve_groups(pattern: str, rng) -> str:
    """Resolve `(a|b|c)` and `(?:a|b)` groups by picking one alternative.

    Handles one level of (possibly alternating) groups — enough for the ID-style
    patterns schemas use. Nested groups are resolved outer-first by repeated
    passes. Unbalanced parens are left as literals (the caller tolerates them).
    """
    for _ in range(8):  # bounded passes; avoids pathological nesting loops
        start = pattern.find("(")
        if start == -1:
            break
        depth = 0
        end = -1
        for k in range(start, len(pattern)):
            if pattern[k] == "(":
                depth += 1
            elif pattern[k] == ")":
                depth -= 1
                if depth == 0:
                    end = k
                    break
        if end == -1:
            break  # unbalanced; leave the rest alone
        inner = pattern[start + 1:end]
        if inner.startswith("?:"):
            inner = inner[2:]
        choice = rng.choice(inner.split("|")) if "|" in inner else inner
        pattern = pattern[:start] + choice + pattern[end + 1:]
    return pattern


def _regexify(pattern: str, rng) -> str:
    # Resolve groups first, then top-level alternation.
    pattern = _resolve_groups(pattern, rng)
    if "|" in pattern:
        pattern = rng.choice(pattern.split("|"))

    out: list[str] = []
    i, n = 0, len(pattern)

    def pool_at(j: int) -> tuple[str, int]:
        """Return (character pool, next index) for the token starting at j."""
        ch = pattern[j]
        if ch == "\\" and j + 1 < n:
            nxt = pattern[j + 1]
            return _RX_CLASS.get(nxt, nxt), j + 2
        if ch == "[":
            k = pattern.find("]", j + 1)
            if k == -1:  # unterminated class → treat '[' as a literal
                return ch, j + 1
            body = pattern[j + 1:k]
            neg = body.startswith("^")
            if neg:
                body = body[1:]
            chars: list[str] = []
            m = 0
            while m < len(body):
                if m + 2 < len(body) and body[m + 1] == "-":
                    # Tolerate reversed ranges (e.g. [9-0]) by ordering the ends.
                    lo_o, hi_o = ord(body[m]), ord(body[m + 2])
                    if lo_o > hi_o:
                        lo_o, hi_o = hi_o, lo_o
                    chars.extend(chr(c) for c in range(lo_o, hi_o + 1))
                    m += 3
                else:
                    chars.append(body[m])
                    m += 1
            if neg:
                allchars = set(_RX_ANY)
                chars = sorted(allchars - set(chars)) or list(_RX_ANY)
            # A class that resolved to nothing (shouldn't happen now) falls back
            # to the generic pool so generation never picks from an empty set.
            return ("".join(chars) or _RX_ANY), k + 1
        if ch == ".":
            return _RX_ANY, j + 1
        # literal
        return ch, j + 1

    while i < n:
        if pattern[i] in "^$":
            i += 1
            continue
        pool, j = pool_at(i)
        is_literal = (j == i + 1 and pattern[i] not in ".") and pattern[i] not in "[\\"
        # quantifier?
        count = 1
        if j < n and pattern[j] in "{?+*":
            q = pattern[j]
            if q == "{":
                k = pattern.find("}", j)
                spec = pattern[j + 1:k] if k != -1 else ""
                # A literal '{' (no closing '}' or non-numeric spec) is treated
                # as a literal brace, not a quantifier.
                if k == -1 or not re.fullmatch(r"\d*,?\d*", spec) or spec in ("", ","):
                    out.append("{")
                    i = j + 1
                    continue
                if "," in spec:
                    lo_s, hi_s = spec.split(",")
                    lo = int(lo_s) if lo_s else 0
                    hi = int(hi_s) if hi_s else lo + 4
                    if lo > hi:  # tolerate reversed bounds e.g. x{5,2}
                        lo, hi = hi, lo
                    count = rng.randint(lo, hi)
                else:
                    count = int(spec)
                j = k + 1
            elif q == "?":
                count = rng.randint(0, 1)
                j += 1
            elif q == "+":
                count = rng.randint(1, 5)
                j += 1
            elif q == "*":
                count = rng.randint(0, 5)
                j += 1
        for _ in range(count):
            if is_literal and len(pool) == 1:
                out.append(pool)
            elif pool:
                out.append(rng.choice(pool))
            # else: empty pool → emit nothing rather than crash (defensive;
            # pool_at never returns "" now, but keep generation crash-proof).
        i = j
    return "".join(out)


@dataclass
class GenerateConfig:
    schema: dict[str, Any]
    rows: int = 100
    export_format: str = "csv"  # "csv" | "json"
    seed: int | None = None
    locale: str = "en_US"
    output_dir: str | None = None


@lru_cache(maxsize=8)
def valid_faker_providers(locale: str = "en_US") -> frozenset[str]:
    """Real Faker data-provider method names (constant per locale; memoized).

    Scans the registered *provider classes* (`fake.get_providers()`), NOT the
    proxy's `dir()`. The proxy exposes control methods — `format`, `parse`,
    `seed`, `seed_instance`, `add_provider`, `get_providers`, … — which are
    callable but are not data generators; admitting them would let a schema
    bind a field to `format` (crashes at generation) or `seed_instance`
    (silently re-seeds mid-run, breaking determinism). Provider classes expose
    only the data methods, so this is the correct allowlist.
    """
    fake = Faker(locale)
    names: set[str] = set()
    for provider in fake.get_providers():
        for attr in dir(provider):
            if attr.startswith("_"):
                continue
            try:
                if callable(getattr(provider, attr)):
                    names.add(attr)
            except (AttributeError, TypeError):
                continue
    return frozenset(names)


def _enum_generator(fake: Faker, values: list, weights: dict | None) -> Callable[[], Any]:
    if weights:
        ordered = OrderedDict((v, float(weights.get(v, 0.0))) for v in values)
        total = sum(ordered.values())
        if total > 0:
            norm = OrderedDict((k, v / total) for k, v in ordered.items())
            return lambda: fake.random_element(elements=norm)
    return lambda: fake.random_element(elements=values)


def _regex_generator(fake: Faker, pattern: str) -> Callable[[], Any]:
    return lambda: _regexify(pattern, fake.random)


def _faker_generator(method: Callable, args: dict) -> Callable[[], Any]:
    return lambda: method(**args)


def _resolve_field_generators(
    schema: dict[str, Any], fake: Faker
) -> list[tuple[str, str, Callable[[], Any]]]:
    """Return (name, type, generator) per field. Validates providers up front."""
    providers = valid_faker_providers(fake.locales[0] if fake.locales else "en_US")
    resolved: list[tuple[str, str, Callable[[], Any]]] = []
    for field in schema["fields"]:
        name = field["name"]
        ftype = field["type"]

        if "enum" in field:
            resolved.append((name, ftype,
                             _enum_generator(fake, list(field["enum"]), field.get("weights"))))
            continue

        if "regex" in field:
            resolved.append((name, ftype, _regex_generator(fake, field["regex"])))
            continue

        faker_name = field.get("faker")
        if faker_name:
            if faker_name not in providers:
                raise SchemaError(
                    f"field {name!r}: unknown Faker provider {faker_name!r}",
                    context={"field": name, "provider": faker_name},
                    hint="Run `pocsynth schema --from-schema <file>` to lint, "
                         "or pick a valid Faker provider",
                )
            args = field.get("faker_args", {}) or {}
            resolved.append((name, ftype, _faker_generator(getattr(fake, faker_name), args)))
            continue

        # No binding: type-appropriate default.
        gen = _default_generator(ftype, fake)
        resolved.append((name, ftype, gen))
    return resolved


def _default_generator(ftype: str, fake: Faker) -> Callable[[], Any]:
    return {
        "string": fake.word,
        "integer": lambda: fake.random_int(min=0, max=1000),
        "number": lambda: round(fake.pyfloat(min_value=0, max_value=1000), 2),
        "boolean": fake.boolean,
        "date": fake.date_object,
        "datetime": fake.date_time,
    }.get(ftype, fake.word)


def run_generation(cfg: GenerateConfig, on_event: EventCallback = None) -> dict[str, Any]:
    emit = on_event or _noop
    schema_mod._validate_schema_shape(cfg.schema)
    if cfg.rows < 0:
        raise SchemaError("rows must be >= 0", context={"rows": cfg.rows})

    fake = Faker(cfg.locale)
    if cfg.seed is not None:
        fake.seed_instance(cfg.seed)

    generators = _resolve_field_generators(cfg.schema, fake)
    names = [n for n, _t, _g in generators]

    start = time.monotonic()
    emit("generation_started", rows=cfg.rows, fields=len(names))
    rows: list[dict[str, Any]] = []
    for i in range(cfg.rows):
        row = {name: gen() for name, _ftype, gen in generators}
        rows.append(row)
        if cfg.rows and (i + 1) % 1000 == 0:
            emit("rows_generated", done=i + 1, of=cfg.rows)

    parent = Path(cfg.output_dir) if cfg.output_dir else Path.cwd()
    parent.mkdir(parents=True, exist_ok=True)

    if cfg.export_format == "json":
        rows_path = parent / "rows.json"
        serial = [
            {name: schema_mod.serialize(row[name], ftype, "json")
             for name, ftype, _g in generators}
            for row in rows
        ]
        rows_path.write_text(json.dumps(serial, indent=2), encoding="utf-8")
    else:
        rows_path = parent / "rows.csv"
        with open(rows_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=names)
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    name: schema_mod.serialize(row[name], ftype, "csv")
                    for name, ftype, _g in generators
                })

    wall = round(time.monotonic() - start, 3)
    emit("generation_complete", rows_written=len(rows), path=str(rows_path))
    return {
        "input": {
            "schema": cfg.schema.get("name"),
            "rows": cfg.rows,
            "format": cfg.export_format,
            "seed": cfg.seed,
            "locale": cfg.locale,
        },
        "output": {
            "dir": str(parent),
            "rows_path": str(rows_path),
            "rows_written": len(rows),
            "wall_time_seconds": wall,
        },
        "cost": None,
        "warnings": [],
    }


def stream_rows(
    schema: dict[str, Any],
    rows: int,
    *,
    export_format: str = "csv",
    seed: int | None = None,
    locale: str = "en_US",
):
    """Yield a dataset as text chunks without materializing it in memory.

    Used by the web UI's /download so the row count is bounded only by patience,
    not RAM. Generates one row at a time and yields serialized text:
      - csv  → a header line, then one CSV line per row
      - json → a streamed JSON array (`[`, comma-separated objects, `]`)
    Deterministic when seeded, identical field semantics to run_generation.
    """
    schema_mod._validate_schema_shape(schema)
    if rows < 0:
        raise SchemaError("rows must be >= 0", context={"rows": rows})

    fake = Faker(locale)
    if seed is not None:
        fake.seed_instance(seed)
    generators = _resolve_field_generators(schema, fake)
    names = [n for n, _t, _g in generators]

    if export_format == "json":
        yield "[\n"
        for i in range(rows):
            obj = {
                name: schema_mod.serialize(gen(), ftype, "json")
                for name, ftype, gen in generators
            }
            yield ("  " + json.dumps(obj)) + (",\n" if i < rows - 1 else "\n")
        yield "]\n"
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=names, lineterminator="\n")
        writer.writeheader()
        yield buf.getvalue()
        for _ in range(rows):
            buf.seek(0)
            buf.truncate(0)
            writer.writerow({
                name: schema_mod.serialize(gen(), ftype, "csv")
                for name, ftype, gen in generators
            })
            yield buf.getvalue()
