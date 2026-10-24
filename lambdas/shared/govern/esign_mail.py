"""Email for Govern e-signature: invitations, reminders, the completed copy,
and notices when an envelope is declined, voided or expires.

Sent through SES v2 from ``NOTIFY_FROM_EMAIL``. When that is not set (or SES
refuses), nothing is sent and the caller gets False: the reviewer can still
copy each signer's link from Govern. Every message is plain text plus a small
HTML version in Blue-IQ's own style; the completed copy attaches the sealed
PDF when it fits under SES's size limit and otherwise links to it.
"""
from __future__ import annotations

import html
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

from .. import aws
from ..config import settings
from ..logger import get_logger

log = get_logger("blue-iq.govern.esign_mail")

BRAND = "Blue-IQ Govern"
# SES caps a raw message at 10 MB, and base64 grows the PDF by a third.
MAX_ATTACHMENT_BYTES = 7 * 1024 * 1024


def _sender(organization: str | None) -> str | None:
    if not settings.notify_from_email:
        return None
    label = f"{organization} via {BRAND}" if organization else BRAND
    return formataddr((label[:120], settings.notify_from_email))


def _html(heading: str, paragraphs: list[str], button: tuple[str, str] | None, footnote: str | None) -> str:
    body = "".join(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.55;color:#334155">{html.escape(p)}</p>'
                   for p in paragraphs if p)
    cta = ""
    if button:
        label, url = button
        cta = (f'<p style="margin:22px 0"><a href="{html.escape(url, quote=True)}" '
               'style="display:inline-block;background:#1F5BE6;color:#ffffff;text-decoration:none;'
               f'font-weight:600;font-size:15px;padding:12px 22px;border-radius:8px">{html.escape(label)}</a></p>')
    foot = (f'<p style="margin:22px 0 0;font-size:12px;line-height:1.5;color:#64748B">{html.escape(footnote)}</p>'
            if footnote else "")
    return (
        '<!doctype html><html><body style="margin:0;background:#F1F5F9;padding:24px 12px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">'
        '<table role="presentation" width="560" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;background:#ffffff;border:1px solid #E2E8F0;border-radius:12px">'
        '<tr><td style="padding:22px 28px;border-bottom:1px solid #E2E8F0;font:600 15px Arial,sans-serif;color:#0B1A3A">'
        f'<span style="display:inline-block;width:12px;height:12px;background:#1F5BE6;border-radius:3px;'
        f'margin-right:8px;vertical-align:-1px"></span>{BRAND}</td></tr>'
        f'<tr><td style="padding:26px 28px;font-family:Arial,sans-serif">'
        f'<h1 style="margin:0 0 16px;font-size:20px;line-height:1.3;color:#0B1220">{html.escape(heading)}</h1>'
        f'{body}{cta}{foot}</td></tr></table></td></tr></table></body></html>'
    )


def _text(heading: str, paragraphs: list[str], button: tuple[str, str] | None, footnote: str | None) -> str:
    parts = [heading, ""] + [p for p in paragraphs if p]
    if button:
        parts += ["", f"{button[0]}: {button[1]}"]
    if footnote:
        parts += ["", footnote]
    parts += ["", f"— {BRAND}"]
    return "\n".join(parts)


def _send(to: list[str], subject: str, heading: str, paragraphs: list[str], *, organization: str | None,
          button: tuple[str, str] | None = None, footnote: str | None = None,
          attachment: tuple[str, bytes] | None = None) -> bool:
    sender = _sender(organization)
    recipients = [t for t in dict.fromkeys(to) if t][:50]
    if not sender or not recipients:
        log.info("esign_mail.skipped", reason="no_sender" if not sender else "no_recipients")
        return False
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject[:200]
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(_text(heading, paragraphs, button, footnote), "plain", "utf-8"))
    alt.attach(MIMEText(_html(heading, paragraphs, button, footnote), "html", "utf-8"))
    msg.attach(alt)
    if attachment:
        name, data = attachment
        part = MIMEApplication(data, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=name)
        msg.attach(part)
    try:
        aws.ses_client().send_email(FromEmailAddress=settings.notify_from_email,
                                Destination={"ToAddresses": recipients},
                                Content={"Raw": {"Data": msg.as_bytes()}})
        return True
    except Exception as exc:  # noqa: BLE001 — email is best effort; the link stays copyable in Govern
        log.warning("esign_mail.failed", error_type=type(exc).__name__, recipients=len(recipients))
        return False


_EXPIRY_NOTE = "This link is for you only. Don't forward it: anyone with it can sign as you."


def invitation(*, to: str, signer_name: str, sender_name: str, organization: str | None, document: str,
               message: str | None, url: str, expires_on: str, reminder: bool = False) -> bool:
    heading = (f"Reminder: {document} is waiting for your signature" if reminder
               else f"{sender_name} sent you {document} to sign")
    paragraphs = [f"Hello {signer_name},",
                  (f"{sender_name}" + (f" at {organization}" if organization else "")
                   + f" asked you to review and sign {document}."),
                  message or "",
                  f"The link works until {expires_on}."]
    subject = f"{'Reminder: ' if reminder else ''}Please sign: {document}"
    return _send([to], subject, heading, paragraphs, organization=organization,
                 button=("Review and sign", url), footnote=_EXPIRY_NOTE)


def completed(*, to: list[str], organization: str | None, document: str, pdf: bytes, filename: str,
              download_url: str | None) -> bool:
    attach = (filename, pdf) if len(pdf) <= MAX_ATTACHMENT_BYTES else None
    paragraphs = ["Everyone has signed. The signed document, sealed with a digital signature and followed by its "
                  "certificate of completion, is " + ("attached." if attach else "ready to download.")]
    button = None if attach or not download_url else ("Download the signed document", download_url)
    return _send(to, f"Completed: {document}", f"{document} is signed", paragraphs, organization=organization,
                 button=button, attachment=attach,
                 footnote="Keep this copy for your records. Any change to the file breaks its digital seal.")


def declined(*, to: list[str], organization: str | None, document: str, signer: str, reason: str,
             contract_url: str) -> bool:
    return _send(to, f"Declined: {document}", f"{signer} declined to sign {document}",
                 [f"Reason given: {reason}", "The envelope is closed. Prepare a new one when you are ready."],
                 organization=organization, button=("Open in Govern", contract_url))


def voided(*, to: list[str], organization: str | None, document: str, reason: str) -> bool:
    return _send(to, f"Cancelled: {document}", f"{document} no longer needs your signature",
                 ["The sender cancelled this signing request, so its link no longer works.",
                  f"Reason given: {reason}" if reason else ""], organization=organization)


def expired(*, to: list[str], organization: str | None, document: str, contract_url: str) -> bool:
    return _send(to, f"Expired: {document}", f"{document} expired before everyone signed",
                 ["The signing links no longer work. Prepare a new envelope to send it again."],
                 organization=organization, button=("Open in Govern", contract_url))
