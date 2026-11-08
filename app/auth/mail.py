"""Email delivery for password reset (Task 15.5).

* ``EmailSender`` is the whole provider interface: one method, one plain message. ``SmtpEmailSender`` implements it with the
  standard library and works with any SMTP server, including the SMTP interfaces of transactional providers.
* Transport security is not optional. ``starttls`` and ``ssl`` verify the server certificate and host name (against the system
  roots, or the CA bundle in ``SMTP_CA_FILE``); STARTTLS that the server does not offer is a failure, never a silent downgrade
  to plain text; credentials are never sent over an unencrypted connection.
* Every failure becomes ``EmailDeliveryError`` with a fixed message and a category. The text of a provider's reply, the host
  name, the credentials and the recipient never appear in an exception, a log line or a metric label.
* ``deliver_password_reset`` is what the request handler schedules after it has answered. It never raises, so a mail outage
  cannot change the response (which must be identical for known and unknown accounts) or take a request down.
* Nothing here logs a token or a link.
"""

import contextlib
import hashlib
import logging
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Protocol

from app.config import Settings
from app.core import metrics
from app.core.logging import log_event

logger = logging.getLogger("atlas.mail")

GENERIC_FAILURE = "The email could not be delivered"


class EmailDeliveryError(Exception):
    """Delivery failed. ``kind`` says roughly why; ``smtp_code`` is the numeric reply code when there was one.

    The message is always the same text: the provider's own wording can contain addresses, host names and policy details.
    """

    def __init__(self, kind: str, smtp_code: int | None = None) -> None:
        super().__init__(GENERIC_FAILURE)
        self.kind = kind
        self.smtp_code = smtp_code


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    text: str


class EmailSender(Protocol):
    def send(self, message: OutgoingEmail) -> None:
        """Deliver the message or raise ``EmailDeliveryError``. Raises nothing else."""
        ...


class DisabledEmailSender:
    """Used when no SMTP server is configured: every attempt fails the same way, with nothing delivered or logged."""

    def send(self, message: OutgoingEmail) -> None:
        raise EmailDeliveryError("not_configured")


class SmtpEmailSender:
    def __init__(self, settings: Settings) -> None:
        if not settings.smtp_host or not settings.smtp_from:
            raise ValueError("SMTP_HOST and SMTP_FROM are required")
        if settings.smtp_username and settings.smtp_security == "none":
            raise ValueError("credentials are never sent over an unencrypted SMTP connection; use starttls or ssl")
        self._s = settings

    def _context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=str(self._s.smtp_ca_file) if self._s.smtp_ca_file else None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context  # create_default_context() verifies the certificate chain AND the host name

    def build(self, message: OutgoingEmail) -> EmailMessage:
        s = self._s
        mail = EmailMessage()  # refuses a header value that contains a line break, so a recipient cannot inject headers
        mail["From"] = formataddr((s.smtp_from_name, s.smtp_from))
        mail["To"] = message.to
        mail["Subject"] = message.subject
        mail["Date"] = formatdate(localtime=False)
        mail["Message-ID"] = make_msgid(domain=s.smtp_from.rsplit("@", 1)[1])
        mail["Auto-Submitted"] = "auto-generated"  # an automatic notification: do not auto-reply
        mail["X-Auto-Response-Suppress"] = "All"
        mail.set_content(message.text, charset="utf-8")
        return mail

    def send(self, message: OutgoingEmail) -> None:
        s = self._s
        try:
            mail = self.build(message)
            if s.smtp_security == "ssl":
                client: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds,
                                                        context=self._context())
            else:
                client = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds)
            try:
                client.ehlo()
                if s.smtp_security == "starttls":
                    if not client.has_extn("starttls"):
                        raise EmailDeliveryError("tls")  # never continue in plain text when encryption was required
                    client.starttls(context=self._context())
                    client.ehlo()
                if s.smtp_username and s.smtp_password is not None:
                    client.login(s.smtp_username, s.smtp_password.get_secret_value())
                client.send_message(mail, from_addr=s.smtp_from, to_addrs=[message.to])
            finally:
                _close_quietly(client)  # cleanup must never replace the real error with its own
        except EmailDeliveryError:
            raise
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailDeliveryError("auth", exc.smtp_code) from None
        except smtplib.SMTPRecipientsRefused as exc:
            # The code sits in {recipient: (code, text)}; the text (a provider's wording) is deliberately dropped.
            codes = [reply[0] for reply in exc.recipients.values()]
            raise EmailDeliveryError("rejected", codes[0] if codes else None) from None
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPHeloError) as exc:
            raise EmailDeliveryError("rejected", getattr(exc, "smtp_code", None)) from None
        except ssl.SSLError:
            raise EmailDeliveryError("tls") from None
        except smtplib.SMTPException as exc:
            # smtplib reports a read that timed out as "server disconnected"; the timeout is the chained cause.
            if _caused_by_timeout(exc):
                raise EmailDeliveryError("timeout") from None
            if isinstance(exc, smtplib.SMTPServerDisconnected | smtplib.SMTPConnectError):
                raise EmailDeliveryError("connect") from None
            raise EmailDeliveryError("protocol") from None
        except TimeoutError:
            raise EmailDeliveryError("timeout") from None
        except (ConnectionError, OSError):
            raise EmailDeliveryError("connect") from None
        except (ValueError, UnicodeError):
            raise EmailDeliveryError("message") from None


def _close_quietly(client: smtplib.SMTP) -> None:
    with contextlib.suppress(Exception):  # the server may already be gone or stalled; only the socket matters now
        client.quit()
    client.close()


def _caused_by_timeout(exc: BaseException) -> bool:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def email_sender_for(settings: Settings) -> EmailSender:
    return SmtpEmailSender(settings) if settings.smtp_host and settings.smtp_from else DisabledEmailSender()


# --- the password-reset message ------------------------------------------------------------------------------------------


def reset_link(settings: Settings, token: str) -> str:
    # The token is in the URL FRAGMENT: browsers never send a fragment to a server or in a Referer header.
    base = (settings.public_base_url or "http://localhost:5173").rstrip("/")
    return f"{base}/reset-password#token={token}"


def build_reset_email(settings: Settings, to: str, token: str) -> OutgoingEmail:
    minutes = settings.auth_password_reset_minutes
    return OutgoingEmail(
        to=to,
        subject="Reset your Atlas AI password",
        text=(
            "Someone asked to reset the password of the Atlas AI account for this address.\n\n"
            f"To choose a new password, open this link within {minutes} minutes. It works once:\n\n"
            f"{reset_link(settings, token)}\n\n"
            "Choosing a new password signs you out everywhere.\n\n"
            "If you did not ask for this, ignore this message: your password has not been changed and nobody can "
            "use the link without this email.\n"
        ),
    )


def recipient_fingerprint(address: str) -> str:
    """A short, one-way label for correlating log lines about one recipient without storing the address."""
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()[:12]


def deliver_password_reset(sender: EmailSender, settings: Settings, to: str, token: str) -> None:
    """Build and send the reset email. Runs after the response; never raises; logs the outcome without any message content."""
    who = recipient_fingerprint(to)
    try:
        sender.send(build_reset_email(settings, to, token))
    except EmailDeliveryError as exc:
        metrics.EMAILS.labels("password_reset", exc.kind).inc()
        log_event(logger, "password_reset_email_failed", level=logging.WARNING, kind=exc.kind, smtp_code=exc.smtp_code,
                  recipient=who)
    except Exception as exc:  # noqa: BLE001 - a background task must not raise; the type is enough to diagnose
        metrics.EMAILS.labels("password_reset", "unexpected").inc()
        log_event(logger, "password_reset_email_failed", level=logging.ERROR, kind="unexpected",
                  error_type=type(exc).__name__, recipient=who)
    else:
        metrics.EMAILS.labels("password_reset", "sent").inc()
        log_event(logger, "password_reset_email_sent", recipient=who)
