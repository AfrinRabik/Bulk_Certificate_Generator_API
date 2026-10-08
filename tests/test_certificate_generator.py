"""Unit tests for the PDF renderer (no API / database involved)."""
from datetime import date

import pytest
from pypdf import PdfReader

from app.services.certificate import (
    CertificateData,
    CertificateGenerationError,
    generate_certificate_pdf,
)


def make_data(**overrides):
    values = dict(
        recipient_name="Asha Raman",
        event_name="Python Bootcamp 2026",
        issued_by="Acme Academy",
        issue_date=date(2026, 10, 8),
        certificate_number="CERT-ABCD1234-00001",
        achievement=None,
    )
    values.update(overrides)
    return CertificateData(**values)


def test_generates_pdf_containing_recipient_specific_data(tmp_path):
    out = tmp_path / "cert.pdf"
    generate_certificate_pdf(make_data(achievement="With distinction"), out)

    assert out.read_bytes().startswith(b"%PDF")
    reader = PdfReader(out)
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert "Asha Raman" in text
    assert "Python Bootcamp 2026" in text
    assert "Acme Academy" in text
    assert "8 October 2026" in text
    assert "CERT-ABCD1234-00001" in text
    assert "With distinction" in text


def test_long_name_is_shrunk_to_fit(tmp_path):
    out = tmp_path / "long.pdf"
    generate_certificate_pdf(make_data(recipient_name="Maximilian " * 8), out)
    assert out.exists()


def test_unsupported_characters_raise_and_leave_no_file(tmp_path):
    out = tmp_path / "bad.pdf"
    with pytest.raises(CertificateGenerationError, match="cannot render"):
        generate_certificate_pdf(make_data(recipient_name="தமிழ்"), out)
    assert list(tmp_path.iterdir()) == []


def test_text_that_cannot_fit_raises_and_leaves_no_file(tmp_path):
    out = tmp_path / "huge.pdf"
    with pytest.raises(CertificateGenerationError, match="too long"):
        generate_certificate_pdf(make_data(recipient_name="W" * 100), out)
    assert list(tmp_path.iterdir()) == []
