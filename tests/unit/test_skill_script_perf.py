# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cold-start performance guard for the bundled skill script.

If someone adds a heavy top-level import to pocsynth.* (boto3 is already
the slowest one; anything else added at module scope will noticeably hurt
first-call latency), this test catches it before it ships.

Budget: `./skills/pocsynth/pocsynth.py --json version` in <2s using the
dev venv's Python (so we're measuring import + startup, not uv's
ephemeral-env resolution).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_SCRIPT = REPO_ROOT / "skills" / "pocsynth" / "pocsynth.py"

# Budget: 2 seconds is comfortable on dev hardware and leaves headroom for
# slower CI runners. If this ever fails legitimately because the runner is
# slow, bump the budget with a comment explaining why — don't delete the test.
COLD_START_BUDGET_SECONDS = 2.0


@pytest.mark.skipif(
    not SKILL_SCRIPT.exists(),
    reason="skills/pocsynth/pocsynth.py missing; run the generator first",
)
def test_version_cold_start_under_budget():
    """Run `--json version`, measure wall time, assert under budget.

    We use the dev venv's Python directly (NOT the script's uv-script
    shebang) so the measurement is just Python startup + pocsynth imports
    + writing the version envelope. That isolates what we can regress on.
    """
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(SKILL_SCRIPT), "--json", "version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0, result.stderr
    assert elapsed < COLD_START_BUDGET_SECONDS, (
        f"`--json version` took {elapsed:.2f}s (budget {COLD_START_BUDGET_SECONDS}s). "
        "Something heavy was added at module scope — most likely a new top-level "
        "import in pocsynth.*. Check the diff and defer the import into the "
        "function that needs it."
    )
