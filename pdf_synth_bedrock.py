# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deprecation shim.

This module previously held the entire application. It has been refactored
into the `pocsynth` package; this shim re-exports the former public symbols
so existing imports keep working. New code should import from `pocsynth.*`
or invoke the `pocsynth` CLI.
"""

from __future__ import annotations

import warnings

warnings.warn(
    "pdf_synth_bedrock is deprecated; use the 'pocsynth' CLI or import from pocsynth.* instead.",
    DeprecationWarning,
    stacklevel=2,
)

from pocsynth.bedrock import MODELS, process_page  # noqa: E402, F401
from pocsynth.comprehend import (  # noqa: E402, F401
    COMPREHEND_MAX_TEXT_LENGTH,
    scan_for_pii,
)
from pocsynth.pdf import (  # noqa: E402, F401
    MAX_REMOTE_PDF_BYTES,
    get_pdf_file,
    validate_safe_url,
)
from pocsynth.prompts import (  # noqa: E402, F401
    PII_CATEGORIES,
    build_prompt,
    build_system_prompt,
)
from pocsynth.textutil import (  # noqa: E402, F401
    convert_html_to_markdown,
    sanitize_filename_part,
    strip_model_preamble,
)


def main() -> None:
    """Legacy entry point — delegates to the new Typer CLI."""
    import sys

    from pocsynth.cli import app

    sys.argv[0] = "pocsynth"
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
