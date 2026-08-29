"""Email transport abstraction.

Decouples notification/email logic from smtplib. Tests use
``MemoryEmailTransport``; production uses ``SMTPEmailTransport``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.logging import get_logger

logger = get_logger("app.email_transport")


@dataclass(frozen=True)
class EmailSendResult:
    """Result of one email send attempt."""

    success: bool
    message_id: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None


class EmailTransport(Protocol):
    """Abstract email transport interface."""

    def send(
        self,
        *,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str,
        from_address: str,
        from_name: str,
        message_id: str,
    ) -> EmailSendResult:
        """Send one email. Returns a result; never raises for delivery errors."""
        ...


class MemoryEmailTransport:
    """In-memory transport for tests. Stores all sent emails.

    Never makes real SMTP connections.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(
        self,
        *,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str,
        from_address: str,
        from_name: str,
        message_id: str,
    ) -> EmailSendResult:
        self.sent.append(
            {
                "recipient": recipient,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
                "from_address": from_address,
                "from_name": from_name,
                "message_id": message_id,
            }
        )
        return EmailSendResult(success=True, message_id=message_id)


class SMTPEmailTransport:
    """Production SMTP transport.

    Uses smtplib to send a multipart plain-text + HTML email.
    One attempt per call. Failures are returned, not raised.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls

    def send(
        self,
        *,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str,
        from_address: str,
        from_name: str,
        message_id: str,
    ) -> EmailSendResult:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{from_address}>"
        msg["To"] = recipient
        msg["Message-ID"] = message_id
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            with smtplib.SMTP(self._host, self._port, timeout=30) as server:
                if self._use_tls:
                    server.starttls()
                if self._username:
                    server.login(self._username, self._password)
                server.sendmail(from_address, [recipient], msg.as_string())
            return EmailSendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.warning(
                "smtp_send_failed",
                recipient=recipient,
                subject=subject,
                error=str(exc),
            )
            return EmailSendResult(
                success=False,
                failure_code=type(exc).__name__,
                failure_message=str(exc)[:500],
            )


def build_email_transport() -> EmailTransport:
    """Build the configured email transport from settings.

    Returns MemoryEmailTransport when email is disabled or in test env.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.email_enabled or settings.is_test:
        return MemoryEmailTransport()
    return SMTPEmailTransport(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password.get_secret_value(),
        use_tls=settings.smtp_use_tls,
    )
