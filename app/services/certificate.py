"""PDF certificate rendering (single predefined template, drawn with reportlab).

`generate_certificate_pdf` is deliberately a plain function with the signature
    (CertificateData, output_path) -> None
so it can be swapped for a fake in tests, or for another renderer later.
"""
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)
CONTENT_WIDTH = PAGE_WIDTH - 140

NAVY = colors.HexColor("#1f3a5f")
GOLD = colors.HexColor("#b8902f")
GREY = colors.HexColor("#555555")

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_ITALIC = "Helvetica-Oblique"


class CertificateGenerationError(Exception):
    """Raised when a single certificate cannot be rendered."""


@dataclass(frozen=True)
class CertificateData:
    recipient_name: str
    event_name: str
    issued_by: str
    issue_date: date
    certificate_number: str
    achievement: str | None = None


def can_render(text: str) -> bool:
    """The built-in PDF fonts support Latin text (Windows-1252 charset)."""
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def _check_renderable(label: str, text: str | None) -> None:
    if text and not can_render(text):
        raise CertificateGenerationError(
            f"{label} contains characters the certificate font cannot render"
        )


def _fit_single_line(text: str, font: str, max_size: int, min_size: int, max_width: float) -> int:
    """Largest font size (<= max_size) at which `text` fits on one line."""
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1
    if stringWidth(text, font, size) > max_width:
        raise CertificateGenerationError("Text is too long to fit on the certificate")
    return size


def _draw_wrapped(c, text, font, size, y, max_lines, line_height) -> float:
    """Draw centred, wrapped text starting at y. Returns the y below the block."""
    lines = simpleSplit(text, font, size, CONTENT_WIDTH)
    if len(lines) > max_lines:
        raise CertificateGenerationError("Text is too long to fit on the certificate")
    c.setFont(font, size)
    for line in lines:
        c.drawCentredString(PAGE_WIDTH / 2, y, line)
        y -= line_height
    return y


def _format_date(d: date) -> str:
    return f"{d.day} {d:%B %Y}"


def generate_certificate_pdf(data: CertificateData, output_path: Path) -> None:
    """Render one certificate to `output_path`.

    Everything that can fail (unsupported characters, text too long) is checked
    BEFORE drawing, and the file is written to a temp name then renamed, so a
    failed certificate never leaves a half-written PDF behind.
    """
    for label, value in (
        ("Recipient name", data.recipient_name),
        ("Event name", data.event_name),
        ("Issuer", data.issued_by),
        ("Achievement", data.achievement),
    ):
        _check_renderable(label, value)

    name_size = _fit_single_line(data.recipient_name, FONT_BOLD, 40, 12, CONTENT_WIDTH)
    issuer_text = f"Issued by {data.issued_by}"
    issuer_size = _fit_single_line(issuer_text, FONT, 16, 8, CONTENT_WIDTH)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    cx = PAGE_WIDTH / 2

    try:
        c = canvas.Canvas(str(tmp_path), pagesize=landscape(A4))
        c.setTitle(f"Certificate - {data.recipient_name}")
        c.setAuthor(data.issued_by)
        c.setSubject(data.event_name)

        # Double border
        c.setStrokeColor(NAVY)
        c.setLineWidth(4)
        c.rect(20, 20, PAGE_WIDTH - 40, PAGE_HEIGHT - 40)
        c.setStrokeColor(GOLD)
        c.setLineWidth(1.5)
        c.rect(30, 30, PAGE_WIDTH - 60, PAGE_HEIGHT - 60)

        # Heading
        c.setFillColor(NAVY)
        c.setFont(FONT_BOLD, 44)
        c.drawCentredString(cx, 490, "CERTIFICATE")
        c.setFillColor(GOLD)
        c.setFont(FONT, 16)
        c.drawCentredString(cx, 462, "OF COMPLETION")

        c.setFillColor(GREY)
        c.setFont(FONT_ITALIC, 14)
        c.drawCentredString(cx, 405, "This certificate is proudly presented to")

        # Recipient name + underline
        c.setFillColor(NAVY)
        c.setFont(FONT_BOLD, name_size)
        c.drawCentredString(cx, 350, data.recipient_name)
        c.setStrokeColor(GOLD)
        c.setLineWidth(1)
        c.line(170, 338, PAGE_WIDTH - 170, 338)

        c.setFillColor(GREY)
        c.setFont(FONT_ITALIC, 14)
        c.drawCentredString(cx, 306, "for successfully completing")

        # Event name (up to 3 lines), then optional achievement (up to 2 lines)
        c.setFillColor(NAVY)
        y = _draw_wrapped(c, data.event_name, FONT_BOLD, 22, 274, 3, 26)
        if data.achievement:
            c.setFillColor(GREY)
            y = _draw_wrapped(c, data.achievement, FONT_ITALIC, 13, y - 8, 2, 18)

        # Footer
        c.setFillColor(NAVY)
        c.setFont(FONT, issuer_size)
        c.drawCentredString(cx, 120, issuer_text)
        c.setFillColor(GREY)
        c.setFont(FONT, 12)
        c.drawString(70, 80, f"Date: {_format_date(data.issue_date)}")
        c.setFont(FONT, 10)
        c.drawRightString(PAGE_WIDTH - 70, 80, f"Certificate No: {data.certificate_number}")

        c.showPage()
        c.save()
        os.replace(tmp_path, output_path)
    finally:
        tmp_path.unlink(missing_ok=True)
