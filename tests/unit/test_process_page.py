# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from unittest.mock import MagicMock

import boto3
from botocore.stub import Stubber

from pocsynth.bedrock import process_page


def _fake_page(text="hello from page"):
    page = MagicMock()
    page.get_text = MagicMock(return_value=text)
    return page


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
