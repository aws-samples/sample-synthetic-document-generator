# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AWS session/region helpers. Single source of truth for region resolution."""

from __future__ import annotations

import os

import boto3
from botocore.exceptions import ProfileNotFound

from pocsynth.errors import AuthError

DEFAULT_REGION = "us-east-1"


def resolve_region(cli_region: str | None, profile: str | None = None) -> tuple[str, str]:
    """Return (region, source) using the documented resolution chain.

    `source` is one of: "cli", "env", "profile", "imds", "default".
    """
    if cli_region:
        return cli_region, "cli"

    if env := os.environ.get("AWS_REGION"):
        return env, "env"
    if env := os.environ.get("AWS_DEFAULT_REGION"):
        return env, "env"

    # boto3 Session.region_name walks ~/.aws/config (via profile or default) and
    # IMDS. We don't have a clean way to distinguish the two, so we report
    # "profile" when a profile is named and "imds" otherwise. The distinction
    # is only for debug / doctor output; neither path changes the value.
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    except ProfileNotFound as exc:
        # A named --profile that isn't in ~/.aws/config would otherwise surface
        # as a raw botocore traceback; route it as a clean auth error (exit 4).
        raise AuthError(
            f"AWS profile not found: {profile!r}",
            context={"profile": profile},
            hint="Check the profile name, or run `aws configure --profile <name>`",
        ) from exc
    if region := session.region_name:
        return region, "profile" if profile else "imds"

    return DEFAULT_REGION, "default"
