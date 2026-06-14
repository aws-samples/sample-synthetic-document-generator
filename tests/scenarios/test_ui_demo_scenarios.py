# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The same three SA demo-data scenarios, driven through the FastAPI + HTMX UI.

These exercise the demo UI as a thin layer over the core: the endpoints must call
the same pipeline functions and surface the same guarantees as the CLI —
crucially, the Scenario-1 PII non-leak guarantee must hold through the web path
too.

The UI's paid Bedrock/Comprehend clients are injected via FastAPI dependency
overrides (the app exposes `get_bedrock_client` / `get_comprehend_client`
dependencies precisely so tests and the real app can swap them). Preset/preview
paths that don't spend tokens run fully offline.

Skipped until the `[ui]` extra + module exist (see importorskip guards).
"""

from __future__ import annotations

import json

import pytest

from .conftest import (
    CUSTOMER_PII_VALUES,
    UI_AVAILABLE,
    UI_SKIP_REASON,
    bedrock_schema_stub,
    comprehend_stub,
)

pytestmark = pytest.mark.skipif(not UI_AVAILABLE, reason=UI_SKIP_REASON)

if UI_AVAILABLE:
    from fastapi.testclient import TestClient

    from pocsynth.ui.app import create_app, get_bedrock_client, get_comprehend_client


@pytest.fixture
def client():
    return TestClient(create_app())


def _override(app, *, bedrock=None, comprehend=None):
    if bedrock is not None:
        app.dependency_overrides[get_bedrock_client] = lambda: bedrock
    if comprehend is not None:
        app.dependency_overrides[get_comprehend_client] = lambda: comprehend


# --------------------------------------------------------------------------- #
# _compose_prompt — pills → NL prompt (time-series is OPTIONAL, scenario-tuned)
# --------------------------------------------------------------------------- #
class TestComposePrompt:
    def test_flat_record_set_has_no_time_series_framing(self):
        from pocsynth.ui.app import _compose_prompt
        p = _compose_prompt("insurance claims", "RAG eval corpus",
                            "one-big-table", "medium").lower()
        # The record type carries the domain; no forced calendar framing.
        assert "insurance claims" in p
        assert "time series" not in p
        assert "granularity" not in p
        assert "trend" not in p
        # Scenario guidance is injected (RAG → retrieval).
        assert "retrieval" in p

    def test_time_series_adds_period_granularity_trend(self):
        from pocsynth.ui.app import _compose_prompt
        p = _compose_prompt("telemetry events", "load testing", "one-big-table",
                            "high", "a time series", "last 90 days", "hourly", "spike").lower()
        assert "time series" in p
        assert "last 90 days" in p and "hourly" in p
        assert "spike trend" in p
        # Scenario guidance is injected (load testing → high-volume).
        assert "high-volume" in p

    def test_scenario_guidance_varies(self):
        from pocsynth.ui.app import _compose_prompt
        agent = _compose_prompt("support tickets", "agent building",
                                "one-big-table", "medium").lower()
        bench = _compose_prompt("support tickets", "model benchmarking",
                                "one-big-table", "medium").lower()
        assert "branchable" in agent
        assert "difficulty" in bench
        assert agent != bench


# --------------------------------------------------------------------------- #
# Page + pill sentence
# --------------------------------------------------------------------------- #
class TestPageAndPills:
    def test_index_renders_pill_sentence(self, client):
        r = client.get("/")
        assert r.status_code == 200
        # The Metabase-style fill-in-the-blank sentence (with pills) is present.
        assert "row dataset of" in r.text.lower()
        assert 'class="pill' in r.text
        assert "healthz" not in r.text

    def test_rows_inputs_default_passes_html_validation(self, client):
        # Regression: a number input's value must satisfy (value - min) % step == 0
        # or the browser rejects it ("not in range"). With step=100/1000 the
        # default 1000 was invalid; rows is a plain count, so step must be 1.
        import re
        html_pages = [client.get("/").text]
        # the download form (in a preview) carries the second rows input
        _override(client.app, bedrock=bedrock_schema_stub(
            schema_fields=[{"name": "x", "type": "string", "faker": "word"}]))
        html_pages.append(client.post(
            "/preview", data={"record_type": "orders", "rows": "1000"}).text)
        rows_inputs = []
        for page in html_pages:
            for m in re.finditer(r'<input[^>]*name="rows"[^>]*>', page):
                rows_inputs.append(m.group(0))
        assert rows_inputs, "no rows number inputs found"
        for tag in rows_inputs:
            value = int(re.search(r'value="(\d+)"', tag).group(1))
            mn = int(re.search(r'min="(\d+)"', tag).group(1))
            st = int(re.search(r'step="(\d+)"', tag).group(1))
            assert value >= mn and (value - mn) % st == 0, (
                f"rows default {value} fails HTML validation (min={mn} step={st}): {tag}")

    def test_index_renders_record_type_and_scenario_pills(self, client):
        # The pills are keyed on record type (the domain) and scenario (workload),
        # aligned to the SIM use cases — not an industry pill.
        r = client.get("/")
        assert 'name="record_type"' in r.text and 'name="scenario"' in r.text
        assert ">support tickets<" in r.text and ">insurance claims<" in r.text
        assert ">RAG eval corpus<" in r.text and ">agent building<" in r.text
        # The old industry framing is gone.
        assert 'name="business"' not in r.text

    def test_index_time_series_clause_is_optional(self, client):
        # Time framing is optional: the period/granularity/trend sub-clause is
        # hidden until "a time series" is chosen.
        r = client.get("/")
        assert 'name="time_shape"' in r.text
        assert 'id="series-clause"' in r.text and "hidden" in r.text
        assert "function toggleSeries" in r.text

    def test_index_carries_the_worked_example(self, client):
        r = client.get("/")
        # The worked example is now a SIM-representative support-ticket / triage
        # agent dataset (embedded for the "load the worked example" button).
        assert "triage agent" in r.text.lower()
        assert "csat" in r.text.lower()

    def test_healthz(self, client):
        assert client.get("/healthz").status_code == 200

    def test_index_makes_no_pii_safe_claim(self, client):
        # The UI must not label the upload path "PII-safe" or claim real values
        # are "barred from"/"never reach" the output — detection is best-effort.
        r = client.get("/")
        text = r.text
        assert "PII-safe" not in text
        assert "barred from output" not in text
        assert "never reach the output" not in text

    def test_index_has_no_cost_language(self, client):
        # Cost/price framing was removed in favor of "runs locally" wording.
        r = client.get("/")
        text = r.text
        assert "$" not in text
        assert "pennies" not in text
        assert "cost nothing" not in text
        assert "runs locally" in text

    def test_index_warns_document_is_sent_to_aws(self, client):
        # The upload pane carries an up-front data-egress warning naming the
        # AWS services that receive the document (Comprehend + Bedrock).
        r = client.get("/")
        text = r.text
        assert "Amazon Comprehend" in text and "Amazon Bedrock" in text
        assert "your AWS account" in text
        # The "Match a document" tab no longer claims safety.
        assert "Match a document" in text
        # Path-accurate disclosure: a sample of the real VALUES (not just the
        # field structure) is sent to Bedrock — value_counts egress in /preview.
        assert "sample of their actual values" in text


# --------------------------------------------------------------------------- #
# Scenario A — compose with pills (the default utility path)
# --------------------------------------------------------------------------- #
class TestPillCompose:
    def test_pills_compose_prompt_and_preview(self, client):
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "campaign_id", "type": "string", "regex": "C[0-9]{5}"},
            {"name": "channel", "type": "string", "enum": ["search", "social"]},
            {"name": "spend_usd", "type": "number", "faker": "pyfloat"},
        ]))
        r = client.post("/preview", data={
            "record_type": "support tickets", "scenario": "agent building",
            "shape": "one-big-table", "variation": "high",
            "time_shape": "a flat record set", "rows": "5000",
        })
        assert r.status_code == 200
        assert "campaign_id" in r.text
        # The download form carries the full requested row count, not a demo cap.
        assert 'value="5000"' in r.text

    def test_download_streams_full_count_uncapped(self, client):
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "id", "type": "integer", "faker": "random_int"},
            {"name": "tier", "type": "string", "enum": ["A", "B", "C"]},
        ]))
        client.post("/preview", data={"record_type": "orders", "rows": "100"})
        # Far above the old 1k demo cap — must stream the full set.
        r = client.post("/download", data={"rows": "25000", "format": "csv", "seed": "7"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        assert len(lines) == 25001  # header + 25,000 rows

    def test_download_json_full_count(self, client):
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "x", "type": "string", "faker": "word"}]))
        client.post("/preview", data={"record_type": "financial transactions", "rows": "10"})
        r = client.post("/download", data={"rows": "3000", "format": "json", "seed": "1"})
        assert r.status_code == 200
        rows = json.loads(r.text)
        assert len(rows) == 3000

    def test_download_filename_is_header_safe(self, client):
        # A model-generated schema name with a quote/CRLF must not corrupt or
        # inject the content-disposition header — it's slugified.
        app = client.app
        bedrock = bedrock_schema_stub(schema_fields=[
            {"name": "x", "type": "string", "faker": "word"}])
        # Force a hostile schema name through the stub.
        bedrock.converse.return_value["output"]["message"]["content"][0][
            "toolUse"]["input"]["name"] = 'evil"\r\nSet-Cookie: x=1'
        _override(app, bedrock=bedrock)
        client.post("/preview", data={"record_type": "orders", "rows": "10"})
        r = client.post("/download", data={"rows": "5", "format": "csv"})
        assert r.status_code == 200
        cd = r.headers["content-disposition"]
        # The CR/LF that would split the header is gone, and the quote that
        # would break out of filename="..." was slugified — so the only two
        # quotes are the wrapping pair. (Set-Cookie may survive as inert text.)
        assert "\r" not in cd and "\n" not in cd
        assert cd.count('"') == 2
        assert cd.startswith('attachment; filename="') and cd.endswith('.csv"')


# --------------------------------------------------------------------------- #
# Scenario B — describe your own dataset (custom prompt)
# --------------------------------------------------------------------------- #
class TestDescribeCustom:
    def test_custom_prompt_drives_schema(self, client):
        # On the custom tab (seed_mode=custom) the prompt drives the schema.
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "marker_field", "type": "string", "faker": "word"}]))
        r = client.post("/preview", data={
            "seed_mode": "custom",
            "prompt": "a marketplace with sellers, listings, and gross merchandise value",
            "record_type": "support tickets",  # pill default also submitted; custom mode wins
            "rows": "200",
        })
        assert r.status_code == 200
        assert "marker_field" in r.text


class TestSeedModeRouting:
    """Regression: a stale prompt in the hidden custom pane must NOT override the
    pills. The server routes on seed_mode (the active tab), not field-emptiness."""

    def test_pills_mode_ignores_stale_prompt(self, client):
        app = client.app
        captured = {}

        def _bedrock_capture():
            stub = bedrock_schema_stub(schema_fields=[
                {"name": "pills_field", "type": "string", "faker": "word"}])
            orig = stub.converse

            def _spy(**kw):
                # Record the prompt text Bedrock was asked to design from.
                captured["text"] = kw["messages"][0]["content"][0]["text"]
                return orig.return_value
            stub.converse.side_effect = _spy
            return stub

        _override(app, bedrock=_bedrock_capture())
        client.post("/preview", data={
            "seed_mode": "pills",
            "record_type": "insurance claims",
            # A stale, pre-filled-style prompt is still submitted by the form…
            "prompt": "A digital advertising platform's campaign performance dataset",
            "rows": "50",
        })
        # …but pills mode ignores it: the composed prompt is the insurance-claims
        # record type, not the advertising text.
        assert "insurance claims" in captured["text"].lower()
        assert "advertising" not in captured["text"].lower()


# --------------------------------------------------------------------------- #
# Command-equivalent panel — CLI + agent-skill commands shown with the preview
# --------------------------------------------------------------------------- #
class TestCommandPanel:
    def test_pills_mode_shows_cli_and_skill_run_commands(self, client):
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "x", "type": "string", "faker": "word"}]))
        r = client.post("/preview", data={
            "seed_mode": "pills", "record_type": "support tickets",
            "scenario": "agent building", "rows": "2000", "seed": "42"})
        assert r.status_code == 200
        t = r.text
        assert 'id="commands"' in t
        # CLI surface: the exact one-shot run command with the composed prompt + flags.
        assert "pocsynth run --prompt" in t
        assert "--rows 2000" in t and "--seed 42" in t and "-o ./out" in t and "--yes" in t
        # copy buttons present (one per surface)
        assert t.count('onclick="copyCmd(this)"') >= 2
        # Agent-skill surface is a NATURAL-LANGUAGE /pocsynth request — how you
        # actually invoke the skill — NOT the underlying ./pocsynth.py command.
        assert "/pocsynth generate" in t
        assert "./pocsynth.py" not in t
        # Usable from Kiro or Claude Code; not the synth-data skill.
        assert "Kiro" in t and "Claude Code" in t
        assert "synth-data" not in t

    def test_custom_mode_uses_the_user_prompt(self, client):
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "x", "type": "string", "faker": "word"}]))
        r = client.post("/preview", data={
            "seed_mode": "custom",
            "prompt": "a beekeeper's hive inspection log",
            "rows": "100", "seed": "5"})
        assert r.status_code == 200
        # The user's own prompt appears, shell-quoted (apostrophe handled).
        assert "beekeeper" in r.text
        assert "--rows 100" in r.text and "--seed 5" in r.text

    def test_document_mode_shows_run_document_placeholder(self, client, customer_pdf):
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(schema_fields=[
                {"name": "state", "type": "string", "enum": ["CA", "NY"]}]),
            comprehend=comprehend_stub(entities_per_call=[[]]),  # clean
        )
        with open(customer_pdf, "rb") as fh:
            r = client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "500", "seed": "1"})
        assert r.status_code == 200
        t = r.text
        assert "pocsynth run --document" in t
        # Skill surface is a natural-language /pocsynth request naming the file.
        assert "/pocsynth generate" in t
        assert "./pocsynth.py" not in t
        # The CLI fuller-extraction caveat is stated.
        assert "full Bedrock extraction" in t

    def test_command_is_shell_injection_safe(self, client):
        # A prompt with shell metacharacters must be single-quoted in the command,
        # not interpolated raw.
        app = client.app
        _override(app, bedrock=bedrock_schema_stub(schema_fields=[
            {"name": "x", "type": "string", "faker": "word"}]))
        r = client.post("/preview", data={
            "seed_mode": "custom", "prompt": "tickets; rm -rf / now", "rows": "10"})
        assert r.status_code == 200
        # Rendered (HTML-escaped) command wraps the whole prompt in single quotes.
        assert "&#x27;tickets; rm -rf / now&#x27;" in r.text

    def test_document_filename_is_shell_quoted(self):
        # A malicious/odd uploaded filename must be shell-quoted in the command so
        # a copy-paste can't run an injected command. (Unit-level: no Bedrock.)
        import shlex

        from pocsynth.ui.app import _build_run_command
        cmd = _build_run_command("document", prompt=None,
                                 document_name="x'; rm -rf ~ #.pdf", rows=5, seed=1)
        toks = shlex.split(cmd)
        i = toks.index("--document")
        assert toks[i + 1] == "x'; rm -rf ~ #.pdf"   # recovered intact, one token
        assert ";" not in toks                       # no stray command separator
        # The missing-filename placeholder needs no quoting.
        ph = _build_run_command("document", prompt=None, document_name=None, rows=5, seed=1)
        assert "--document <your-file.pdf>" in ph

    def test_skill_request_collapses_whitespace(self):
        # A multi-line custom prompt must render as a single-line /pocsynth request.
        from pocsynth.ui.app import _build_skill_request
        sr = _build_skill_request("custom", prompt="line1\nline2\twith   spaces",
                                  document_name=None, rows=10, seed=1)
        assert "\n" not in sr and "\t" not in sr
        assert sr.startswith("/pocsynth generate a synthetic dataset of line1 line2 with spaces")


# --------------------------------------------------------------------------- #
# Scenario C — match a real (PII-bearing) document, output stays clean
# --------------------------------------------------------------------------- #
class TestMatchDocument:
    def test_upload_audits_pii_and_output_has_no_real_values(self, client, customer_pdf):
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(schema_fields=[
                {"name": "full_name", "type": "string", "faker": "name", "pii": True},
                {"name": "ssn", "type": "string", "faker": "ssn", "pii": True},
                {"name": "state", "type": "string", "enum": ["CA", "NY"]},
            ]),
            comprehend=comprehend_stub(entities_per_call=[[{"Type": "NAME"}, {"Type": "SSN"}]]),
        )
        with open(customer_pdf, "rb") as fh:
            r = client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "300"},
            )
        assert r.status_code == 200
        assert "pii" in r.text.lower()
        for real in CUSTOMER_PII_VALUES:
            assert real not in r.text, f"real PII leaked into UI preview: {real!r}"

    def test_download_full_dataset_is_pii_free(self, client, customer_pdf):
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(schema_fields=[
                {"name": "full_name", "type": "string", "faker": "name", "pii": True},
                {"name": "state", "type": "string", "enum": ["CA", "NY"]},
            ]),
            comprehend=comprehend_stub(entities_per_call=[[{"Type": "NAME"}]]),
        )
        with open(customer_pdf, "rb") as fh:
            client.post(
                "/preview",
                files={"seed_document": ("customer_intake.pdf", fh, "application/pdf")},
                data={"rows": "1000"},
            )
        r = client.post("/download", data={"rows": "1000", "format": "csv", "seed": "42"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        for real in CUSTOMER_PII_VALUES:
            assert real not in r.text


# --------------------------------------------------------------------------- #
# Scenario 3 — SA describes the business in the UI text box (no document)
# --------------------------------------------------------------------------- #
class TestScenario3UIPromptSeeded:
    def test_describe_business_preview_renders_fields(self, client):
        app = client.app
        _override(
            app,
            bedrock=bedrock_schema_stub(
                schema_fields=[
                    {"name": "account_name", "type": "string", "faker": "company"},
                    {"name": "plan", "type": "string",
                     "enum": ["Starter", "Pro", "Enterprise"]},
                ]
            ),
        )
        r = client.post(
            "/preview",
            data={"seed_mode": "custom",
                  "prompt": "A B2B SaaS company's customer accounts with plan tier",
                  "rows": "10"},
        )
        assert r.status_code == 200
        # The prompt path renders a preview of the designed schema. The UI no
        # longer surfaces a dollar cost figure (cost language removed) — the
        # preview shows the schema's fields instead.
        assert "account_name" in r.text
        assert "fields" in r.text

    def test_prompt_path_requires_explicit_submit_not_on_load(self, client):
        """GET / must not trigger a paid call; only POST /preview does."""
        app = client.app
        bedrock = bedrock_schema_stub(schema_fields=[{"name": "x", "type": "string",
                                                      "faker": "word"}])
        _override(app, bedrock=bedrock)
        client.get("/")
        bedrock.converse.assert_not_called()
