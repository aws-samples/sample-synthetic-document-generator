# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deprecation shim tests.

`pdf_synth_bedrock` must:
  * Emit a DeprecationWarning on import.
  * Re-export the former public names from the new pocsynth.* modules.
"""

import importlib
import sys
import warnings


def test_import_emits_deprecation_warning():
    # Ensure a clean import even if other tests already loaded the shim.
    sys.modules.pop("pdf_synth_bedrock", None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module("pdf_synth_bedrock")
    messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    assert any("pdf_synth_bedrock is deprecated" in m for m in messages), caught


def test_reexports_resolve():
    sys.modules.pop("pdf_synth_bedrock", None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        shim = importlib.import_module("pdf_synth_bedrock")
    # Spot-check each re-export resolves to the expected new module path.
    from pocsynth.bedrock import MODELS as _MODELS
    from pocsynth.bedrock import process_page as _process_page
    from pocsynth.comprehend import scan_for_pii as _scan_for_pii
    from pocsynth.pdf import (
        MAX_REMOTE_PDF_BYTES as _MAX_REMOTE_PDF_BYTES,
    )
    from pocsynth.pdf import (
        get_pdf_file as _get_pdf_file,
    )
    from pocsynth.pdf import (
        validate_safe_url as _validate_safe_url,
    )
    from pocsynth.prompts import build_prompt as _build_prompt
    from pocsynth.prompts import build_system_prompt as _build_system_prompt
    from pocsynth.textutil import (
        convert_html_to_markdown as _convert_html_to_markdown,
    )
    from pocsynth.textutil import (
        sanitize_filename_part as _sanitize_filename_part,
    )
    from pocsynth.textutil import (
        strip_model_preamble as _strip_model_preamble,
    )

    assert shim.MODELS is _MODELS
    assert shim.process_page is _process_page
    assert shim.scan_for_pii is _scan_for_pii
    assert shim.MAX_REMOTE_PDF_BYTES is _MAX_REMOTE_PDF_BYTES
    assert shim.get_pdf_file is _get_pdf_file
    assert shim.validate_safe_url is _validate_safe_url
    assert shim.build_prompt is _build_prompt
    assert shim.build_system_prompt is _build_system_prompt
    assert shim.convert_html_to_markdown is _convert_html_to_markdown
    assert shim.sanitize_filename_part is _sanitize_filename_part
    assert shim.strip_model_preamble is _strip_model_preamble
