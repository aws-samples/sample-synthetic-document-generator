# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for pocsynth.bedrock.translate_aws_error.

Six-plus branches that map boto3/botocore exceptions to DocSynthError
subclasses. Each branch gets its own test so we know which one broke.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    PartialCredentialsError,
)

from pocsynth.bedrock import translate_aws_error
from pocsynth.errors import AuthError, AuthExpiredError, UpstreamError


def _client_error(code: str, message: str = "boom") -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": message}},
        operation_name="Converse",
    )


class TestNoCredentials:
    def test_no_credentials_becomes_auth_error(self):
        err = translate_aws_error(NoCredentialsError(), service="bedrock")
        assert isinstance(err, AuthError)
        assert err.code == "AWS_AUTH_FAILED"
        assert err.retryable is False
        assert err.hint is not None and "AWS_PROFILE" in err.hint

    def test_partial_credentials_becomes_auth_error(self):
        err = translate_aws_error(
            PartialCredentialsError(provider="env", cred_var="AWS_ACCESS_KEY_ID"),
            service="bedrock",
        )
        assert isinstance(err, AuthError)
        assert err.code == "AWS_AUTH_FAILED"


class TestExpiredToken:
    @pytest.mark.parametrize("code", [
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidClientTokenId",
    ])
    def test_expired_codes_become_auth_expired(self, code):
        err = translate_aws_error(_client_error(code), service="bedrock")
        assert isinstance(err, AuthExpiredError)
        assert err.code == "AWS_AUTH_EXPIRED"
        assert err.retryable is False
        assert err.hint is not None and "refresh" in err.hint.lower()
        assert err.context["boto3_code"] == code


class TestAccessDenied:
    @pytest.mark.parametrize("code", [
        "AccessDeniedException",
        "UnauthorizedOperation",
        "AccessDenied",
    ])
    def test_denied_codes_become_auth_error(self, code):
        err = translate_aws_error(_client_error(code), service="bedrock")
        assert isinstance(err, AuthError)
        assert err.code == "AWS_AUTH_FAILED"
        assert err.hint is not None and "IAM" in err.hint


class TestRetryableUpstream:
    @pytest.mark.parametrize("code", [
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "InternalServerException",
    ])
    def test_transient_errors_are_retryable(self, code):
        err = translate_aws_error(_client_error(code), service="bedrock")
        assert isinstance(err, UpstreamError)
        assert err.retryable is True
        assert err.hint is not None
        assert "backoff" in err.hint.lower()


class TestGenericClientError:
    def test_unknown_client_error_becomes_upstream(self):
        err = translate_aws_error(_client_error("ValidationException"), service="bedrock")
        assert isinstance(err, UpstreamError)
        assert err.code == "BEDROCK_ERROR"
        # Non-throttling ValidationException is NOT retryable
        assert err.retryable is False
        assert err.context["boto3_code"] == "ValidationException"


class TestBotoCoreError:
    def test_botocore_error_becomes_upstream(self):
        err = translate_aws_error(BotoCoreError(), service="bedrock")
        assert isinstance(err, UpstreamError)
        assert err.code == "BEDROCK_ERROR"
        assert "boto3_exception" in err.context


class TestServiceLabel:
    def test_comprehend_service_sets_comprehend_error_code(self):
        err = translate_aws_error(_client_error("ThrottlingException"), service="comprehend")
        assert err.code == "COMPREHEND_ERROR"
        assert err.retryable is True

    def test_unknown_exception_for_comprehend_also_gets_comprehend_code(self):
        err = translate_aws_error(RuntimeError("what"), service="comprehend")
        assert err.code == "COMPREHEND_ERROR"


class TestUnknownException:
    def test_unknown_exception_becomes_generic_upstream(self):
        err = translate_aws_error(ValueError("unexpected"), service="bedrock")
        assert isinstance(err, UpstreamError)
        assert err.context.get("exception") == "ValueError"
