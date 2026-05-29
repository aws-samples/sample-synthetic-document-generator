# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from unittest.mock import MagicMock, patch

import pytest
import requests

from pocsynth.errors import HttpError, InputError, InputNotPdfError, UrlRejectedError
from pocsynth.pdf import MAX_REMOTE_PDF_BYTES, get_pdf_file, validate_safe_url


def _fake_response(*, status=200, content_type="application/pdf",
                   content=b"%PDF-1.4\n", content_length=None):
    resp = MagicMock()
    resp.status_code = status
    resp.is_redirect = False
    resp.is_permanent_redirect = False
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError()
    headers = {"Content-Type": content_type}
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    resp.headers = headers
    resp.iter_content = MagicMock(return_value=iter([content]))
    return resp


class TestGetPdfFileUrl:
    def test_downloads_pdf_from_url(self):
        with patch("pocsynth.pdf.requests.get",
                   return_value=_fake_response(content=b"%PDF-1.4 data")) as mock_get:
            result = get_pdf_file("https://example.com/doc.pdf")
        assert result == b"%PDF-1.4 data"
        mock_get.assert_called_once()

    def test_rejects_non_pdf_content_type(self):
        with patch("pocsynth.pdf.requests.get",
                   return_value=_fake_response(content_type="text/html")):
            with pytest.raises(InputNotPdfError):
                get_pdf_file("https://example.com/doc.pdf")

    def test_rejects_declared_oversize(self):
        with patch("pocsynth.pdf.requests.get",
                   return_value=_fake_response(content_length=MAX_REMOTE_PDF_BYTES + 1)):
            with pytest.raises(HttpError) as excinfo:
                get_pdf_file("https://example.com/big.pdf")
        assert excinfo.value.context["reason"] == "size_cap_exceeded"

    def test_timeout_raises(self):
        with patch("pocsynth.pdf.requests.get",
                   side_effect=requests.exceptions.Timeout()):
            with pytest.raises(HttpError) as excinfo:
                get_pdf_file("https://example.com/doc.pdf")
        assert excinfo.value.context["reason"] == "timeout"

    def test_connection_error_raises(self):
        with patch("pocsynth.pdf.requests.get",
                   side_effect=requests.exceptions.ConnectionError()):
            with pytest.raises(HttpError) as excinfo:
                get_pdf_file("https://example.com/doc.pdf")
        assert excinfo.value.context["reason"] == "connection_error"

    def test_streamed_oversize_aborts(self):
        big_chunks = [b"x" * (64 * 1024)] * 2000  # 128 MB
        resp = MagicMock()
        resp.status_code = 200
        resp.is_redirect = False
        resp.is_permanent_redirect = False
        resp.raise_for_status = MagicMock()
        resp.headers = {"Content-Type": "application/pdf"}
        resp.iter_content = MagicMock(return_value=iter(big_chunks))
        with patch("pocsynth.pdf.requests.get", return_value=resp):
            with pytest.raises(HttpError) as excinfo:
                get_pdf_file("https://example.com/big.pdf")
        assert excinfo.value.context["reason"] == "size_cap_exceeded_streaming"


class TestGetPdfFileLocal:
    def test_reads_local_pdf(self, tmp_path):
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF-1.4 local")
        assert get_pdf_file(str(p)) == b"%PDF-1.4 local"

    def test_missing_file(self, tmp_path):
        missing = tmp_path / "nope.pdf"
        with pytest.raises(InputError):
            get_pdf_file(str(missing))


class TestGetPdfFileValidation:
    @pytest.mark.parametrize("bad", ["file.txt", "noext", "https://example.com/img.png"])
    def test_rejects_non_pdf_extension(self, bad):
        with pytest.raises(InputNotPdfError):
            get_pdf_file(bad)


class TestValidateSafeUrl:
    def test_allows_public_https(self):
        # Mock DNS so the test doesn't need network access. 93.184.216.34 is
        # the historical example.com IP — doesn't matter what we use as long
        # as it's a routable public address.
        with patch(
            "pocsynth.pdf.socket.getaddrinfo",
            return_value=[(0, 0, 0, "", ("93.184.216.34", 0))],
        ):
            assert validate_safe_url("https://example.com/doc.pdf") is None

    @pytest.mark.parametrize("scheme", ["http", "ftp", "file", "gopher", "data"])
    def test_rejects_non_https_scheme(self, scheme):
        result = validate_safe_url(f"{scheme}://example.com/doc.pdf")
        assert result is not None and "https only" in result

    @pytest.mark.parametrize("host,addr", [
        ("loopback.test", "127.0.0.1"),
        ("private.test", "10.0.0.1"),
        ("private.test", "192.168.1.1"),
        ("private.test", "172.16.0.1"),
        ("linklocal.test", "169.254.169.254"),  # AWS metadata endpoint
        ("multicast.test", "224.0.0.1"),
    ])
    def test_rejects_private_and_reserved(self, host, addr):
        with patch("pocsynth.pdf.socket.getaddrinfo",
                   return_value=[(0, 0, 0, "", (addr, 0))]):
            result = validate_safe_url(f"https://{host}/doc.pdf")
        assert result is not None and "disallowed" in result

    def test_dns_failure_returns_error(self):
        import socket as _socket
        with patch("pocsynth.pdf.socket.getaddrinfo",
                   side_effect=_socket.gaierror("nope")):
            result = validate_safe_url("https://nonexistent.invalid/doc.pdf")
        assert result is not None and "DNS" in result


class TestGetPdfFileSsrf:
    def test_metadata_endpoint_blocked_at_fetch(self):
        with patch("pocsynth.pdf.socket.getaddrinfo",
                   return_value=[(0, 0, 0, "", ("169.254.169.254", 0))]):
            with pytest.raises(UrlRejectedError) as excinfo:
                get_pdf_file("https://meta.example/doc.pdf")
        assert excinfo.value.context["reason"] == "private_or_reserved_ip"

    def test_http_scheme_blocked_before_request(self):
        with patch("pocsynth.pdf.requests.get") as mock_get:
            with pytest.raises(UrlRejectedError) as excinfo:
                get_pdf_file("http://example.com/doc.pdf")
        assert excinfo.value.context["reason"] == "non_https_scheme"
        mock_get.assert_not_called()

    def test_redirect_to_private_ip_blocked(self):
        """First hop is public, response 301s to a loopback host which must be rejected."""
        redirect_resp = MagicMock()
        redirect_resp.status_code = 301
        redirect_resp.is_redirect = True
        redirect_resp.is_permanent_redirect = True
        redirect_resp.headers = {"Location": "https://internal.test/doc.pdf"}
        redirect_resp.close = MagicMock()

        def fake_getaddrinfo(host, _port):
            if host == "example.com":
                return [(0, 0, 0, "", ("93.184.216.34", 0))]
            return [(0, 0, 0, "", ("127.0.0.1", 0))]

        with patch("pocsynth.pdf.socket.getaddrinfo", side_effect=fake_getaddrinfo), \
             patch("pocsynth.pdf.requests.get", return_value=redirect_resp):
            with pytest.raises(UrlRejectedError) as excinfo:
                get_pdf_file("https://example.com/doc.pdf")
        assert excinfo.value.context["reason"] == "private_or_reserved_ip"
