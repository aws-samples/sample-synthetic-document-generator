# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the region-resolution chain (aws.resolve_region)."""

from __future__ import annotations

import pytest

from pocsynth.aws import DEFAULT_REGION, resolve_region
from pocsynth.errors import AuthError


class TestResolveRegionPrecedence:
    def test_cli_region_wins(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        region, source = resolve_region("us-west-2", profile=None)
        assert region == "us-west-2" and source == "cli"

    def test_env_region_used_when_no_cli(self, monkeypatch):
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
        region, source = resolve_region(None, profile=None)
        assert region == "ap-southeast-2" and source == "env"

    def test_default_region_fallback(self, monkeypatch):
        # No CLI, no env, and a session that resolves no region -> default.
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        class _NoRegionSession:
            region_name = None

        monkeypatch.setattr("pocsynth.aws.boto3.Session", lambda *a, **k: _NoRegionSession())
        region, source = resolve_region(None, profile=None)
        assert region == DEFAULT_REGION and source == "default"


class TestResolveRegionProfileNotFound:
    """Gap 3: a named profile that doesn't exist must surface as a clean
    AuthError (exit 4), not a raw botocore ProfileNotFound traceback."""

    def test_bad_profile_raises_auth_error(self, monkeypatch):
        from botocore.exceptions import ProfileNotFound

        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        def _raise(*_a, **_k):
            raise ProfileNotFound(profile="ghost")

        monkeypatch.setattr("pocsynth.aws.boto3.Session", _raise)
        with pytest.raises(AuthError) as ei:
            resolve_region(None, profile="ghost")
        assert ei.value.exit_code == 4
        assert ei.value.context.get("profile") == "ghost"

    def test_cli_region_short_circuits_before_profile_lookup(self, monkeypatch):
        # With an explicit region, a bad profile is never consulted (no raise).
        def _raise(*_a, **_k):
            raise AssertionError("Session should not be constructed when region is given")

        monkeypatch.setattr("pocsynth.aws.boto3.Session", _raise)
        region, source = resolve_region("us-east-1", profile="ghost")
        assert region == "us-east-1" and source == "cli"
