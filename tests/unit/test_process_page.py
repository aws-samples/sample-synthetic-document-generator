# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.stub import Stubber

from pocsynth.bedrock import process_page
from pocsynth.errors import UpstreamError


def _fake_page(text="hello from page"):
    page = MagicMock()
    page.get_text = MagicMock(return_value=text)
    return page


def _converse_client(response: dict) -> MagicMock:
    client = MagicMock()
    client.converse.return_value = response
    return client


class TestProcessPage:
    def test_calls_converse_and_returns_content(self):
        client = boto3.client("bedrock-runtime", region_name="us-east-1")
        stubber = Stubber(client)
        stubber.add_response(
            "converse",
            {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"text": "<p>converted</p>"}],
                    }
                },
                "stopReason": "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                "metrics": {"latencyMs": 100},
            },
        )
        stubber.activate()

        try:
            result = process_page(
                client,
                "global.anthropic.claude-sonnet-4-6",
                [{"text": "system"}],
                "Prompt: {page_text}",
                _fake_page("raw page"),
                0,
                b"\x89PNG fake",
                max_tokens=1000,
            )
        finally:
            stubber.deactivate()

        assert result["text"] == "<p>converted</p>"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5


class TestProcessPageGuardrails:
    """Bedrock can return a guardrail intervention / empty content with no text
    block. process_page must surface a structured UpstreamError carrying the
    stopReason, not a KeyError/IndexError."""

    def test_empty_content_raises_upstream_with_stop_reason(self):
        client = _converse_client({
            "output": {"message": {"role": "assistant", "content": []}},
            "stopReason": "guardrail_intervened",
            "usage": {"inputTokens": 7, "outputTokens": 0},
        })
        with pytest.raises(UpstreamError) as ei:
            process_page(client, "model", [{"text": "sys"}], "P: {page_text}",
                         _fake_page("x"), 0, b"png", max_tokens=100)
        assert ei.value.context.get("stop_reason") == "guardrail_intervened"
        assert ei.value.context.get("page") == 1

    def test_tooluse_only_no_text_raises_upstream(self):
        # A response that is all toolUse blocks (no text) for the convert path.
        client = _converse_client({
            "output": {"message": {"role": "assistant",
                                   "content": [{"toolUse": {"name": "x", "input": {}}}]}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": 5, "outputTokens": 2},
        })
        with pytest.raises(UpstreamError):
            process_page(client, "model", [{"text": "sys"}], "P: {page_text}",
                         _fake_page("x"), 1, b"png", max_tokens=100)

    def test_braced_page_text_does_not_break_substitution(self):
        # page text with literal {curly} braces (code/JSON/merge-fields) must be
        # injected verbatim via str.replace, never .format (which would KeyError).
        client = _converse_client({
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 3, "outputTokens": 1},
        })
        braced = 'config = {"name": "{value}", "id": {0}}'
        process_page(client, "model", [{"text": "sys"}], "Convert: {page_text}",
                     _fake_page(braced), 0, b"png", max_tokens=100)
        sent = client.converse.call_args.kwargs["messages"][0]["content"][-1]["text"]
        assert braced in sent  # injected literally, no KeyError, no mangling

    def test_missing_usage_tokens_default_to_zero(self):
        client = _converse_client({
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {},  # no token fields at all
        })
        res = process_page(client, "model", [{"text": "sys"}], "P: {page_text}",
                           _fake_page("x"), 0, b"png", max_tokens=100)
        assert res["usage"] == {"input_tokens": 0, "output_tokens": 0}
