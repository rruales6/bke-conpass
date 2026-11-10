"""Transactional email (D18): one generic SMTP sender, stdlib only.

Separate from D10's NotificationProvider, which handles bulk, template-based customer
messaging. This module sends one message and reports success or failure right away,
which credential emails need. Any SMTP relay works (Hostinger today; SES-SMTP or Brevo
later), so switching providers only means changing settings.

Never log a message body: credential emails carry a temporary password.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from abc import ABC, abstractmethod
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

log = logging.getLogger("conpass.email")

# Socket timeout per SMTP step. Kept well below the 15 s Lambda timeout so an
# unreachable relay fails fast and the onboarding request still answers.
SMTP_TIMEOUT_SECONDS = 6


class EmailDeliveryError(Exception):
    """The message was not accepted by the relay (or email is not configured)."""


class EmailSender(ABC):
    @property
    @abstractmethod
    def configured(self) -> bool:
        """True when this sender can actually deliver (credentials present)."""

    @abstractmethod
    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        """Deliver one message. Raises EmailDeliveryError on any failure."""


class StubEmailSender(EmailSender):
    """Local/test default: logs recipient + subject only, never the body."""

    @property
    def configured(self) -> bool:
        return True

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        log.info("email_stub_send", extra={"to": to, "subject": subject})


class SmtpEmailSender(EmailSender):
    def __init__(self, *, host: str | None, port: int, username: str | None,
                 password: str | None, from_address: str | None, from_name: str,
                 reply_to: str | None = None, timeout: float = SMTP_TIMEOUT_SECONDS):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.from_address = from_address or username
        self.from_name = from_name
        self.reply_to = reply_to
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password and self.from_address)

    def build_message(self, to: str, subject: str, text: str,
                      html: str | None = None) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = formataddr((self.from_name, self.from_address))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(usegmt=True)
        domain = (self.from_address or "conpass.cards").rpartition("@")[2] or "conpass.cards"
        msg["Message-ID"] = make_msgid(domain=domain)
        msg["Auto-Submitted"] = "auto-generated"
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        return msg

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        if not self.configured:
            raise EmailDeliveryError("smtp is not configured")
        msg = self.build_message(to, subject, text, html)
        context = ssl.create_default_context()
        try:
            if self.port == 465:  # implicit TLS
                with smtplib.SMTP_SSL(self.host, self.port, context=context,
                                      timeout=self.timeout) as smtp:
                    smtp.login(self.username, self.password)
                    smtp.send_message(msg)
            else:  # 587 / 25 → STARTTLS
                with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                    smtp.starttls(context=context)
                    smtp.login(self.username, self.password)
                    smtp.send_message(msg)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            # The exception text may echo server replies, never our password/body.
            log.error("email_send_failed", extra={"to": to, "error": type(exc).__name__})
            raise EmailDeliveryError(type(exc).__name__) from exc
        log.info("email_sent", extra={"to": to, "subject": subject})
