"""PDF text extraction.

These exist because of a bug that reached live use. The extractor imported a
`PdfError` that pypdf does not export, inside the `try` whose `except` clause
referenced it. The failed import left the name unbound, so the first real PDF
produced `UnboundLocalError: cannot access local variable 'PdfError'` while
Python was evaluating the exception handler -- a crash in the error path, which
is the worst place for one.

It survived because every PDF path ran only against live Drive. Extraction is
now a pure function over bytes, so it can be tested without a provider, a
network or credentials.
"""

from __future__ import annotations

import pytest

from mcp_cloudtube.errors import ErrorCode, ToolError
from mcp_cloudtube.storage.drive import _pdf_to_text


def _make_pdf(lines: list[str]) -> bytes:
    """A minimal but genuinely valid single-page PDF with a text layer."""
    text = "BT /F1 14 Tf 60 760 Td 18 TL\n"
    for line in lines:
        esc = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        text += f"({esc}) Tj T*\n"
    text += "ET"
    stream = text.encode("latin-1")

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


class TestValidPdfs:
    def test_text_is_extracted(self) -> None:
        text, pages = _pdf_to_text(
            _make_pdf(["Hello from a real PDF", "second line"]),
            summary="test.pdf",
            file_id="x",
        )
        assert pages == 1
        assert "Hello from a real PDF" in text

    def test_page_count_is_reported(self) -> None:
        _text, pages = _pdf_to_text(_make_pdf(["one"]), summary="t.pdf", file_id="x")
        assert pages == 1

    def test_a_pdf_with_no_text_layer_returns_empty_not_an_error(self) -> None:
        """A scan is readable as a file but has nothing to extract.

        It must not raise: the caller turns the empty result into an explicit
        'this needs OCR' notice, which is more useful than a failure.
        """
        text, pages = _pdf_to_text(_make_pdf([]), summary="scan.pdf", file_id="x")
        assert pages == 1
        assert text.strip() == ""


class TestMalformedInput:
    """The regression. Every one of these previously raised UnboundLocalError."""

    @pytest.mark.parametrize(
        ("label", "data"),
        [
            ("truncated header", b"%PDF-1.4 and then nothing useful"),
            ("empty file", b""),
            ("not a pdf at all", b"hello world, plainly text"),
            ("html error page", b"<html><body>503 Service Unavailable</body></html>"),
            ("truncated mid-object", _make_pdf(["abc"])[:120]),
        ],
    )
    def test_bad_bytes_produce_a_tool_error(self, label: str, data: bytes) -> None:
        with pytest.raises(ToolError) as caught:
            _pdf_to_text(data, summary=label, file_id="file123")

        assert caught.value.code == ErrorCode.UNSUPPORTED_MEDIA_TYPE
        assert caught.value.retryable is False
        assert "metadata" in caught.value.remediation

    def test_the_error_names_the_file(self) -> None:
        with pytest.raises(ToolError) as caught:
            _pdf_to_text(b"nonsense", summary="Report.pdf", file_id="file123")
        assert "Report.pdf" in caught.value.message
        assert caught.value.details["file_id"] == "file123"

    def test_no_bare_exception_escapes(self) -> None:
        """The property that actually broke: only ToolError may come out."""
        for data in (b"", b"x", b"%PDF", b"\x00\x01\x02", _make_pdf(["a"])[:40]):
            try:
                _pdf_to_text(data, summary="s", file_id="x")
            except ToolError:
                pass
            except Exception as exc:
                pytest.fail(f"non-ToolError escaped for {data!r}: {type(exc).__name__}: {exc}")


class TestImports:
    def test_pypdf_exception_names_are_real(self) -> None:
        """Guards the exact mistake: importing a class pypdf does not export.

        A wrong name here fails only when an exception is being handled, which
        is both the rarest path and the one that must never fail.
        """
        from pypdf.errors import DependencyError, PyPdfError

        assert issubclass(PyPdfError, Exception)
        assert issubclass(DependencyError, Exception)

        import pypdf.errors as errors

        assert not hasattr(errors, "PdfError"), (
            "pypdf now exports PdfError; the extractor's except clause should be revisited"
        )
