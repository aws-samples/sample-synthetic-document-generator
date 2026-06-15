# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""High-level per-document conversion flow.

`run_conversion(cfg, on_event=None)` is what `cli.py convert` calls. The
`on_event(name, **kwargs)` callback is used for NDJSON streaming; it's a
no-op in non-stream mode.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
from bs4 import BeautifulSoup

from pocsynth.bedrock import MODELS, make_session, process_page
from pocsynth.comprehend import scan_for_pii
from pocsynth.errors import InputError, InputNotPdfError, PartialError, UpstreamError
from pocsynth.pdf import get_pdf_file
from pocsynth.prompts import build_prompt, build_system_prompt
from pocsynth.textutil import (
    convert_html_to_markdown,
    sanitize_filename_part,
    strip_model_preamble,
)

logger = logging.getLogger(__name__)


@dataclass
class ConversionConfig:
    pdf_url: str
    model_key: str = "sonnet"
    export_format: str = "html"       # "html" or "markdown"
    synthetic: bool = True
    system_prompt_user: str = ""
    num_pages: int | None = None
    num_docs: int = 1
    pii_audit: bool = True
    redact_values: bool = False
    max_tokens: int = 8000
    region: str | None = None
    profile: str | None = None
    output_dir: str | None = None      # parent directory; default cwd
    # Injectable clients (used by tests; otherwise created from profile/region)
    bedrock_client: Any = field(default=None, repr=False)
    comprehend_client: Any = field(default=None, repr=False)


EventCallback = Callable[..., None] | None


def _noop(*_args, **_kwargs) -> None:
    pass


def run_conversion(cfg: ConversionConfig, on_event: EventCallback = None) -> dict[str, Any]:
    """Execute the conversion. Returns the full `result` dict for the JSON envelope."""
    emit = on_event or _noop
    model = MODELS[cfg.model_key]
    format_label = "HTML" if cfg.export_format == "html" else "Markdown"
    prompt_template = build_prompt(cfg.synthetic, format_label)

    base_system_prompt = build_system_prompt(format_label)
    if cfg.system_prompt_user.strip():
        system_prompts = [{"text": f"{base_system_prompt}\n\n{cfg.system_prompt_user.strip()}"}]
    else:
        system_prompts = [{"text": base_system_prompt}]

    pdf_bytes = get_pdf_file(cfg.pdf_url)

    # One session per conversion, reused for Bedrock + Comprehend so both
    # services bind to the same resolved profile/region. Tests that inject
    # both clients skip session creation entirely.
    needs_session = cfg.bedrock_client is None or (
        cfg.pii_audit and cfg.comprehend_client is None
    )
    bedrock_client = cfg.bedrock_client
    comprehend_client = cfg.comprehend_client
    if needs_session:
        session = make_session(profile=cfg.profile, region=cfg.region)
        if bedrock_client is None:
            bedrock_client = session.client("bedrock-runtime")
        if cfg.pii_audit and comprehend_client is None:
            comprehend_client = session.client("comprehend")

    filename = sanitize_filename_part(Path(cfg.pdf_url).stem)
    extension = ".html" if cfg.export_format == "html" else ".md"

    parent = Path(cfg.output_dir) if cfg.output_dir else Path.cwd()
    parent.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    total_input_tokens = 0
    total_output_tokens = 0
    pages_processed = 0
    pages_attempted = 0
    per_page_paths: list[str] = []
    per_page_images: list[str] = []
    combined_path_final: str = ""
    folder_name_final: str = ""
    page_failures: list[dict] = []
    pii_audit_path: str | None = None
    entities_found: int = 0
    try:
        doc_cm = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # pymupdf raises fitz.FileDataError / RuntimeError
        raise InputNotPdfError(
            f"Could not parse PDF: {exc}",
            context={"path": cfg.pdf_url, "exception": type(exc).__name__},
            hint="Verify the file is a valid, non-encrypted PDF",
        ) from exc

    with doc_cm as doc:
        total_pages = len(doc)
        if cfg.num_pages is not None:
            total_pages = min(total_pages, cfg.num_pages)

        emit("conversion_started", total_pages=total_pages, model=cfg.model_key)

        for iteration in range(cfg.num_docs):
            page_outputs: list[str] = []
            folder_path = parent / f"{filename}_{iteration + 1}"
            folder_path.mkdir(exist_ok=True)
            folder_name_final = str(folder_path)

            for page_num in range(len(doc)):
                if cfg.num_pages and page_num >= cfg.num_pages:
                    break
                pages_attempted += 1
                page = doc.load_page(page_num)
                pix = page.get_pixmap()
                img_bytes = pix.tobytes()

                img_path = folder_path / f"{filename}_page_{page_num + 1}.png"
                pix.save(str(img_path))

                emit("page_started", page=page_num + 1, of=total_pages)

                try:
                    page_result = process_page(
                        bedrock_client,
                        model["id"],
                        system_prompts,
                        prompt_template,
                        page,
                        page_num,
                        img_bytes,
                        cfg.max_tokens,
                    )
                except UpstreamError as exc:
                    page_failures.append(
                        {"page": page_num + 1, "error": exc.code, "message": exc.message}
                    )
                    emit(
                        "page_failed",
                        page=page_num + 1,
                        of=total_pages,
                        error_code=exc.code,
                        message=exc.message,
                    )
                    continue

                response_message = strip_model_preamble(page_result["text"])
                total_input_tokens += page_result["usage"]["input_tokens"]
                total_output_tokens += page_result["usage"]["output_tokens"]

                if cfg.export_format == "html":
                    soup = BeautifulSoup(response_message, "html.parser")
                    page_html = soup.decode(formatter="html")
                    page_section = (
                        f'<section data-page="{page_num + 1}">\n'
                        f"{page_html}\n"
                        f'<p>Page <data value="{page_num + 1}">{page_num + 1}</data></p>\n'
                        f"</section>\n"
                    )
                    page_outputs.append(page_section)
                    page_file_content = (
                        "<!DOCTYPE html>\n<html><body>\n"
                        f"{page_section}"
                        "</body></html>\n"
                    )
                else:
                    md = convert_html_to_markdown(response_message)
                    page_section = f"{md}\n\nPage: {page_num + 1}\n\n"
                    page_outputs.append(page_section)
                    page_file_content = page_section

                output_path = folder_path / f"{filename}_page_{page_num + 1}{extension}"
                with open(output_path, "w", encoding="utf-8") as output_file:
                    output_file.write(page_file_content)
                per_page_paths.append(str(output_path))
                # Image is registered only after the page successfully
                # produced output, keeping per_page_images and per_page_paths
                # length-aligned for failed pages.
                per_page_images.append(str(img_path))

                pages_processed += 1
                emit(
                    "page_processed",
                    page=page_num + 1,
                    of=total_pages,
                    page_path=str(output_path),
                )

            combined = "".join(page_outputs)
            if cfg.export_format == "html":
                combined_doc = (
                    "<!DOCTYPE html>\n<html><body>\n"
                    f"{combined}"
                    "</body></html>\n"
                )
                cleaned = BeautifulSoup(combined_doc, "html.parser").prettify()
            else:
                cleaned = combined

            cleaned_path = folder_path / f"{filename}_cleaned{extension}"
            with open(cleaned_path, "w", encoding="utf-8") as cleaned_file:
                cleaned_file.write(cleaned)
            combined_path_final = str(cleaned_path)

            if cfg.pii_audit:
                detected = scan_for_pii(
                    cleaned,
                    folder_name=str(parent / "pii-audit"),
                    filename=filename,
                    comprehend=comprehend_client,
                    redact_values=cfg.redact_values,
                )
                entities_found = len(detected)
                pii_audit_path = str(
                    parent / "pii-audit" / f"{filename}_pii_scan_audit.csv"
                )

    wall_time = round(time.monotonic() - start, 3)

    if pages_attempted == 0:
        raise InputError(
            "PDF contained no pages to process",
            context={"pdf": cfg.pdf_url},
        )

    if pages_processed == 0 and page_failures:
        # Every attempted page failed — surface a structured PartialError so
        # the CLI exit code (6) and JSON envelope reflect the failure rather
        # than reporting `ok: true` with empty output.
        raise PartialError(
            f"All {pages_attempted} attempted pages failed",
            context={
                "pdf": cfg.pdf_url,
                "pages_attempted": pages_attempted,
                "pages_processed": 0,
                "page_failures": page_failures,
            },
            hint="Inspect page_failures for the underlying Bedrock errors",
        )

    result = {
        "input": {
            "path": cfg.pdf_url,
            "format": cfg.export_format,
            "mode": "synthetic" if cfg.synthetic else "real",
        },
        "output": {
            "dir": folder_name_final,
            "combined_path": combined_path_final,
            "per_page_paths": per_page_paths,
            "per_page_images": per_page_images,
            "pages_processed": pages_processed,
            "pages_attempted": pages_attempted,
            "wall_time_seconds": wall_time,
            "bedrock_usage": {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            },
        },
        "pii_audit": {
            "enabled": cfg.pii_audit,
            "path": pii_audit_path if cfg.pii_audit else None,
            "redacted": cfg.redact_values if cfg.pii_audit else False,
            "entities_found": entities_found if cfg.pii_audit else 0,
        },
        "page_failures": page_failures,
    }
    return result
