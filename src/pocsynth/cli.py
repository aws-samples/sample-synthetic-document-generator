# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Typer CLI entry point for `pocsynth`.

Human mode is the default. `--json` produces the stable envelope defined
in output.py. All stdout writes must route through output.emit / emit_ndjson;
ruff T201 enforces no bare print() in this package.
"""

from __future__ import annotations

import json as _json
import logging
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from pocsynth import __version__
from pocsynth.aws import resolve_region
from pocsynth.bedrock import DEFAULT_MAX_TOKENS, DEFAULT_MODEL, MODELS, make_session
from pocsynth.comprehend import scan_for_pii
from pocsynth.core import ConversionConfig, run_conversion
from pocsynth.errors import (
    AuthError,
    DataInvalidError,
    DocSynthError,
    InputError,
    SchemaError,
)
from pocsynth.output import emit, emit_ndjson, envelope, error_envelope, ndjson_event
from pocsynth.pricing import (
    actual_convert_cost,
    estimate_bedrock_cost,
    estimate_convert_cost,
    load_pricing,
)

# Whether human mode should also write the JSON envelope to stdout.
# - If stdout is a pipe/redirect (non-TTY), yes — agents/scripts rely on it.
# - If stdout is a terminal (TTY), no — the Rich summary on stderr is enough.
# - POCSYNTH_NO_STDOUT_JSON=1 forces suppression regardless (for recordings
#   like VHS/ttyd where stdout looks non-TTY but a human is watching).
_STDOUT_IS_TTY = sys.stdout.isatty()
_FORCE_QUIET_STDOUT = os.environ.get("POCSYNTH_NO_STDOUT_JSON") == "1"

app = typer.Typer(
    name="pocsynth",
    help="PoC synthetic-document generator. Convert PDFs to synthetic HTML/Markdown via Amazon Bedrock.",
    no_args_is_help=True,
    rich_markup_mode=None,
)

# stderr console for human logs / progress; stdout NEVER gets human output
# when --json is on (see output.emit).
_stderr = Console(stderr=True, highlight=False, soft_wrap=True)


class ModelChoice(str, Enum):
    sonnet = "sonnet"
    opus = "opus"
    haiku = "haiku"


class FormatChoice(str, Enum):
    html = "html"
    markdown = "markdown"


class ModeChoice(str, Enum):
    synthetic = "synthetic"
    real = "real"


class DataFormatChoice(str, Enum):
    csv = "csv"
    json = "json"


class ExtractFormatChoice(str, Enum):
    json = "json"
    csv = "csv"
    jsonl = "jsonl"


class DistributionChoice(str, Enum):
    auto = "auto"
    infer = "infer"
    synthetic = "synthetic"
    uniform = "uniform"


class EstimateTargetChoice(str, Enum):
    convert = "convert"
    extract = "extract"
    schema = "schema"


# ---------- global options (shared) ----------


_HELP_JSON = "Emit machine-readable JSON on stdout."
_HELP_STREAM = "With --json, emit NDJSON progress events before the final complete event."
_HELP_QUIET = "Suppress stderr logs (errors still go to stderr in human mode)."
_HELP_LOGLEVEL = "One of DEBUG / INFO / WARNING / ERROR."
_HELP_INTERACTIVE = "Prompt for missing values (human only)."
_HELP_PROFILE = "AWS profile to use. Overrides AWS_PROFILE."
_HELP_REGION = "AWS region. Defaults to AWS_REGION or us-east-1."


@app.callback()
def _root(
    ctx: typer.Context,
    json_mode: Annotated[bool, typer.Option("--json", help=_HELP_JSON)] = False,
    stream: Annotated[bool, typer.Option("--stream", help=_HELP_STREAM)] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help=_HELP_QUIET)] = False,
    log_level: Annotated[str, typer.Option("--log-level", help=_HELP_LOGLEVEL)] = "WARNING",
    interactive: Annotated[bool, typer.Option("--interactive", help=_HELP_INTERACTIVE)] = False,
    profile: Annotated[str | None, typer.Option("--profile", help=_HELP_PROFILE)] = None,
    region: Annotated[str | None, typer.Option("--region", help=_HELP_REGION)] = None,
) -> None:
    if stream and not json_mode:
        raise typer.BadParameter("--stream requires --json")
    if interactive and json_mode:
        raise typer.BadParameter("--interactive cannot be used with --json")

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if quiet:
        logging.getLogger().setLevel(logging.ERROR)

    ctx.ensure_object(dict)
    ctx.obj.update(
        json_mode=json_mode,
        stream=stream,
        quiet=quiet,
        interactive=interactive,
        profile=profile,
        region=region,
    )


# ---------- internal helpers ----------


def _emit_ok(ctx: typer.Context, command: str, result: dict[str, Any]) -> None:
    obj = envelope(command, result)
    if ctx.obj["json_mode"]:
        emit(obj, json_mode=True)
    else:
        _emit_human(command, obj)


def _emit_err(ctx: typer.Context, command: str, exc: DocSynthError) -> None:
    obj = error_envelope(command, exc)
    if ctx.obj["json_mode"]:
        emit(obj, json_mode=True)
    else:
        # human mode: log to stderr
        _stderr.print(f"[red]error:[/] {exc.message}")
        if exc.hint:
            _stderr.print(f"[yellow]hint:[/] {exc.hint}")
        if exc.context:
            _stderr.print(f"[dim]context:[/] {_json.dumps(exc.context)}")


def _emit_human(command: str, obj: dict[str, Any]) -> None:
    """Pretty-print success envelope to stderr for humans.

    If stdout is piped / redirected (not a TTY), also write the JSON envelope
    there so `pocsynth convert … | jq` and `pocsynth convert … > out.json`
    keep working without an explicit --json flag. On a TTY stdout stays silent
    — the Rich summary is all the viewer needs.
    """
    _stderr.print(f"[green]✓[/] {command} ok")
    result = obj.get("result") or {}
    if command == "convert" and "output" in result:
        out = result["output"]
        _stderr.print(f"  combined: [bold]{out.get('combined_path')}[/]")
        _stderr.print(
            f"  pages: {out.get('pages_processed')}/{out.get('pages_attempted')} "
            f"in {out.get('wall_time_seconds')}s"
        )
        bu = out.get("bedrock_usage", {})
        _stderr.print(
            f"  tokens: in={bu.get('input_tokens', 0)} out={bu.get('output_tokens', 0)}"
        )
        cost = result.get("cost")
        if cost:
            _stderr.print(
                f"  cost: [bold]${cost.get('total_cost_usd', 0):.4f}[/] "
                f"(Bedrock ${cost.get('bedrock', {}).get('total_cost_usd', 0):.4f} "
                f"+ Comprehend ${(cost.get('comprehend') or {}).get('cost_usd', 0):.4f})"
            )
    elif command == "estimate":
        _stderr.print(
            f"  pages: {result.get('pages')}  model: [bold]{result.get('bedrock', {}).get('model')}[/]"
        )
        bedrock = result.get("bedrock", {})
        comp = result.get("comprehend") or {}
        _stderr.print(
            f"  tokens (est): in={bedrock.get('input_tokens', 0)} "
            f"out={bedrock.get('output_tokens', 0)}"
        )
        _stderr.print(
            f"  cost (est): [bold]${result.get('total_cost_usd', 0):.4f}[/] "
            f"(Bedrock ${bedrock.get('total_cost_usd', 0):.4f}"
            + (f" + Comprehend ${comp.get('cost_usd', 0):.4f}" if comp else "")
            + ") · [dim]heuristic, ±30-50%[/]"
        )
    if not _STDOUT_IS_TTY and not _FORCE_QUIET_STDOUT:
        emit(obj, json_mode=True)


def _wrap(ctx: typer.Context, command: str, fn):
    try:
        result = fn()
        _emit_ok(ctx, command, result)
        raise typer.Exit(0)
    except DocSynthError as e:
        _emit_err(ctx, command, e)
        raise typer.Exit(e.exit_code) from e
    except typer.Exit:
        raise
    except Exception as e:  # pragma: no cover - defence in depth
        wrapped = DocSynthError(str(e), context={"exception": type(e).__name__})
        _emit_err(ctx, command, wrapped)
        raise typer.Exit(1) from e


# ---------- convert ----------


@app.command()
def convert(
    ctx: typer.Context,
    pdf: Annotated[str, typer.Argument(help="PDF path or https:// URL.")],
    model: Annotated[ModelChoice, typer.Option("--model")] = ModelChoice.sonnet,
    fmt: Annotated[FormatChoice, typer.Option("--format", "-f")] = FormatChoice.html,
    mode: Annotated[ModeChoice, typer.Option("--mode")] = ModeChoice.synthetic,
    pages: Annotated[int | None, typer.Option("--pages", help="Max pages per document.")] = None,
    num_docs: Annotated[int, typer.Option("--num-docs", help="Number of synthetic docs to generate.")] = 1,
    pii_audit: Annotated[
        bool, typer.Option("--pii-audit/--no-pii-audit", help="Run Comprehend PII audit.")
    ] = True,
    redact_values: Annotated[
        bool,
        typer.Option("--redact-values", help="Store [REDACTED] instead of raw PII in audit CSV."),
    ] = False,
    max_tokens: Annotated[int, typer.Option("--max-tokens")] = DEFAULT_MAX_TOKENS,
    system_prompt: Annotated[str | None, typer.Option("--system-prompt")] = None,
    output_dir: Annotated[str | None, typer.Option("--output-dir", "-o")] = None,
) -> None:
    """Convert a PDF to synthetic HTML/Markdown."""
    ctx.ensure_object(dict)
    stream = ctx.obj.get("stream", False)
    json_mode = ctx.obj.get("json_mode", False)

    cfg = ConversionConfig(
        pdf_url=pdf,
        model_key=model.value,
        export_format=fmt.value,
        synthetic=(mode is ModeChoice.synthetic),
        system_prompt_user=system_prompt or "",
        num_pages=pages,
        num_docs=num_docs,
        pii_audit=pii_audit,
        redact_values=redact_values,
        max_tokens=max_tokens,
        region=ctx.obj.get("region"),
        profile=ctx.obj.get("profile"),
        output_dir=output_dir,
    )

    def on_event(event_name: str, **payload):
        if stream and json_mode:
            emit_ndjson(ndjson_event(event_name, "convert", **payload))

    def _run_and_cost(event_cb):
        result = run_conversion(cfg, on_event=event_cb)
        try:
            pricing = load_pricing()
            region, _src = resolve_region(cfg.region, cfg.profile)
            result["cost"] = actual_convert_cost(
                result, pricing, model_key=cfg.model_key, region=region
            )
        except DocSynthError as exc:
            # If pricing is broken, don't fail the whole convert — convert
            # succeeded. Record the reason so callers can distinguish
            # 'no cost reported' from 'cost computation failed'.
            result["cost"] = None
            result.setdefault("warnings", []).append(
                f"cost computation failed ({exc.code}): {exc.message}"
            )
        return result

    # Human mode gets a Rich progress spinner routed to stderr.
    if not json_mode:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=_stderr,
            transient=True,
        ) as progress:
            task = progress.add_task("Converting…", total=None)

            def human_event(event_name: str, **payload):
                if event_name == "page_processed":
                    progress.update(
                        task, description=f"page {payload.get('page')}/{payload.get('of')}"
                    )
                on_event(event_name, **payload)

            _wrap(ctx, "convert", lambda: _run_and_cost(human_event))
    else:
        _wrap(ctx, "convert", lambda: _run_and_cost(on_event))


# ---------- estimate ----------


@app.command()
def estimate(
    ctx: typer.Context,
    target_path: Annotated[
        str,
        typer.Argument(
            help="PDF path (convert/extract) or sample/schema path (--for schema). "
                 "Offline; URLs are not fetched."
        ),
    ],
    for_: Annotated[
        EstimateTargetChoice, typer.Option("--for", help="Which paid command to estimate.")
    ] = EstimateTargetChoice.convert,
    model: Annotated[ModelChoice, typer.Option("--model")] = ModelChoice.sonnet,
    pages: Annotated[int | None, typer.Option("--pages", help="Page cap.")] = None,
    pii_audit: Annotated[
        bool, typer.Option("--pii-audit/--no-pii-audit", help="Include PII audit in estimate.")
    ] = True,
) -> None:
    """Pre-flight cost estimate (offline, no AWS calls) for convert / extract / schema.

    Heuristic-based: expect ±30-50% error. Use the command's returned `cost` for
    exact numbers after a run.
    """
    from pocsynth.pricing import estimate_extract_cost, estimate_schema_infer_cost

    def _go() -> dict[str, Any]:
        path = Path(target_path)
        if not path.exists():
            raise InputError(
                f"File not found: {target_path}",
                context={"path": target_path},
                hint="estimate runs offline; provide a local path.",
            )
        pricing = load_pricing()
        region, _src = resolve_region(ctx.obj.get("region"), ctx.obj.get("profile"))
        if for_ is EstimateTargetChoice.extract:
            result = estimate_extract_cost(
                path, model.value, pricing, pages=pages, pii_audit=pii_audit, region=region
            )
        elif for_ is EstimateTargetChoice.schema:
            result = estimate_schema_infer_cost(path, model.value, pricing, region=region)
        else:
            result = estimate_convert_cost(
                path, model.value, pricing, pages=pages, pii_audit=pii_audit, region=region
            )
        if result.get("warnings"):
            for w in result["warnings"]:
                _stderr.print(f"[yellow]warning:[/] {w}")
        return result

    _wrap(ctx, "estimate", _go)


# ---------- pii-audit ----------


@app.command(name="pii-audit")
def pii_audit_cmd(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="Local text / HTML / MD file to scan.")],
    redact_values: Annotated[bool, typer.Option("--redact-values")] = False,
) -> None:
    """Re-scan an existing local file with Amazon Comprehend (no Bedrock)."""
    def _go() -> dict[str, Any]:
        if not file.exists():
            raise InputError(
                f"File not found: {file}",
                context={"path": str(file)},
                hint="Provide an existing local text / HTML / Markdown file",
            )
        text = file.read_text(encoding="utf-8", errors="replace")
        session = make_session(
            profile=ctx.obj.get("profile"), region=ctx.obj.get("region")
        )
        comprehend = session.client("comprehend")
        audit_dir = file.parent / "pii-audit"
        filename_stem = file.stem
        detected = scan_for_pii(
            text,
            folder_name=str(audit_dir),
            filename=filename_stem,
            comprehend=comprehend,
            redact_values=redact_values,
        )
        return {
            "input": {"path": str(file)},
            "pii_audit": {
                "enabled": True,
                "path": str(audit_dir / f"{filename_stem}_pii_scan_audit.csv"),
                "redacted": redact_values,
                "entities_found": len(detected),
            },
        }

    _wrap(ctx, "pii-audit", _go)


# ---------- models ----------


@app.command()
def models(ctx: typer.Context) -> None:
    """List available Bedrock models."""
    def _go() -> dict[str, Any]:
        return {
            "models": [
                {"name": name, **info}
                for name, info in MODELS.items()
            ],
            "default": DEFAULT_MODEL,
        }

    json_mode = ctx.obj.get("json_mode", False)
    if not json_mode:
        table = Table(title="Bedrock Models")
        table.add_column("name")
        table.add_column("id")
        table.add_column("context window", justify="right")
        table.add_column("description")
        for name, info in MODELS.items():
            marker = " (default)" if name == DEFAULT_MODEL else ""
            table.add_row(
                f"{name}{marker}",
                info["id"],
                f"{info['context_window']:,}",
                info["description"],
            )
        _stderr.print(table)
    _wrap(ctx, "models", _go)


# ---------- doctor ----------


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Run real environment + AWS probes. Intended as the first command an agent calls."""
    def _go() -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        # 1. Python version
        import platform
        checks.append(
            {
                "name": "python",
                "ok": True,
                "detail": platform.python_version(),
            }
        )

        # 2. boto3 + pymupdf versions
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version
        checks.append({"name": "boto3", "ok": True, "detail": _pkg_version("boto3")})
        try:
            checks.append(
                {"name": "pymupdf", "ok": True, "detail": _pkg_version("pymupdf")}
            )
        except PackageNotFoundError as exc:
            checks.append(
                {"name": "pymupdf", "ok": False, "detail": f"not installed: {exc}"}
            )

        profile = ctx.obj.get("profile")
        region, region_source = resolve_region(ctx.obj.get("region"), profile)
        checks.append(
            {
                "name": "region",
                "ok": True,
                "detail": region,
                "source": region_source,
                "profile": profile,
            }
        )

        session = make_session(profile=profile, region=region)

        # 3. STS caller identity
        try:
            sts = session.client("sts")
            ident = sts.get_caller_identity()
            checks.append(
                {
                    "name": "sts_caller_identity",
                    "ok": True,
                    "detail": ident.get("Arn"),
                    "account": ident.get("Account"),
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "name": "sts_caller_identity",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )

        # 4. Minimal Bedrock Converse
        try:
            bedrock = session.client("bedrock-runtime")
            bedrock.converse(
                modelId=MODELS[DEFAULT_MODEL]["id"],
                messages=[{"role": "user", "content": [{"text": "Reply with exactly: OK"}]}],
                inferenceConfig={"maxTokens": 5, "temperature": 0},
            )
            checks.append({"name": "bedrock_converse", "ok": True, "detail": f"{DEFAULT_MODEL} reachable"})
        except Exception as exc:
            checks.append(
                {
                    "name": "bedrock_converse",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )

        # 5. Minimal Comprehend DetectPiiEntities
        try:
            comp = session.client("comprehend")
            comp.detect_pii_entities(Text="hello world", LanguageCode="en")
            checks.append({"name": "comprehend_detect_pii", "ok": True, "detail": "reachable"})
        except Exception as exc:
            checks.append(
                {
                    "name": "comprehend_detect_pii",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )

        all_ok = all(c.get("ok") for c in checks)
        if not all_ok:
            # doctor uses a dedicated exit path: it returns a result that
            # indicates partial/failed checks without raising a DocSynthError.
            # Convert to an explicit error envelope so the exit code maps.
            # We use AuthError when auth-class checks failed, otherwise generic.
            failed_names = [c["name"] for c in checks if not c.get("ok")]
            if "sts_caller_identity" in failed_names:
                raise AuthError(
                    "Doctor detected AWS auth failure",
                    context={"checks": checks, "failed": failed_names},
                    hint="Run `aws sts get-caller-identity` or `aws sso login` to validate credentials",
                )
            raise DocSynthError(
                "Doctor detected environment failure",
                context={"checks": checks, "failed": failed_names},
            )

        return {"checks": checks, "all_ok": all_ok}

    _wrap(ctx, "doctor", _go)


# ---------- version ----------


@app.command()
def version(ctx: typer.Context) -> None:
    """Print pocsynth version."""
    _wrap(ctx, "version", lambda: {"version": __version__})


# ---------- generate (free, offline) ----------


@app.command()
def generate(
    ctx: typer.Context,
    schema: Annotated[str | None, typer.Option("--schema", help="Schema JSON path.")] = None,
    preset: Annotated[str | None, typer.Option("--preset", help="Bundled preset name.")] = None,
    rows: Annotated[int, typer.Option("--rows", help="Number of rows to generate.")] = 100,
    fmt: Annotated[DataFormatChoice, typer.Option("--format", "-f")] = DataFormatChoice.csv,
    seed: Annotated[int | None, typer.Option("--seed", help="Deterministic seed.")] = None,
    locale: Annotated[str, typer.Option("--locale")] = "en_US",
    output_dir: Annotated[str | None, typer.Option("--output-dir", "-o")] = None,
) -> None:
    """Generate synthetic rows from a schema or preset (offline, free)."""
    from pocsynth import presets as presets_mod
    from pocsynth.generate import GenerateConfig, run_generation
    from pocsynth.schema import load_schema

    stream = ctx.obj.get("stream", False)
    json_mode = ctx.obj.get("json_mode", False)

    def _go() -> dict[str, Any]:
        if bool(schema) == bool(preset):
            raise SchemaError(
                "provide exactly one of --schema or --preset",
                hint="e.g. --preset b2b_saas  OR  --schema my_schema.json",
            )
        schema_dict = presets_mod.load_preset(preset) if preset else load_schema(schema)

        def on_event(name: str, **payload):
            if stream and json_mode:
                emit_ndjson(ndjson_event(name, "generate", **payload))

        return run_generation(
            GenerateConfig(
                schema=schema_dict, rows=rows, export_format=fmt.value,
                seed=seed, locale=locale, output_dir=output_dir,
            ),
            on_event=on_event,
        )

    _wrap(ctx, "generate", _go)


# ---------- test (free, offline validation) ----------


@app.command(name="test")
def test_cmd(
    ctx: typer.Context,
    rows: Annotated[str, typer.Option("--rows", help="Path to generated rows (CSV/JSON).")],
    schema: Annotated[str, typer.Option("--schema", help="Schema JSON path.")],
    in_format: Annotated[
        DataFormatChoice | None,
        typer.Option("--in-format", help="Override format (else inferred from extension)."),
    ] = None,
) -> None:
    """Validate generated rows against a schema (offline). Exit 7 if invalid."""
    from pocsynth.schema import load_schema
    from pocsynth.validate import ValidateConfig, run_validation

    def _go() -> dict[str, Any]:
        schema_dict = load_schema(schema)
        result = run_validation(
            ValidateConfig(
                rows_path=rows, schema=schema_dict,
                in_format=in_format.value if in_format else None,
            )
        )
        if not result["valid"]:
            raise DataInvalidError(
                f"{len(result['violations'])} rows/fields violate the schema",
                context={"report": result},
                hint="Inspect result.context.report.violations",
            )
        return result

    _wrap(ctx, "test", _go)


# ---------- schema (lint mode here; infer/from-prompt added in Slice 2) ----------


@app.command()
def schema(
    ctx: typer.Context,
    from_schema: Annotated[
        str | None, typer.Option("--from-schema", help="Lint/document an existing schema (offline).")
    ] = None,
    from_sample: Annotated[
        str | None, typer.Option("--from-sample", help="Infer a schema from an extract sample (paid).")
    ] = None,
    from_prompt: Annotated[
        str | None, typer.Option("--from-prompt", help="Infer a schema from a description (paid).")
    ] = None,
    model: Annotated[ModelChoice, typer.Option("--model")] = ModelChoice.sonnet,
    fix: Annotated[bool, typer.Option("--fix", help="Apply autofixable lint issues.")] = False,
    distribution: Annotated[
        DistributionChoice, typer.Option("--distribution")
    ] = DistributionChoice.auto,
    max_tokens: Annotated[int, typer.Option("--max-tokens")] = DEFAULT_MAX_TOKENS,
    output_dir: Annotated[str | None, typer.Option("--output-dir", "-o")] = None,
) -> None:
    """Generate, document, and lint a schema. Lint mode is offline; infer /
    from-prompt call Bedrock."""
    from pocsynth.schemagen import SchemaConfig, run_schema

    sources = [s for s in (from_schema, from_sample, from_prompt) if s]
    json_mode = ctx.obj.get("json_mode", False)
    stream = ctx.obj.get("stream", False)

    def _go() -> dict[str, Any]:
        if len(sources) != 1:
            raise SchemaError(
                "provide exactly one of --from-schema / --from-sample / --from-prompt",
            )

        def on_event(name: str, **payload):
            if stream and json_mode:
                emit_ndjson(ndjson_event(name, "schema", **payload))

        result = run_schema(
            SchemaConfig(
                in_schema_path=from_schema, sample_path=from_sample, prompt=from_prompt,
                model_key=model.value, fix=fix, distribution=distribution.value,
                max_tokens=max_tokens, output_dir=output_dir,
                region=ctx.obj.get("region"), profile=ctx.obj.get("profile"),
            ),
            on_event=on_event,
        )
        # Paid modes (infer/from-prompt) report bedrock_usage → price it.
        usage = result.get("output", {}).get("bedrock_usage")
        if usage:
            try:
                pricing = load_pricing()
                region, _src = resolve_region(ctx.obj.get("region"), ctx.obj.get("profile"))
                result["cost"] = estimate_bedrock_cost(
                    model.value, usage["input_tokens"], usage["output_tokens"],
                    pricing, region=region,
                )
            except DocSynthError as exc:
                result["cost"] = None
                result.setdefault("warnings", []).append(
                    f"cost computation failed ({exc.code}): {exc.message}"
                )
        return result

    _wrap(ctx, "schema", _go)


# ---------- presets (free, offline) ----------


@app.command()
def presets(ctx: typer.Context) -> None:
    """List bundled preset schemas."""
    from pocsynth import presets as presets_mod

    json_mode = ctx.obj.get("json_mode", False)

    def _go() -> dict[str, Any]:
        return {"presets": presets_mod.list_presets()}

    if not json_mode:
        from pocsynth import presets as presets_mod
        table = Table(title="Preset Schemas")
        table.add_column("name")
        table.add_column("description")
        for p in presets_mod.list_presets():
            table.add_row(p["name"], p["description"])
        _stderr.print(table)
    _wrap(ctx, "presets", _go)


# ---------- extract (paid, Bedrock) ----------


@app.command()
def extract(
    ctx: typer.Context,
    pdf: Annotated[str, typer.Argument(help="PDF path or https:// URL.")],
    schema: Annotated[
        str | None,
        typer.Option("--schema", help="Conform to this schema (else discovery mode)."),
    ] = None,
    model: Annotated[ModelChoice, typer.Option("--model")] = ModelChoice.sonnet,
    fmt: Annotated[ExtractFormatChoice, typer.Option("--format", "-f")] = ExtractFormatChoice.json,
    pages: Annotated[int | None, typer.Option("--pages", help="Max pages.")] = None,
    max_tokens: Annotated[int, typer.Option("--max-tokens")] = DEFAULT_MAX_TOKENS,
    pii_audit: Annotated[
        bool, typer.Option("--pii-audit/--no-pii-audit", help="Audit extracted values for PII.")
    ] = True,
    output_dir: Annotated[str | None, typer.Option("--output-dir", "-o")] = None,
) -> None:
    """Extract structured records (conform) or field observations (discovery) from a PDF."""
    from pocsynth.extract import ExtractConfig, run_extraction
    from pocsynth.schema import load_schema

    stream = ctx.obj.get("stream", False)
    json_mode = ctx.obj.get("json_mode", False)

    def _go() -> dict[str, Any]:
        schema_dict = load_schema(schema) if schema else None

        def on_event(name: str, **payload):
            if stream and json_mode:
                emit_ndjson(ndjson_event(name, "extract", **payload))

        result = run_extraction(
            ExtractConfig(
                pdf_url=pdf, schema=schema_dict, model_key=model.value,
                export_format=fmt.value, num_pages=pages, max_tokens=max_tokens,
                pii_audit=pii_audit, region=ctx.obj.get("region"),
                profile=ctx.obj.get("profile"), output_dir=output_dir,
            ),
            on_event=on_event,
        )
        try:
            pricing = load_pricing()
            region, _src = resolve_region(ctx.obj.get("region"), ctx.obj.get("profile"))
            result["cost"] = actual_convert_cost(
                result, pricing, model_key=model.value, region=region
            )
        except DocSynthError as exc:
            result["cost"] = None
            result.setdefault("warnings", []).append(
                f"cost computation failed ({exc.code}): {exc.message}"
            )
        return result

    _wrap(ctx, "extract", _go)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
