# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bedrock model catalog, client creation, per-page Converse call, and
boto3-exception translation into DocSynthError subclasses."""

from __future__ import annotations

from typing import Any

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    PartialCredentialsError,
)

from pocsynth.errors import (
    AuthError,
    AuthExpiredError,
    ComprehendError,
    DocSynthError,
    UpstreamError,
)

MODELS: dict[str, dict[str, Any]] = {
    "sonnet": {
        "id": "global.anthropic.claude-sonnet-4-6",
        "context_window": 1_000_000,
        "description": "Claude Sonnet 4.6 (1M context on Bedrock)",
    },
    "opus": {
        "id": "global.anthropic.claude-opus-4-6-v1",
        "context_window": 1_000_000,
        "description": "Claude Opus 4.6 (1M context on Bedrock)",
    },
    "haiku": {
        "id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "context_window": 200_000,
        "description": "Claude Haiku 4.5 (200k context)",
    },
}

DEFAULT_MODEL = "sonnet"
DEFAULT_MAX_TOKENS = 8000


def make_session(profile: str | None = None, region: str | None = None) -> boto3.Session:
    kwargs: dict[str, Any] = {}
    if profile:
        kwargs["profile_name"] = profile
    if region:
        kwargs["region_name"] = region
    return boto3.Session(**kwargs)


def translate_aws_error(exc: Exception, *, service: str) -> DocSynthError:
    """Map a boto3 / botocore exception to a DocSynthError subclass.

    `service` is "bedrock" or "comprehend" and controls the upstream class
    used for non-auth errors.
    """
    if isinstance(exc, NoCredentialsError | PartialCredentialsError):
        return AuthError(
            "No AWS credentials found",
            context={"boto3_exception": type(exc).__name__},
            hint="Set AWS_PROFILE, or configure credentials via aws configure / IAM role",
        )

    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        ctx = {"boto3_code": code, "service": service, "message": message}

        if code in {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId"}:
            return AuthExpiredError(
                f"AWS credentials expired or invalid ({code})",
                context=ctx,
                hint="Refresh credentials (e.g. `aws sso login`)",
            )
        if code in {"AccessDeniedException", "UnauthorizedOperation", "AccessDenied"}:
            return AuthError(
                f"AWS access denied ({code}): {message}",
                context=ctx,
                hint="Check the IAM policy; see README for the minimum required actions",
            )

        upstream_cls = ComprehendError if service == "comprehend" else UpstreamError
        err = upstream_cls(f"{service}: {code}: {message}", context=ctx)
        transient = {
            "ThrottlingException",
            "TooManyRequestsException",
            "ServiceUnavailableException",
            "InternalServerException",
        }
        if code in transient:
            err.retryable = True
            err.hint = "Retry with exponential backoff"
        else:
            # Non-transient ClientError (ValidationException, etc.) is not
            # worth a blind retry; the call needs to change, not repeat.
            err.retryable = False
        return err

    if isinstance(exc, BotoCoreError):
        cls = ComprehendError if service == "comprehend" else UpstreamError
        return cls(f"{service}: {exc}", context={"boto3_exception": type(exc).__name__})

    # Unknown — re-wrap as generic upstream
    wrapped_cls = ComprehendError if service == "comprehend" else UpstreamError
    return wrapped_cls(str(exc), context={"exception": type(exc).__name__})


def read_tool_use(response: dict, tool_name: str) -> dict | None:
    """Return the `input` of the first `toolUse` block matching `tool_name`.

    Shared by the forced-toolConfig paid stages (extract, schema). Returns None
    when no matching toolUse block is present (e.g. a guardrail intervention or
    an empty page) so callers can route that as a per-page failure.
    """
    content = response.get("output", {}).get("message", {}).get("content", []) or []
    for block in content:
        if isinstance(block, dict) and "toolUse" in block:
            tu = block["toolUse"]
            if tu.get("name") == tool_name:
                return tu.get("input", {}) or {}
    return None


def process_page(
    bedrock_client,
    model_id: str,
    system_prompts: list[dict],
    prompt_template: str,
    page,
    page_num: int,
    img_bytes: bytes,
    max_tokens: int,
) -> dict[str, Any]:
    """Send a single page (text + PNG image) to Bedrock Converse.

    Returns a dict: {"text": str, "usage": {"input_tokens": int, "output_tokens": int}}.
    Raises a DocSynthError subclass on failure via translate_aws_error.
    """
    page_text = page.get_text("text")
    # str.replace (not .format) — page text routinely contains literal
    # `{name}`-style braces (code samples, JSON, Word merge fields) that
    # would otherwise raise KeyError before reaching Bedrock.
    prompt_with_text = prompt_template.replace("{page_text}", page_text)

    image_message = {
        "role": "user",
        "content": [
            {"text": f"Page {page_num + 1}:"},
            {"image": {"format": "png", "source": {"bytes": img_bytes}}},
            {"text": prompt_with_text},
        ],
    }

    try:
        response = bedrock_client.converse(
            modelId=model_id,
            messages=[image_message],
            system=system_prompts,
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
        )
    except (ClientError, BotoCoreError, NoCredentialsError, PartialCredentialsError) as exc:
        raise translate_aws_error(exc, service="bedrock") from exc

    # Defensive extraction: Bedrock can return guardrail interventions, tool
    # use blocks, or empty content arrays. Pull the first text block and
    # surface a structured error if no usable text exists.
    content_blocks = response.get("output", {}).get("message", {}).get("content", []) or []
    text = next(
        (block["text"] for block in content_blocks if isinstance(block, dict) and "text" in block),
        None,
    )
    if text is None:
        stop_reason = response.get("stopReason", "unknown")
        raise UpstreamError(
            f"Bedrock returned no text content (stopReason={stop_reason})",
            context={
                "stop_reason": stop_reason,
                "page": page_num + 1,
                "content_block_types": [
                    next(iter(b.keys()), "?") for b in content_blocks if isinstance(b, dict)
                ],
            },
        )
    return {
        "text": text,
        "usage": {
            "input_tokens": int(response.get("usage", {}).get("inputTokens", 0) or 0),
            "output_tokens": int(response.get("usage", {}).get("outputTokens", 0) or 0),
        },
    }
