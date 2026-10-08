"""Sends emails over SMTP (settings from Admin -> Settings). With no SMTP server set,
emails are saved as .eml files in the outbox folder so the app still works for testing."""
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from html import escape

from flask import current_app

import settings
from models import EmailLog, db


def text_to_html(text):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    body = "".join(f"<p style='margin:0 0 14px'>{escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)
    return (
        "<div style=\"font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.55;"
        f"color:#1f2933;max-width:640px\">{body}</div>"
    )


def _build(to, subject, text, attachments):
    msg = EmailMessage()
    sender = settings.get("smtp_from") or settings.get("smtp_user") or "noreply@example.com"
    msg["From"] = f"{settings.get('program_name')} <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(text_to_html(text), subtype="html")
    for filename, data, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def _deliver(msg):
    host = settings.get("smtp_host")
    port = settings.get_int("smtp_port")
    user, password = settings.get("smtp_user"), settings.get("smtp_password")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            if user:
                s.login(user, password)
            s.send_message(msg)


def send_email(to, subject, text, attachments=None, raise_errors=False):
    """Returns True if the email was sent (or saved to the outbox)."""
    msg = _build(to, subject, text, attachments)
    log = EmailLog(to_email=to, subject=subject)
    try:
        if settings.get("smtp_host"):
            _deliver(msg)
            log.status = "sent"
        else:
            outbox = current_app.config["OUTBOX_DIR"]
            outbox.mkdir(exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{to}_{subject}")[:80]
            (outbox / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}.eml").write_bytes(bytes(msg))
            log.status = "saved"
        ok = True
    except Exception as e:  # SMTP errors come in many types; log them all
        log.status, log.error, ok = "failed", str(e), False
        current_app.logger.error("Email to %s failed: %s", to, e)
        if raise_errors:
            db.session.add(log)
            db.session.commit()
            raise
    db.session.add(log)
    db.session.commit()
    return ok
