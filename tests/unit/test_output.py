# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the stable JSON envelope, error envelope, and NDJSON events."""

import json

from pocsynth import __version__
from pocsynth.errors import DocSynthError, UpstreamError, UrlRejectedError
from pocsynth.output import (
    SCHEMA_VERSION,
    emit_ndjson,
    envelope,
    error_envelope,
    ndjson_event,
)


class TestEnvelope:
    def test_success_envelope_required_keys(self):
        e = envelope("convert", {"foo": 1})
        assert e["ok"] is True
        assert e["schema"] == SCHEMA_VERSION == 1
        assert e["tool_version"] == __version__
        assert e["command"] == "convert"
        assert e["event"] == "complete"
        assert e["result"] == {"foo": 1}

    def test_success_event_always_present(self):
        # Non-stream envelopes must still carry event="complete".
        e = envelope("models", {"models": []})
        assert "event" in e and e["event"] == "complete"

    def test_error_envelope_shape(self):
        exc = UrlRejectedError(
            "bad url",
            context={"url": "http://x", "reason": "non_https_scheme"},
            hint="Use https",
        )
        e = error_envelope("convert", exc)
        assert e["ok"] is False
        assert e["schema"] == 1
        assert e["command"] == "convert"
        assert e["event"] == "complete"
        assert e["error"]["code"] == "URL_REJECTED"
        assert e["error"]["message"] == "bad url"
        assert e["error"]["retryable"] is False
        assert e["error"]["hint"] == "Use https"
        assert e["error"]["context"] == {"url": "http://x", "reason": "non_https_scheme"}

    def test_error_envelope_retryable_true_for_upstream(self):
        e = error_envelope("convert", UpstreamError("throttled"))
        assert e["error"]["retryable"] is True

    def test_error_envelope_unknown_error_default_code(self):
        e = error_envelope("convert", DocSynthError("oops"))
        assert e["error"]["code"] == "INTERNAL_ERROR"


class TestNdjsonEvent:
    def test_event_envelope_keys(self):
        ev = ndjson_event("page_processed", "convert", page=3, of=5, page_path="/tmp/x.html")
        assert ev["schema"] == 1
        assert ev["tool_version"] == __version__
        assert ev["command"] == "convert"
        assert ev["event"] == "page_processed"
        assert ev["page"] == 3 and ev["of"] == 5
        assert ev["page_path"] == "/tmp/x.html"

    def test_emit_ndjson_one_line(self, capsys):
        emit_ndjson(ndjson_event("page_started", "convert", page=1, of=2))
        captured = capsys.readouterr()
        assert captured.err == ""
        # Exactly one line ending in \n, and it must be valid JSON
        assert captured.out.endswith("\n")
        assert captured.out.count("\n") == 1
        parsed = json.loads(captured.out)
        assert parsed["event"] == "page_started"


class TestStreamInvariant:
    """The `complete` NDJSON line's envelope must match a non-stream --json envelope."""

    def test_complete_event_matches_non_stream_envelope(self):
        result = {"foo": "bar", "n": 5}
        non_stream = envelope("convert", result)
        stream_complete = ndjson_event("complete", "convert", ok=True, result=result)

        # The stream "complete" line should carry the same command and result; the
        # `event` key is always "complete" in both shapes (per the invariant).
        assert stream_complete["event"] == non_stream["event"] == "complete"
        assert stream_complete["command"] == non_stream["command"] == "convert"
        assert stream_complete["result"] == non_stream["result"]
