# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Amazon Comprehend PII audit: detect PII entities and write a CSV audit file.

Process-wide behaviour is unchanged from the legacy module. The optional
`comprehend` parameter is used by tests to inject a botocore.stub.Stubber-
backed client.
"""

from __future__ import annotations

import csv
import logging
import os
import re
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from pocsynth.bedrock import translate_aws_error

logger = logging.getLogger(__name__)

COMPREHEND_MAX_TEXT_LENGTH = 100_000
# Overlap between consecutive chunks so PII spanning a chunk boundary is
# detected. 200 chars comfortably covers any single Comprehend entity
# (longest PII type — addresses — rarely exceeds ~120 chars).
_CHUNK_OVERLAP = 200


def scan_for_pii(
    text: str,
    folder_name: str = "pii-audit",
    filename: str = "all",
    page_num: int = 0,
    comprehend=None,
    redact_values: bool = False,
):
    """Detect PII with Comprehend and write a CSV audit file.

    The ``comprehend`` client is required — callers must pass one built
    from the resolved profile/region (see ``aws.make_session``) so that
    Comprehend binds to the same region as Bedrock. A bare
    ``boto3.client("comprehend")`` would silently ignore CLI flags.

    By default, the audit file contains the raw matched PII values. Pass
    ``redact_values=True`` to store a fixed ``[REDACTED]`` marker; offsets,
    type, and confidence score are still recorded.
    """
    if comprehend is None:
        raise ValueError(
            "scan_for_pii requires a pre-built comprehend client. "
            "Build one via pocsynth.aws.make_session().client('comprehend') "
            "so profile/region routing matches Bedrock."
        )
    os.makedirs(folder_name, exist_ok=True)
    detected_pii: list[dict] = []
    # Dedup entities re-detected in the overlap window between adjacent chunks.
    seen_offsets: set[tuple[int, int, str]] = set()

    # Step the window by (max_length - overlap) so a PII token spanning the
    # previous chunk's tail also appears whole in the next chunk's head.
    step = max(1, COMPREHEND_MAX_TEXT_LENGTH - _CHUNK_OVERLAP)
    chunk_starts = list(range(0, max(len(text), 1), step))
    chunks = [(start, text[start : start + COMPREHEND_MAX_TEXT_LENGTH])
              for start in chunk_starts]

    for chunk_index, (chunk_start, chunk) in enumerate(chunks):
        try:
            response = comprehend.detect_pii_entities(Text=chunk, LanguageCode="en")
        except (ClientError, BotoCoreError) as exc:
            # Translate to a typed ComprehendError so callers see structured
            # error info (auth, throttle, validation) instead of a silent
            # gap in the audit. Loss of even one chunk means we can't claim
            # 'no PII detected' for the document; raise.
            raise translate_aws_error(exc, service="comprehend") from exc

        for entity in response.get("Entities", []):
            begin_offset = entity["BeginOffset"] + chunk_start
            end_offset = entity["EndOffset"] + chunk_start
            key = (begin_offset, end_offset, entity["Type"])
            if key in seen_offsets:
                continue
            seen_offsets.add(key)
            detected_pii.append(
                {
                    "FileName": filename,
                    "PageNumber": page_num,
                    "Type": entity["Type"],
                    "Score": entity["Score"],
                    "BeginOffset": begin_offset,
                    "EndOffset": end_offset,
                    "Value": text[begin_offset:end_offset],
                }
            )

    audit_file_path = Path(folder_name) / f"{filename}_pii_scan_audit.csv"
    write_header = not audit_file_path.exists() or audit_file_path.stat().st_size == 0

    with open(audit_file_path, "a", encoding="utf-8", newline="") as audit_file:
        writer = csv.writer(audit_file, quoting=csv.QUOTE_MINIMAL)
        # Always write the header on first touch so the artifact is present and
        # parseable even when no PII was detected. Consumers and demos can
        # `cat` or `pandas.read_csv` the file without a separate "file exists?"
        # branch.
        if write_header:
            writer.writerow(
                ["FileName", "PageNumber", "Type", "Score",
                 "BeginOffset", "EndOffset", "Value"]
            )
        for pii in detected_pii:
            if redact_values:
                value = "[REDACTED]"
            else:
                value = re.sub(r"<br\s*/?>", "\n", pii["Value"])
                value = re.sub(r"</p>\s*<p>", "\n", value)
                value = value.replace("\n", " ").replace("\r", "")
            writer.writerow(
                [pii["FileName"], pii["PageNumber"], pii["Type"], pii["Score"],
                 pii["BeginOffset"], pii["EndOffset"], value]
            )
        if not detected_pii:
            logger.info("No PII detected in %s", filename)

    return detected_pii
