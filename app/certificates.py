from pathlib import Path
from tempfile import NamedTemporaryFile

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas


def generate_certificate(
    target: Path, *, name: str, email: str, event_name: str, issuer: str, issue_date: str, certificate_id: str
) -> None:
    """Publish a complete PDF atomically; never expose a partly written file."""
    width, height = landscape(A4)
    with NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        canvas = Canvas(str(temporary_path), pagesize=(width, height))
        canvas.setTitle(f"Certificate of completion - {name}")
        canvas.setAuthor(issuer)
        canvas.setFillColor(HexColor("#fcfbf8"))
        canvas.rect(0, 0, width, height, fill=1, stroke=0)
        canvas.setStrokeColor(HexColor("#b8a47c"))
        canvas.setLineWidth(1)
        canvas.line(64, height - 64, width - 64, height - 64)
        canvas.line(64, 64, width - 64, 64)

        def centered(text: str, y: float, size: float, font: str = "Helvetica") -> None:
            # Width-based sizing also handles a long name with no spaces.
            measured = stringWidth(text, font, size)
            fitted = min(size, size * (width - 128) / max(measured, 1))
            canvas.setFont(font, fitted)
            canvas.drawCentredString(width / 2, y, text)

        canvas.setFillColor(HexColor("#766649"))
        centered("CERTIFICATE OF COMPLETION", 490, 15)
        canvas.setFillColor(HexColor("#242824"))
        centered("This certificate is presented to", 423, 12)
        centered(name, 365, 34, "Times-Bold")
        centered(email, 336, 11)
        centered("for completing", 288, 12)
        centered(event_name, 250, 23, "Times-Roman")
        centered(f"Issued by {issuer}", 181, 12)
        centered(f"Date of issue: {issue_date}", 153, 11)
        centered(f"Certificate ID: {certificate_id}", 93, 9)
        canvas.showPage()
        canvas.save()
        temporary_path.replace(target)
    finally:
        temporary_path.unlink(missing_ok=True)
