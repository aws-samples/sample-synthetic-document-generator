#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run the deterministic half of the pocsynth skill evaluations.

The skill-creator convention has two grading layers:

  1. Deterministic assertions (exit code, JSON shape, invocation argv,
     file-on-disk). This script runs them and writes
     `deterministic_grading.json` per case.
  2. Behavioral assertions (did Claude call AskUserQuestion, did fast-mode
     skip confirmation, etc.). Those require a real Claude Code run with
     the skill installed; they are graded by a subagent reading
     `skills/pocsynth/agents/grader.md`. This script does not cover them.

Usage:
    uv run python scripts/run-skill-evals.py \\
        --workspace skills/pocsynth-workspace/iteration-1 \\
        [--case-id <id>]

Expects a workspace laid out per the skill-creator convention:

    <workspace>/
        case-<id>/
            with_skill/outputs/
                invocation.json       # {"argv": [...], "exit_code": N, "stdout": "..."}
                transcript.md         # (graded by the LLM grader, not here)
            without_skill/outputs/...
            case_metadata.json        # copy of the case from evals.json

If the workspace is empty, the script reports which cases it would grade
but does not invent data - you are expected to populate `with_skill/`
outputs by running each prompt through Claude Code with the skill
installed and saving the resulting transcripts + invocation details.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = REPO_ROOT / "skills" / "pocsynth" / "evals" / "evals.json"


def _load_invocation(outputs_dir: Path) -> dict[str, Any] | None:
    path = outputs_dir / "invocation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _grade_deterministic(
    assertion: dict[str, Any], invocation: dict[str, Any]
) -> tuple[bool, str]:
    """Return (passed, evidence) for a single deterministic assertion."""
    kind = assertion.get("kind")
    argv: list[str] = invocation.get("argv", [])
    exit_code: int = invocation.get("exit_code", -1)
    stdout: str = invocation.get("stdout", "")

    if kind == "exit_code":
        ok = exit_code == assertion["expected"]
        return ok, f"observed exit_code={exit_code}, expected={assertion['expected']}"

    if kind == "json_ok_true":
        try:
            env = json.loads(stdout)
            ok = env.get("ok") is True
            return ok, f"stdout ok={env.get('ok')!r}"
        except json.JSONDecodeError as exc:
            return False, f"stdout is not valid JSON: {exc}"

    if kind == "file_exists":
        path_from = assertion["path_from"]  # e.g. $.result.output.combined_path
        try:
            env = json.loads(stdout)
            cursor: Any = env
            for part in path_from.removeprefix("$.").split("."):
                cursor = cursor[part]
            on_disk = Path(cursor).exists()
            return on_disk, f"{path_from} = {cursor!r}; on_disk={on_disk}"
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return False, f"could not resolve {path_from}: {exc}"

    if kind == "invocation_contains":
        arg = assertion["arg"]
        value = assertion["value"]
        if arg == "positional":
            ok = value in argv
            return ok, f"argv={argv}; positional {value!r} present={ok}"
        # Flag/value pair: look for `--foo value` or `--foo=value`
        for i, token in enumerate(argv):
            if token == arg and i + 1 < len(argv) and argv[i + 1] == value:
                return True, f"found {arg} {value} at argv[{i}]"
            if token == f"{arg}={value}":
                return True, f"found {arg}={value} at argv[{i}]"
        return False, f"argv={argv}; {arg} {value!r} not found"

    if kind == "error_code_in":
        try:
            env = json.loads(stdout)
            code = env.get("error", {}).get("code")
            ok = code in assertion["values"]
            return ok, f"error.code={code!r}, allowed={assertion['values']}"
        except json.JSONDecodeError as exc:
            return False, f"stdout is not valid JSON: {exc}"

    if kind is None:
        # Non-kind deterministic assertion (e.g., disjunctive) - cannot be
        # graded automatically; defer to the LLM grader.
        return False, "INCONCLUSIVE: no `kind` field; assertion requires LLM grading"

    return False, f"unknown assertion kind: {kind}"


def _grade_case(case_def: dict[str, Any], case_dir: Path) -> dict[str, Any]:
    outputs = case_dir / "with_skill" / "outputs"
    invocation = _load_invocation(outputs)
    expectations: list[dict[str, Any]] = []

    for assertion in case_def.get("assertions", {}).get("deterministic", []):
        if invocation is None:
            expectations.append(
                {
                    "text": assertion["text"],
                    "passed": False,
                    "evidence": (
                        "INCONCLUSIVE: with_skill/outputs/invocation.json not "
                        f"found in {case_dir}. Populate this file by running "
                        "the prompt through Claude Code with the skill "
                        "installed, capturing the final pocsynth.py argv, "
                        "stdout, and exit_code."
                    ),
                }
            )
            continue

        passed, evidence = _grade_deterministic(assertion, invocation)
        expectations.append(
            {"text": assertion["text"], "passed": passed, "evidence": evidence}
        )

    return {
        "case_id": case_def["id"],
        "case_name": case_def.get("name"),
        "expectations": expectations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic grader for pocsynth skill evaluations."
    )
    parser.add_argument(
        "--workspace",
        required=True,
        type=Path,
        help="Path to the iteration workspace, e.g. skills/pocsynth-workspace/iteration-1",
    )
    parser.add_argument(
        "--case-id",
        help="Grade only this case id; otherwise grade all cases in evals.json",
    )
    args = parser.parse_args()

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["evals"]
    if args.case_id:
        cases = [c for c in cases if c["id"] == args.case_id]
        if not cases:
            print(f"no case with id={args.case_id!r}", file=sys.stderr)
            return 2

    any_failed = False
    for case_def in cases:
        case_dir = args.workspace / f"case-{case_def['id']}"
        grading = _grade_case(case_def, case_dir)
        out = case_dir / "deterministic_grading.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(grading, indent=2) + "\n", encoding="utf-8")

        passed = sum(1 for e in grading["expectations"] if e["passed"])
        total = len(grading["expectations"])
        mark = "PASS" if passed == total else "FAIL"
        if passed < total:
            any_failed = True
        print(f"[{mark}] {case_def['id']:30s} {passed}/{total} deterministic")
        if "CI" in os.environ and passed < total:
            for exp in grading["expectations"]:
                if not exp["passed"]:
                    print(f"        - {exp['text']}: {exp['evidence']}")

    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
