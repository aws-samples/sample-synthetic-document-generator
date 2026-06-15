# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Output envelope + single stdout writer.

`emit()` is the ONLY function in the package allowed to write to stdout.
Ruff rule T201 enforces no bare `print()` elsewhere in src/pocsynth/.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from pocsynth import __version__
from pocsynth.errors import DocSynthError

SCHEMA_VERSION = 1


def envelope(
    command: str,
    result: dict[str, Any],
    *,
    event: str = "complete",
) -> dict[str, Any]:
    """Build a success envelope. `event` is always present so agents can use
    one parser path for both single and streamed outputs.
    """
    return {
        "ok": True,
        "schema": SCHEMA_VERSION,
        "tool_version": __version__,
        "command": command,
        "event": event,
        "result": result,
    }


def error_envelope(command: str, exc: DocSynthError) -> dict[str, Any]:
    return {
        "ok": False,
        "schema": SCHEMA_VERSION,
        "tool_version": __version__,
        "command": command,
        "event": "complete",
        "error": {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "hint": exc.hint,
            "context": dict(exc.context),
        },
    }


def ndjson_event(event_name: str, command: str, /, **payload: Any) -> dict[str, Any]:
    """Build an enveloped NDJSON event for streaming."""
    return {
        "schema": SCHEMA_VERSION,
        "tool_version": __version__,
        "command": command,
        "event": event_name,
        **payload,
    }


def emit(obj: dict[str, Any], *, json_mode: bool, dest: str = "stdout") -> None:
    """Write `obj` in the appropriate format.

    - json_mode=True: write a single compact JSON line to the chosen stream.
    - json_mode=False: caller is responsible for human formatting; this
      function falls back to JSON indent=2 for diagnostic use.
    """
    stream = sys.stdout if dest == "stdout" else sys.stderr
    if json_mode:
        stream.write(json.dumps(obj, separators=(",", ":")))
        stream.write("\n")
    else:
        stream.write(json.dumps(obj, indent=2))
        stream.write("\n")
    stream.flush()


def emit_ndjson(obj: dict[str, Any]) -> None:
    """Write one NDJSON event to stdout. Used only by --stream."""
    sys.stdout.write(json.dumps(obj, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()
