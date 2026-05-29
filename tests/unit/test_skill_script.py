# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Feature-compatibility tests between the installed `pocsynth` Typer CLI
and the bundled single-file skill script at skills/pocsynth/pocsynth.py.

Both must produce byte-equal JSON envelopes for machine-mode invocations.
This is the deterministic layer of skill testing; the behavioral layer
(Claude + AskUserQuestion + fast-mode semantics) lives under
skills/pocsynth/evals/ and is run manually via scripts/run-skill-evals.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_SCRIPT = REPO_ROOT / "skills" / "pocsynth" / "pocsynth.py"


def _run_script(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the bundled skill script with the venv's Python directly.

    We bypass the PEP 723 shebang (which would call `uv run --script` and
    resolve a second, ephemeral env) and instead run the script in the
    dev venv, which already has every required dep. Behavior is identical;
    tests are faster and don't depend on uv's remote-cache state.
    """
    return subprocess.run(
        [sys.executable, str(SKILL_SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_installed(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pocsynth", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def skill_script_exists() -> None:
    if not SKILL_SCRIPT.exists():
        pytest.skip(
            "skills/pocsynth/pocsynth.py not found; "
            "run `uv run python scripts/generate-skill-script.py` first"
        )


class TestFeatureCompat:
    def test_models_json_byte_equal(self, skill_script_exists):
        a = _run_script(["--json", "models"])
        b = _run_installed(["--json", "models"])
        assert a.returncode == b.returncode == 0, (a.stderr, b.stderr)
        assert a.stdout == b.stdout, (
            f"skill stdout:\n{a.stdout}\ninstalled stdout:\n{b.stdout}"
        )

    def test_version_json_shape_matches(self, skill_script_exists):
        a = _run_script(["--json", "version"])
        b = _run_installed(["--json", "version"])
        assert a.returncode == b.returncode == 0
        ea = json.loads(a.stdout)
        eb = json.loads(b.stdout)
        assert ea == eb

    @pytest.mark.parametrize(
        "args,expected_exit,expected_code",
        [
            (["--json", "convert", "/tmp/pocsynth-nonexistent.pdf"], 3, "INPUT_NOT_FOUND"),
            (["--json", "convert", "http://example.com/x.pdf"], 3, "URL_REJECTED"),
        ],
    )
    def test_error_envelopes_match(
        self, skill_script_exists, args, expected_exit, expected_code
    ):
        a = _run_script(args)
        b = _run_installed(args)
        assert a.returncode == b.returncode == expected_exit, (a.stderr, b.stderr)
        ea = json.loads(a.stdout)
        eb = json.loads(b.stdout)
        # Top-level envelope must match exactly (schema, command, event, ok, error.code)
        assert ea["ok"] == eb["ok"] is False
        assert ea["schema"] == eb["schema"] == 1
        assert ea["command"] == eb["command"]
        assert ea["event"] == eb["event"] == "complete"
        assert ea["error"]["code"] == eb["error"]["code"] == expected_code
        assert ea["error"]["retryable"] == eb["error"]["retryable"]
        # Messages and hints should be identical (same source, same format strings)
        assert ea["error"]["message"] == eb["error"]["message"]
        assert ea["error"]["hint"] == eb["error"]["hint"]

    def test_help_text_length_parity(self, skill_script_exists):
        """Typer's help output is terminal-width-dependent, so we don't
        assert byte-equality. We DO assert the two helps are close in
        length (within 5%) and both mention the required subcommands.
        """
        a = _run_script(["--help"])
        b = _run_installed(["--help"])
        assert a.returncode == b.returncode == 0
        la, lb = len(a.stdout), len(b.stdout)
        assert max(la, lb) / min(la, lb) <= 1.05, (la, lb)
        for token in ("convert", "pii-audit", "models", "doctor", "version"):
            assert token in a.stdout
            assert token in b.stdout


class TestSelfContained:
    """Sanity checks that the script stands alone — no pocsynth import leakage."""

    def test_script_does_not_depend_on_src_pocsynth(
        self, skill_script_exists, tmp_path
    ):
        """Copy the script to a temp dir outside the repo, run it there, and
        confirm it still works. This catches accidental sys.path fiddling
        that would only succeed when run from the repo root.
        """
        dest = tmp_path / "pocsynth.py"
        dest.write_bytes(SKILL_SCRIPT.read_bytes())
        dest.chmod(0o755)

        # Strip PYTHONPATH so we don't accidentally pick up the repo's src/
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

        result = subprocess.run(
            [sys.executable, str(dest), "--json", "version"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        env_out = json.loads(result.stdout)
        assert env_out["ok"] is True
        assert env_out["schema"] == 1


class TestPEP723Header:
    """Verify the generated script carries the metadata that makes it
    self-deploying via `uv run --script`."""

    def test_shebang_is_uv_script(self, skill_script_exists):
        first = SKILL_SCRIPT.read_text(encoding="utf-8").splitlines()[0]
        assert first == "#!/usr/bin/env -S uv run --script"

    def test_declares_core_runtime_deps(self, skill_script_exists):
        header = SKILL_SCRIPT.read_text(encoding="utf-8").split("\n", 80)
        blob = "\n".join(header)
        # Must contain PEP 723 block opener and every runtime dep.
        assert "# /// script" in blob
        for pkg in ("boto3", "pymupdf", "typer", "rich", "beautifulsoup4",
                    "html2text", "requests"):
            assert pkg in blob, f"missing {pkg} in PEP 723 header"

    def test_marked_as_generated(self, skill_script_exists):
        assert "GENERATED FILE" in SKILL_SCRIPT.read_text(encoding="utf-8")[:1024]
