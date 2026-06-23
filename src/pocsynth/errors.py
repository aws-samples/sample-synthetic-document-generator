# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Error hierarchy. Each subclass carries a stable `code`, its `exit_code`,
whether a blind retry may succeed, and an optional actionable `hint`.

Per-call detail lives in the instance `context` dict so agents can route
without the top-level codes exploding in number.
"""

from __future__ import annotations


class DocSynthError(Exception):
    code: str = "INTERNAL_ERROR"
    exit_code: int = 1
    retryable: bool = False
    hint: str | None = None

    def __init__(
        self,
        message: str,
        *,
        context: dict | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict = dict(context or {})
        if hint is not None:
            self.hint = hint


class UsageError(DocSynthError):
    code = "INVALID_ARGS"
    exit_code = 2


class InputError(DocSynthError):
    code = "INPUT_NOT_FOUND"
    exit_code = 3


class InputNotPdfError(DocSynthError):
    code = "INPUT_NOT_PDF"
    exit_code = 3


class UrlRejectedError(DocSynthError):
    code = "URL_REJECTED"
    exit_code = 3


class AuthError(DocSynthError):
    code = "AWS_AUTH_FAILED"
    exit_code = 4


class AuthExpiredError(DocSynthError):
    code = "AWS_AUTH_EXPIRED"
    exit_code = 4


class UpstreamError(DocSynthError):
    code = "BEDROCK_ERROR"
    exit_code = 5
    retryable = True


class ComprehendError(DocSynthError):
    code = "COMPREHEND_ERROR"
    exit_code = 5
    retryable = True


class HttpError(DocSynthError):
    code = "HTTP_ERROR"
    exit_code = 5
    retryable = True


class PartialError(DocSynthError):
    code = "PARTIAL_SUCCESS"
    exit_code = 6


class PricingDataError(DocSynthError):
    code = "PRICING_DATA_ERROR"
    exit_code = 2


class SchemaError(DocSynthError):
    """Invalid schema file, schema arguments, or unknown Faker provider."""

    code = "SCHEMA_INVALID"
    exit_code = 2


class ExtractionError(DocSynthError):
    """Structured extraction produced nothing usable."""

    code = "EXTRACTION_FAILED"
    exit_code = 5
    retryable = True


class DataInvalidError(DocSynthError):
    """`test` found rows that violate the schema. The validation report is still
    emitted in the envelope; this maps the failure to a distinct exit code so
    CI / agents can gate on it.
    """

    code = "DATA_INVALID"
    exit_code = 7


class LeakDetectedError(DocSynthError):
    """`verify` found a real source PII value leaked into the generated Rows or
    the Schema artifact (ADR-0010). Fail-closed: distinct exit code so CI /
    agents / the one-shot `run` gate can refuse to clear the output for sharing.
    The full Attestation rides in `context.attestation`.
    """

    code = "PII_LEAK_DETECTED"
    exit_code = 8


class CostGateError(DocSynthError):
    """The one-shot `run` verb's code-enforced cost gate (ADR-0011) blocked a
    paid run whose estimate exceeded the threshold without explicit confirmation.
    Usage-family exit code (2); agents route on `code` and the `estimate` /
    `threshold` in `context`, then re-run with `--yes` (or `--no-gate`).
    """

    code = "COST_GATE_BLOCKED"
    exit_code = 2
