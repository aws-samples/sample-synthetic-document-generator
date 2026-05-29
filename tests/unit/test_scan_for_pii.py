# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import csv
from pathlib import Path

import boto3
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
        assert len(rows) == 3  # 1 header + 2 data rows
