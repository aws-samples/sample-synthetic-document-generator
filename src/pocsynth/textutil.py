# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Text utilities: HTML→Markdown, filename sanitisation, model-preamble stripping."""

import re

import html2text


def convert_html_to_markdown(html_content: str) -> str:
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_links = False
    converter.ignore_images = False
    return converter.handle(html_content)


def sanitize_filename_part(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return safe or "document"


def strip_model_preamble(text: str) -> str:
    """Remove a leading ```...``` fence or 'Here is ...' preamble, if present."""
    stripped = text.lstrip()
    fence = re.match(r"^```[a-zA-Z]*\n(.*?)\n```\s*$", stripped, flags=re.DOTALL)
    if fence:
        return fence.group(1)
    stripped = re.sub(r"^```[a-zA-Z]*\n", "", stripped)
    stripped = re.sub(r"\n```\s*$", "", stripped)
    stripped = re.sub(
        r"^(Here(?:'s| is)[^\n]{0,200}\n)",
        "",
        stripped,
        count=1,
        flags=re.IGNORECASE,
    )
    return stripped
