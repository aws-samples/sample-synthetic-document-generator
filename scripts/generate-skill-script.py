#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bundle `src/pocsynth/` into a single Python file for the Claude Code skill.

Produces `skills/pocsynth/pocsynth.py`:
  1. Stickytape inlines the `pocsynth.*` package graph into one module.
  2. We prepend a PEP 723 inline-script-metadata header so the file can be
     run directly via `uv run --script` (or the shebang `#!/usr/bin/env -S
     uv run --script`), resolving deps into an ephemeral cached venv on
     first use. No prior `pip install` required — the user only needs uv.

Run:
    uv run python scripts/generate-skill-script.py

The output is committed; CI (`.gitlab-ci.yml:skill-script-drift`) re-runs
this and fails the MR if the committed file has drifted.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY = REPO_ROOT / "src" / "pocsynth" / "__main__.py"
OUT = REPO_ROOT / "skills" / "pocsynth" / "pocsynth.py"
PRICING = REPO_ROOT / "src" / "pocsynth" / "pricing.json"

# Generated from pyproject.toml [project.dependencies]. Keep in sync manually
# (the CI drift check will catch staleness if this list doesn't match reality,
# since the resulting script will fail to import one of its deps at run-time).
PEP723_HEADER = """\
#!/usr/bin/env -S uv run --script
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# GENERATED FILE - DO NOT EDIT BY HAND.
# Source: src/pocsynth/ (see pyproject.toml)
# Regenerate: uv run python scripts/generate-skill-script.py
#
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "boto3>=1.42,<2",
#     "pymupdf>=1.27,<2",
#     "beautifulsoup4>=4.14,<5",
#     "requests>=2.33,<3",
#     "html2text>=2025.4.15",
#     "charset-normalizer>=3.4,<4",
#     "typer>=0.12,<1",
#     "rich>=13,<15",
#     "faker>=37,<38",
# ]
# ///
"""


def ensure_entry_module() -> None:
    """Create src/pocsynth/__main__.py so stickytape has a single entry point.

    Stickytape inlines starting from a script; we want the skill script to
    execute the Typer app. Writing a tiny __main__.py that imports and
    invokes `app` gives stickytape a clean root.
    """
    content = (
        "# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\n"
        "# SPDX-License-Identifier: MIT-0\n"
        '"""Entry point for `python -m pocsynth` and the bundled skill script."""\n\n'
        "from pocsynth.cli import app\n\n"
        'if __name__ == "__main__":\n'
        "    app()\n"
    )
    if not ENTRY.exists() or ENTRY.read_text() != content:
        ENTRY.write_text(content)


def run_stickytape() -> str:
    """Invoke stickytape as a subprocess. Returns the single-file source."""
    # stickytape installs a console script; locate it in the active venv.
    stickytape = Path(sys.executable).parent / "stickytape"
    if not stickytape.exists():
        raise SystemExit(
            "stickytape not found on PATH. Run `uv sync --all-groups` first."
        )
    cmd = [
        str(stickytape),
        str(ENTRY),
        "--add-python-path",
        str(REPO_ROOT / "src"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"stickytape failed (exit {result.returncode}). See stderr above."
        )
    return result.stdout


def strip_stickytape_shebang(body: str) -> str:
    """Stickytape output starts with its own shebang/marker. Drop it — our
    PEP 723 header supplies the shebang.
    """
    lines = body.splitlines(keepends=True)
    while lines and (lines[0].startswith("#!") or lines[0].startswith("# -*-")):
        lines.pop(0)
    return "".join(lines)


def pricing_inline_block() -> str:
    """Return a Python stanza that inlines pricing.json as a module constant
    and patches pocsynth.pricing.load_pricing to serve it by default.

    Stickytape only inlines .py source, not package data. The skill script
    needs pricing.json available at runtime even when copied outside the
    repo, so we embed the JSON as a literal string and monkey-patch the
    default path.
    """
    pricing_text = PRICING.read_text(encoding="utf-8")
    return (
        "\n# ---- pocsynth bundled pricing.json (inlined by generate-skill-script.py) ----\n"
        "import json as _pocsynth_bundle_json\n"
        f"_POCSYNTH_BUNDLED_PRICING = _pocsynth_bundle_json.loads({pricing_text!r})\n"
        "def _pocsynth_install_bundled_pricing():\n"
        "    from pocsynth import pricing as _p\n"
        "    _original_load = _p.load_pricing\n"
        "    def _patched_load(path=None):\n"
        "        if path is None:\n"
        "            _p._validate_pricing_shape(_POCSYNTH_BUNDLED_PRICING)\n"
        "            return _POCSYNTH_BUNDLED_PRICING\n"
        "        return _original_load(path)\n"
        "    _p.load_pricing = _patched_load\n"
        "_pocsynth_install_bundled_pricing()\n"
        "# ---- end bundled pricing ----\n"
    )


def presets_inline_block() -> str:
    """Inline the bundled preset schema JSON files + patch pocsynth.presets to
    serve them. Like pricing.json, importlib.resources can't find package data
    in the flattened skill script, so embed each preset as a literal.
    """
    import json as _json

    presets_dir = REPO_ROOT / "src" / "pocsynth" / "presets"
    data = {
        p.stem: _json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(presets_dir.glob("*.json"))
    }
    return (
        "\n# ---- pocsynth bundled presets (inlined by generate-skill-script.py) ----\n"
        "import json as _pocsynth_presets_json\n"
        f"_POCSYNTH_BUNDLED_PRESETS = _pocsynth_presets_json.loads({_json.dumps(data)!r})\n"
        "def _pocsynth_install_bundled_presets():\n"
        "    from pocsynth import presets as _ps\n"
        "    from pocsynth.schema import _validate_schema_shape as _v\n"
        "    def _patched_load(name):\n"
        "        if name not in _POCSYNTH_BUNDLED_PRESETS:\n"
        "            from pocsynth.errors import SchemaError as _SE\n"
        "            raise _SE(f'unknown preset: {name!r}', context={'preset': name})\n"
        "        d = _POCSYNTH_BUNDLED_PRESETS[name]\n"
        "        _v(d)\n"
        "        return d\n"
        "    _ps.load_preset = _patched_load\n"
        "_pocsynth_install_bundled_presets()\n"
        "# ---- end bundled presets ----\n"
    )


def main() -> None:
    ensure_entry_module()

    body = strip_stickytape_shebang(run_stickytape())
    # Stickytape wraps everything in `with __stickytape_temporary_dir():`
    # and appends the runtime entry point inside that block. To inject the
    # pricing patch at the right moment, find the entry-point line — which
    # is the LAST `from pocsynth.cli import app` line at indent level 4
    # (ignoring occurrences inside the embedded __stickytape_write_module
    # byte-string literals, which will be at indent level 4 too but end
    # with `')` on the same line because they're inside a function arg).
    body_lines = body.splitlines(keepends=True)
    entry_idx = None
    for i in range(len(body_lines) - 1, -1, -1):
        line = body_lines[i]
        # The real entry-point line starts with 4 spaces and is the clean
        # import statement. Embedded module strings wrap the text inside
        # `__stickytape_write_module('pocsynth/__main__.py', b'...')` which
        # starts with `    __stickytape_write_module(`.
        if line.startswith("    from pocsynth.cli import app"):
            entry_idx = i
            break
    if entry_idx is None:
        raise SystemExit(
            "Could not locate the 'from pocsynth.cli import app' entry-point "
            "line in stickytape output. Bundler output format may have changed."
        )
    # pricing_inline_block() returns unindented (module-level) code, but
    # it needs to land inside the with-block at indent 4 so variable scopes
    # line up. Re-indent every line by 4 spaces.
    patch = "".join(
        ("    " + ln if ln.strip() else ln)
        for ln in (pricing_inline_block() + presets_inline_block()).splitlines(keepends=True)
    )
    body = (
        "".join(body_lines[:entry_idx])
        + patch
        + "".join(body_lines[entry_idx:])
    )
    final = PEP723_HEADER + "\n" + body

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(final)
    OUT.chmod(0o755)
    print(f"wrote {OUT.relative_to(REPO_ROOT)} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
