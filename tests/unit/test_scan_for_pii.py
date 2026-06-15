# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import csv
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from pocsynth.comprehend import scan_for_pii


def _make_stubbed_comprehend(entities):
    client = boto3.client("comprehend", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "detect_pii_entities",
        {"Entities": entities},
        expected_params={"Text": None, "LanguageCode": "en"},
    )
    # Stubber defaults to strict params; relax by replacing expected_params with ANY
    stubber = Stubber(client)
    stubber.add_response("detect_pii_entities", {"Entities": entities})
    stubber.activate()
    return client, stubber


class TestScanForPii:
    def test_csv_escapes_values_with_commas_and_quotes(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        #             0         1         2
        #             0123456789012345678901
        text = 'Name: John, Smith is here'
        name_start = text.index("John")
        name_end = name_start + len("John, Smith")
        entities = [
            {
                "Type": "NAME",
                "Score": 0.99,
                "BeginOffset": name_start,
                "EndOffset": name_end,
            },
        ]
        client, stubber = _make_stubbed_comprehend(entities)

        try:
            detected = scan_for_pii(text, folder_name="pii-audit",
                                    filename="sample", comprehend=client)
        finally:
            stubber.deactivate()

        assert len(detected) == 1
        audit = Path("pii-audit/sample_pii_scan_audit.csv")
        assert audit.exists()

        with open(audit, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        assert rows[0] == ["FileName", "PageNumber", "Type", "Score",
                           "BeginOffset", "EndOffset", "Value"]
        # Exactly one data row — the comma inside the value didn't split it
        assert len(rows) == 2
        assert rows[1][-1] == "John, Smith"

    def test_no_pii_writes_header_only_csv(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        client, stubber = _make_stubbed_comprehend([])
        try:
            detected = scan_for_pii("no sensitive data here",
                                    folder_name="pii-audit",
                                    filename="clean",
                                    comprehend=client)
        finally:
            stubber.deactivate()
        assert detected == []
        # Artifact is still created with the header row so consumers can
        # `cat` or `pandas.read_csv` it without branching on existence.
        audit = Path("pii-audit/clean_pii_scan_audit.csv")
        assert audit.exists()
        with open(audit, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows == [
            ["FileName", "PageNumber", "Type", "Score",
             "BeginOffset", "EndOffset", "Value"]
        ]

    def test_redact_values_replaces_raw_pii(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        text = "Call Alice at 555-0123 today"
        entities = [
            {"Type": "NAME", "Score": 0.95,
             "BeginOffset": text.index("Alice"),
             "EndOffset": text.index("Alice") + len("Alice")},
            {"Type": "PHONE", "Score": 0.92,
             "BeginOffset": text.index("555-0123"),
             "EndOffset": text.index("555-0123") + len("555-0123")},
        ]
        client = boto3.client("comprehend", region_name="us-east-1")
        stubber = Stubber(client)
        stubber.add_response("detect_pii_entities", {"Entities": entities})
        stubber.activate()
        try:
            scan_for_pii(text, folder_name="pii-audit", filename="redacted",
                         comprehend=client, redact_values=True)
        finally:
            stubber.deactivate()

        audit = Path("pii-audit/redacted_pii_scan_audit.csv")
        with open(audit, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[0][-1] == "Value"
        values = [r[-1] for r in rows[1:]]
        assert values == ["[REDACTED]", "[REDACTED]"]
        # The raw PII must not appear anywhere in the audit file
        contents = audit.read_text(encoding="utf-8")
        assert "Alice" not in contents
        assert "555-0123" not in contents
        # Offsets and types are still recorded so findings can be located
        assert any(r[2] == "NAME" for r in rows[1:])
        assert any(r[2] == "PHONE" for r in rows[1:])

    def test_header_written_only_once_on_rerun(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        entities = [{"Type": "NAME", "Score": 0.9, "BeginOffset": 0, "EndOffset": 4}]

        for _ in range(2):
            client = boto3.client("comprehend", region_name="us-east-1")
            stubber = Stubber(client)
            stubber.add_response("detect_pii_entities", {"Entities": entities})
            stubber.activate()
            try:
                scan_for_pii("Alan likes pie", folder_name="pii-audit",
                             filename="rerun", comprehend=client)
            finally:
                stubber.deactivate()

        audit = Path("pii-audit/rerun_pii_scan_audit.csv")
        with open(audit, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        header_count = sum(1 for r in rows if r and r[0] == "FileName")
        assert header_count == 1


class TestScanForPiiChunking:
    """Large text is scanned in overlapping chunks; entities re-seen in the
    overlap window must be de-duplicated, and Comprehend failures must surface
    as a typed ComprehendError (not a silent gap in the audit)."""

    def test_boundary_entity_deduplicated(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        from pocsynth.comprehend import _CHUNK_OVERLAP, COMPREHEND_MAX_TEXT_LENGTH
        monkeypatch.chdir(tmp_path)

        # An entity that lands inside the overlap window appears in BOTH chunks
        # (with chunk-local offsets). scan_for_pii dedups by absolute span+type.
        step = COMPREHEND_MAX_TEXT_LENGTH - _CHUNK_OVERLAP
        # Chunk starts are range(0, len, step). To force EXACTLY 2 chunks the
        # length must be in (step, 2*step]; step+100 sits just past chunk 1.
        text = "x" * (step + 100)
        # Absolute span sits 50 chars into the overlap region of chunk 2.
        abs_begin = step + 50
        abs_end = abs_begin + 8

        def _detect(Text="", **_kw):
            # Determine which chunk this is by length / content offset is hard;
            # instead return the entity whenever the chunk covers abs_begin.
            # Chunk 1 starts at 0; chunk 2 starts at `step`.
            # We can't see the start here, so return for both calls with the
            # offset translated to chunk-local — scan_for_pii adds chunk_start.
            # Emit chunk-1-local for the first call, chunk-2-local for the second.
            calls.append(len(Text))
            if len(calls) == 1:  # chunk 1 (starts at 0)
                return {"Entities": [{"Type": "SSN", "Score": 0.9,
                                      "BeginOffset": abs_begin, "EndOffset": abs_end}]}
            return {"Entities": [{"Type": "SSN", "Score": 0.9,
                                  "BeginOffset": abs_begin - step, "EndOffset": abs_end - step}]}

        calls: list[int] = []
        client = MagicMock()
        client.detect_pii_entities.side_effect = _detect

        detected = scan_for_pii(text, folder_name="pii-audit",
                                filename="boundary", comprehend=client)
        # Two chunks scanned, but the same absolute (begin,end,type) appears once.
        assert client.detect_pii_entities.call_count == 2
        assert len(detected) == 1
        assert detected[0]["BeginOffset"] == abs_begin

    def test_many_chunks_all_scanned(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        from pocsynth.comprehend import _CHUNK_OVERLAP, COMPREHEND_MAX_TEXT_LENGTH
        monkeypatch.chdir(tmp_path)
        step = COMPREHEND_MAX_TEXT_LENGTH - _CHUNK_OVERLAP
        text = "a" * (step * 2 + 10)  # 3 chunks
        client = MagicMock()
        client.detect_pii_entities.return_value = {"Entities": []}
        scan_for_pii(text, folder_name="pii-audit", filename="many", comprehend=client)
        assert client.detect_pii_entities.call_count == 3

    def test_comprehend_clienterror_raises_comprehend_error(self, tmp_path, monkeypatch):

        from pocsynth.errors import ComprehendError
        monkeypatch.chdir(tmp_path)
        client = boto3.client("comprehend", region_name="us-east-1")
        stubber = Stubber(client)
        stubber.add_client_error(
            "detect_pii_entities", service_error_code="ThrottlingException",
            service_message="Rate exceeded", http_status_code=429)
        stubber.activate()
        try:
            with pytest.raises(ComprehendError) as ei:
                scan_for_pii("some text", folder_name="pii-audit",
                             filename="throttle", comprehend=client)
        finally:
            stubber.deactivate()
        # Throttling is retryable so callers can back off.
        assert ei.value.retryable is True

    def test_empty_text_is_scanned_once_clean(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        monkeypatch.chdir(tmp_path)
        client = MagicMock()
        client.detect_pii_entities.return_value = {"Entities": []}
        detected = scan_for_pii("", folder_name="pii-audit",
                                filename="empty", comprehend=client)
        assert detected == []
        assert client.detect_pii_entities.call_count == 1
        assert Path("pii-audit/empty_pii_scan_audit.csv").exists()
