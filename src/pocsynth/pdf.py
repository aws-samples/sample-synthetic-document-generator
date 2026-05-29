# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""PDF fetching + SSRF-safe URL validation."""

from __future__ import annotations

import ipaddress
import logging
import socket
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlparse

import requests

from pocsynth.errors import HttpError, InputError, InputNotPdfError, UrlRejectedError

logger = logging.getLogger(__name__)

MAX_REMOTE_PDF_BYTES = 100 * 1024 * 1024  # 100 MB
_MAX_REDIRECTS = 5


def _reject_url(reason: str, url: str, **context) -> UrlRejectedError:
    ctx = {"url": url, "reason": reason, **context}
    return UrlRejectedError(
        f"URL rejected: {reason}",
        context=ctx,
        hint="Use an https:// URL whose hostname resolves to a public IP",
    )


def validate_safe_url(url: str) -> str | None:
    """Return an error string if the URL is unsafe to fetch, else None.

    Preserved (None-returning) signature because existing unit tests drive
    this directly. get_pdf_file() raises UrlRejectedError instead.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return f"scheme {parsed.scheme!r} not allowed (https only)"
    if not parsed.hostname:
        return "missing hostname"

    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        return f"DNS resolution failed: {exc}"

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return f"host {parsed.hostname} resolves to disallowed address {addr}"
    return None


def _classify_url_error(url: str, err: str) -> UrlRejectedError:
    if "https only" in err:
        return _reject_url("non_https_scheme", url, detail=err)
    if "missing hostname" in err:
        return _reject_url("missing_hostname", url, detail=err)
    if "DNS resolution failed" in err:
        return _reject_url("dns_failure", url, detail=err)
    if "disallowed address" in err:
        return _reject_url("private_or_reserved_ip", url, detail=err)
    return _reject_url("unknown", url, detail=err)


def get_pdf_file(url_or_path: str) -> bytes:
    """Load a PDF from either a URL or a local path.

    Raises a DocSynthError subclass on failure (UrlRejectedError,
    InputError, InputNotPdfError, or HttpError) with structured context.
    """
    is_url = "://" in url_or_path
    # For URLs, check the path component only — query strings and fragments
    # (e.g. presigned-S3 URLs like ".../foo.pdf?X-Amz-Signature=...") would
    # otherwise fail a naive endswith(".pdf") check.
    if is_url:
        path_suffix = PurePosixPath(urlparse(url_or_path).path).suffix.lower()
    else:
        path_suffix = PurePosixPath(url_or_path).suffix.lower()
    if path_suffix != ".pdf":
        raise InputNotPdfError(
            f"'{url_or_path}' is not a PDF file (extension check)",
            context={"path": url_or_path},
            hint="Ensure the path ends in .pdf",
        )

    if is_url:
        return _fetch_url(url_or_path)

    try:
        with open(url_or_path, "rb") as pdf_file:
            return pdf_file.read()
    except FileNotFoundError as exc:
        raise InputError(
            f"File not found: {url_or_path}",
            context={"path": url_or_path},
            hint="Check the path, or pass an https:// URL",
        ) from exc
    except OSError as exc:
        raise InputError(
            f"Error reading {url_or_path}: {exc}",
            context={"path": url_or_path, "os_error": str(exc)},
        ) from exc


def _fetch_url(url: str) -> bytes:
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        err = validate_safe_url(current_url)
        if err:
            raise _classify_url_error(current_url, err)

        try:
            response = requests.get(
                current_url,
                timeout=30,
                stream=True,
                verify=True,
                allow_redirects=False,
            )
        except requests.exceptions.Timeout as exc:
            raise HttpError(
                f"Timeout fetching {current_url}",
                context={"url": current_url, "reason": "timeout"},
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise HttpError(
                f"Error fetching {current_url}: {exc}",
                context={"url": current_url, "reason": "connection_error", "detail": str(exc)},
            ) from exc

        if response.is_redirect or response.is_permanent_redirect:
            next_url = response.headers.get("Location")
            response.close()
            if not next_url:
                raise HttpError(
                    f"Redirect from {current_url} had no Location header",
                    context={"url": current_url, "reason": "redirect_without_location"},
                )
            # Resolve relative redirects (RFC 7231 permits Location to be a
            # relative reference) against the current URL before re-validating.
            current_url = urljoin(current_url, next_url)
            continue

        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            status = response.status_code
            response.close()
            raise HttpError(
                f"HTTP error fetching {current_url}: {exc}",
                context={
                    "url": current_url,
                    "reason": "http_error",
                    "status_code": status,
                },
            ) from exc

        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("application/pdf"):
            response.close()
            raise InputNotPdfError(
                f"URL {current_url} did not return application/pdf (got {content_type})",
                context={"url": current_url, "content_type": content_type},
            )

        declared_size = response.headers.get("Content-Length")
        if declared_size:
            try:
                parsed_size = int(declared_size)
            except ValueError as exc:
                response.close()
                raise HttpError(
                    f"Remote server returned non-numeric Content-Length: {declared_size!r}",
                    context={
                        "url": current_url,
                        "reason": "malformed_content_length",
                        "declared_size": declared_size,
                    },
                ) from exc
            if parsed_size > MAX_REMOTE_PDF_BYTES:
                response.close()
                raise HttpError(
                    f"Remote PDF exceeds size cap ({parsed_size} bytes > {MAX_REMOTE_PDF_BYTES})",
                    context={
                        "url": current_url,
                        "reason": "size_cap_exceeded",
                        "declared_size": parsed_size,
                        "max_bytes": MAX_REMOTE_PDF_BYTES,
                    },
                )

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            total += len(chunk)
            if total > MAX_REMOTE_PDF_BYTES:
                raise HttpError(
                    "Remote PDF exceeded size cap during download",
                    context={
                        "url": current_url,
                        "reason": "size_cap_exceeded_streaming",
                        "max_bytes": MAX_REMOTE_PDF_BYTES,
                    },
                )
            chunks.append(chunk)
        return b"".join(chunks)

    raise HttpError(
        f"Too many redirects (>{_MAX_REDIRECTS}) starting from {url}",
        context={"url": url, "reason": "too_many_redirects", "max": _MAX_REDIRECTS},
    )
