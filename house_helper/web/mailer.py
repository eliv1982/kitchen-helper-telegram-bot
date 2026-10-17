"""Sending the sign-in email: what the message says, and how it leaves.

The application talks to an `EmailSender` (one method, `send`) and knows nothing of
SMTP; tests give it a recording stand-in, so no test sends mail or opens a port. The
one real implementation is `SmtpEmailSender`, on the standard library's smtplib (run
on a worker thread, because smtplib blocks) and nothing else: no email platform SDK.

Transport. The connection to the mail server is always TLS, with the server's
certificate and host name verified against the system's trust store
(ssl.create_default_context; TLS 1.2 or newer): either implicit TLS from the first
byte ("ssl", port 465) or STARTTLS ("starttls", port 587). STARTTLS is required, not
attempted: a server that does not offer it, or a handshake that fails, is a failed
delivery, never a fall-back to plaintext. The login (if configured) happens only after
TLS is up. There is no setting for an unencrypted connection.

Secrets. The message holds the sign-in link, which is the credential, so a message is
never logged, never put in an exception, and `OutgoingEmail` keeps it out of its repr.
Neither does the SMTP login: a failure is reported as `EmailDeliveryError`, whose only
content is the class name of what went wrong (smtplib's own messages quote the server's
reply, which can quote an address), and the original exception is not chained.

Addresses. The recipient is the allowlisted address exactly as stored, and it must
pass identifiers.is_mailable_address (no quoting, no comma, no angle bracket: nothing
that could add a recipient or a header); anything else is a failed delivery. The
sender comes from configuration, validated the same way when it is loaded.
"""

import logging
import math
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Optional, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import anyio.to_thread

from house_helper.persistence.identifiers import is_mailable_address
from house_helper.web.settings import EmailSettings

logger = logging.getLogger(__name__)

PRODUCT_NAME = "Kitchen Helper"
CONFIRM_PATH = "/auth/email/confirm"


class EmailDeliveryError(Exception):
    """The message was not handed to the mail server. `reason` is the class name of
    the cause, which is all that may be logged (see the module docstring)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class OutgoingEmail:
    to: str = field(repr=False)
    subject: str
    body: str = field(repr=False)

    def __post_init__(self) -> None:
        if not is_mailable_address(self.to):
            raise ValueError("this address is not one that mail is sent to")


class EmailSender(Protocol):
    async def send(self, email: OutgoingEmail) -> None:
        """Deliver `email` or raise EmailDeliveryError."""


# --- the message --------------------------------------------------------------------


def login_link(origin: str, challenge_id: UUID, token: str) -> str:
    """The link in the email. The id is in the query (not a secret: it only lets the
    confirmation page say which address the link is for); the secret is in the
    fragment, which a browser keeps to itself: it is not sent to the server by the
    page load, so it reaches no access log, proxy, redirect or Referer header."""
    return f"{origin}{CONFIRM_PATH}?c={challenge_id}#{token}"


def minutes_in_russian(minutes: int) -> str:
    last_two, last = minutes % 100, minutes % 10
    if last == 1 and last_two != 11:
        word = "минута"
    elif 2 <= last <= 4 and not 12 <= last_two <= 14:
        word = "минуты"
    else:
        word = "минут"
    return f"{minutes} {word}"


def compose_login_email(address: str, link: str, lifetime: timedelta) -> OutgoingEmail:
    """The sign-in email, in Russian. ValueError if `address` is not mailable."""
    minutes = max(1, math.ceil(lifetime.total_seconds() / 60))
    body = (
        "Здравствуйте!\n"
        "\n"
        f"Для этого адреса запрошен вход в {PRODUCT_NAME}. Чтобы войти, откройте ссылку и "
        "нажмите кнопку «Войти»:\n"
        "\n"
        f"{link}\n"
        "\n"
        f"Ссылка действует {minutes_in_russian(minutes)} и срабатывает один раз. Открыть её "
        "можно на любом устройстве, не обязательно на том, где вы запрашивали вход.\n"
        "\n"
        "Если вы не запрашивали вход, ничего не делайте: без вашего подтверждения никто "
        "не войдёт, а ссылка скоро перестанет работать.\n"
        "\n"
        "Никому не пересылайте это письмо: ссылка из него открывает доступ к вашей "
        "учётной записи.\n"
    )
    return OutgoingEmail(to=address, subject=f"Вход в {PRODUCT_NAME}", body=body)


# --- SMTP ---------------------------------------------------------------------------

_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _ehlo_name(origin: str) -> str:
    """What the client calls itself in EHLO: the public host name. smtplib would
    otherwise ask the resolver for the machine's FQDN, which is slow and reveals
    the machine."""
    host = urlsplit(origin).hostname or ""
    if all(_HOST_LABEL.fullmatch(label) for label in host.split(".")):
        return host
    return "localhost"


def default_tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class SmtpEmailSender:
    """Sends over SMTP, always over TLS (see the module docstring). One connection
    per message: sign-in mail is rare, and nothing is kept open or shared."""

    def __init__(self, settings: EmailSettings, *, tls_context: Optional[ssl.SSLContext] = None):
        """`tls_context` is for tests (a context that trusts their own certificate
        authority); the default verifies against the system's trust store."""
        self._settings = settings
        self._context = tls_context if tls_context is not None else default_tls_context()

    async def send(self, email: OutgoingEmail) -> None:
        message = self._build(email)
        try:
            await anyio.to_thread.run_sync(self._deliver, message, email.to)
        except EmailDeliveryError:
            raise
        except Exception as exc:  # smtplib, ssl, socket, anything: see the docstring
            raise EmailDeliveryError(type(exc).__name__) from None

    def _build(self, email: OutgoingEmail) -> EmailMessage:
        settings = self._settings
        sender_name, _, sender_domain = settings.sender.partition("@")
        message = EmailMessage()
        message["From"] = Address(display_name=PRODUCT_NAME, username=sender_name, domain=sender_domain)
        recipient_name, _, recipient_domain = email.to.partition("@")
        message["To"] = Address(username=recipient_name, domain=recipient_domain)
        message["Subject"] = email.subject
        message["Date"] = formatdate(usegmt=True)
        message["Message-ID"] = make_msgid(domain=sender_domain)
        message["Auto-Submitted"] = "auto-generated"  # RFC 3834: no vacation replies to it
        # Quoted-printable: 7-bit clean on the wire whatever the server advertises
        # (smtplib does not negotiate 8BITMIME), and readable by every mail client.
        message.set_content(email.body, charset="utf-8", cte="quoted-printable")
        return message

    def _deliver(self, message: EmailMessage, recipient: str) -> None:
        settings = self._settings
        common = {
            "host": settings.smtp_host,
            "port": settings.smtp_port,
            "timeout": settings.smtp_timeout,
            "local_hostname": _ehlo_name(settings.origin),
        }
        if settings.smtp_security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(context=self._context, **common)
        else:
            client = smtplib.SMTP(**common)
        with client:
            if settings.smtp_security == "starttls":
                client.ehlo()
                client.starttls(context=self._context)  # SMTPNotSupportedError if not offered
                client.ehlo()
            if settings.smtp_username is not None:
                client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(message, from_addr=settings.sender, to_addrs=[recipient])
