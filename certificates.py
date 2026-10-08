"""Generates internship completion certificates as PDF files."""
import secrets
from datetime import datetime

from flask import current_app
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

import settings

NAVY = colors.HexColor("#1e3a5f")
GOLD = colors.HexColor("#b8860b")
GREY = colors.HexColor("#555555")


def new_certificate_id():
    return f"CERT-{datetime.now():%Y}-{secrets.token_hex(4).upper()}"


def certificate_path(user):
    return current_app.config["CERT_DIR"] / f"{user.certificate_id}.pdf"


def generate_pdf(user):
    """Writes the certificate PDF for a user who has a certificate_id. Returns the file path."""
    path = certificate_path(user)
    path.parent.mkdir(exist_ok=True)
    width, height = landscape(A4)
    c = canvas.Canvas(str(path), pagesize=landscape(A4))
    c.setTitle(f"Certificate - {user.name}")

    # Borders
    c.setStrokeColor(NAVY)
    c.setLineWidth(6)
    c.rect(24, 24, width - 48, height - 48)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.5)
    c.rect(38, 38, width - 76, height - 76)

    cx = width / 2
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(cx, height - 95, settings.get("organization_name").upper())

    c.setFont("Times-Bold", 42)
    c.drawCentredString(cx, height - 160, "Certificate of Completion")

    c.setFillColor(GREY)
    c.setFont("Helvetica", 14)
    c.drawCentredString(cx, height - 205, "This is to certify that")

    c.setFillColor(NAVY)
    c.setFont("Times-BoldItalic", 36)
    c.drawCentredString(cx, height - 255, user.name)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(cx - 200, height - 268, cx + 200, height - 268)

    c.setFillColor(GREY)
    c.setFont("Helvetica", 14)
    c.drawCentredString(cx, height - 300, "has successfully completed the")
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(cx, height - 330, settings.get("program_name"))
    c.setFillColor(GREY)
    c.setFont("Helvetica", 13)
    c.drawCentredString(
        cx, height - 358,
        f"from {user.enrolled_at:%d %B %Y} to {user.internship_end:%d %B %Y}, "
        "completing all assigned modules and reviews.")

    # Footer: date, signature line, certificate ID
    issued = user.certificate_issued_at or datetime.now()
    c.setFont("Helvetica", 11)
    c.drawString(90, 110, f"Date of issue: {issued:%d %B %Y}")
    c.line(width - 290, 120, width - 90, 120)
    c.drawCentredString(width - 190, 104, "Program Director")
    c.setFont("Helvetica", 9)
    c.drawCentredString(cx, 66, f"Certificate ID: {user.certificate_id}   |   Verify at "
                                f"{settings.get('base_url')}/verify/{user.certificate_id}")
    c.showPage()
    c.save()
    return path
