"""The sign-in email: what it says, and how it leaves.

The message tests need no network. The transport tests talk to a small SMTP server
on the loopback interface (tests/smtp_support.py) that speaks real TLS with
certificates made for the test run, so what is shown is that the sender behaves as
documented against something that answers: TLS before the login and before the
message, no fall-back to plaintext, the certificate and host name checked, no secret
in what it raises. No mail leaves the machine and no real mail server is contacted.
"""

import email
import email.policy
import ipaddress
import re
import ssl
import time
from datetime import timedelta
from uuid import uuid4

import pytest

from house_helper.web.mailer import (
    CONFIRM_PATH,
    EmailDeliveryError,
    OutgoingEmail,
    SmtpEmailSender,
    compose_login_email,
    default_tls_context,
    login_link,
    minutes_in_russian,
)
from tests.email_support import ADDRESS, SENDER, email_settings
from tests.smtp_support import client_context_trusting, make_pki, smtp_server

LINK = "https://kitchen.example.test/auth/email/confirm?c=" + str(uuid4()) + "#" + "A" * 43
PASSWORD = "mail-password-0f9e8d7c"


# --- the message ------------------------------------------------------------------------


def test_the_message_is_in_russian_and_holds_the_link_once_and_nothing_else_to_open():
    message = compose_login_email(ADDRESS, LINK, timedelta(minutes=10))

    assert message.subject == "Вход в House Helper"
    assert message.to == ADDRESS
    assert re.findall(r"https?://\S+", message.body) == [LINK]
    assert "Ссылка действует 10 минут" in message.body
    assert "Здравствуйте" in message.body and "Войти" in message.body
    assert "на любом устройстве" in message.body  # it says the link works anywhere
    assert "Никому не пересылайте" in message.body
    assert ADDRESS not in message.body  # a forwarded mail gives away no address


@pytest.mark.parametrize(
    "minutes, shown",
    [
        (1, "1 минута"),
        (2, "2 минуты"),
        (4, "4 минуты"),
        (5, "5 минут"),
        (10, "10 минут"),
        (11, "11 минут"),
        (12, "12 минут"),
        (14, "14 минут"),
        (15, "15 минут"),
        (21, "21 минута"),
        (22, "22 минуты"),
        (25, "25 минут"),
        (101, "101 минута"),
        (111, "111 минут"),
    ],
)
def test_the_lifetime_is_spelled_with_the_right_russian_plural(minutes, shown):
    assert minutes_in_russian(minutes) == shown
    assert f"Ссылка действует {shown} " in compose_login_email(ADDRESS, LINK, timedelta(minutes=minutes)).body


def test_a_lifetime_under_a_minute_is_shown_as_one_minute_not_zero():
    assert "Ссылка действует 1 минута " in compose_login_email(ADDRESS, LINK, timedelta(seconds=20)).body
    assert "Ссылка действует 2 минуты " in compose_login_email(ADDRESS, LINK, timedelta(seconds=61)).body


def test_the_link_keeps_the_secret_in_the_fragment_and_the_id_in_the_query():
    challenge_id, token = uuid4(), "B" * 43

    link = login_link("https://kitchen.example.test", challenge_id, token)

    assert link == f"https://kitchen.example.test{CONFIRM_PATH}?c={challenge_id}#{token}"
    before_fragment, _, fragment = link.partition("#")
    assert fragment == token and token not in before_fragment


@pytest.mark.parametrize(
    "address",
    [
        "a<b@example.com",
        "a>b@example.com",
        'a"b@example.com',
        "a,b@example.com",
        "a b@example.com",
        "a@b.com\r\nBcc: evil@example.net",
        "a@b.com\nSubject: x",
        "a@Example.com",  # not the normalized form: the domain is lower-case
        "A@EXAMPLE.COM",
        "a@example",
        "a@@example.com",
        "é@example.com",
        "a@exämple.com",
        "a%b@example.com",
        "",
        "plain",
    ],
)
def test_a_message_cannot_be_addressed_to_anything_but_a_plain_address(address):
    with pytest.raises(ValueError):
        OutgoingEmail(to=address, subject="s", body="b")
    with pytest.raises(ValueError):
        compose_login_email(address, LINK, timedelta(minutes=10))


@pytest.mark.parametrize(
    "address", ["Victim@example.com", "VICTIM@example.com", "vIcTiM+Tag@example.com", "V.i.C@sub.example.co.uk"]
)
def test_a_message_can_be_addressed_to_the_case_an_administrator_approved(address):
    message = compose_login_email(address, LINK, timedelta(minutes=10))

    assert message.to == address  # exactly, never lower-cased on the way
    assert OutgoingEmail(to=address, subject="s", body="b").to == address


def test_the_link_and_the_address_stay_out_of_the_representation_of_a_message():
    message = compose_login_email(ADDRESS, LINK, timedelta(minutes=10))

    assert LINK not in repr(message) and ADDRESS not in repr(message)
    assert LINK.split("#")[1] not in repr(message)


# --- how the message is built for SMTP ----------------------------------------------------


def built(tmp_path=None, **options):
    sender = SmtpEmailSender(email_settings(**options))
    return sender._build(compose_login_email(ADDRESS, LINK, timedelta(minutes=10)))


def test_the_built_message_has_the_headers_a_receiving_server_expects():
    message = built()

    assert message["To"] == ADDRESS
    assert message["From"] == f"House Helper <{SENDER}>"
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["Message-ID"].endswith("@kitchen.example.test>")
    assert re.fullmatch(r"[A-Z][a-z]{2}, \d{1,2} [A-Z][a-z]{2} \d{4} \d\d:\d\d:\d\d (\+0000|GMT)", str(message["Date"]))
    assert str(message["Subject"]) == "Вход в House Helper"
    assert message.get_content_type() == "text/plain" and message.get_content_charset() == "utf-8"


def test_the_body_travels_as_seven_bit_quoted_printable_and_reads_back_as_written():
    message = built()
    wire = message.as_bytes(policy=email.policy.SMTP)

    assert all(byte < 128 for byte in wire)  # nothing 8-bit, whatever the server advertises
    assert message["Content-Transfer-Encoding"] == "quoted-printable"
    parsed = email.message_from_bytes(wire, policy=email.policy.default)
    body = parsed.get_content()
    assert LINK in body and "Здравствуйте" in body


def test_no_header_is_built_from_anything_a_caller_could_inject_into():
    message = built()

    for name, value in message.items():
        assert "\n" not in str(value) and "\r" not in str(value), name


# --- TLS settings ----------------------------------------------------------------------------


def test_the_default_tls_context_checks_the_certificate_and_the_name_and_wants_tls_12():
    context = default_tls_context()

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_the_sender_uses_that_context_unless_a_test_hands_it_another():
    assert SmtpEmailSender(email_settings())._context.verify_mode is ssl.CERT_REQUIRED
    trusting = client_context_trusting(make_pki())
    assert SmtpEmailSender(email_settings(), tls_context=trusting)._context is trusting


# --- SMTP over a real, local, TLS-speaking server --------------------------------------------------


@pytest.fixture(scope="module")
def pki():
    return make_pki()


def sender_for(server, pki, *, security="starttls", username="mailer", password=PASSWORD, **options):
    settings = email_settings(
        smtp_host="127.0.0.1",
        smtp_port=server.port,
        smtp_security=security,
        smtp_username=username,
        smtp_password=password,
        smtp_timeout=options.pop("smtp_timeout", 5.0),
        **options,
    )
    return SmtpEmailSender(settings, tls_context=client_context_trusting(pki))


def a_message():
    return compose_login_email(ADDRESS, LINK, timedelta(minutes=10))


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", ["Victim@example.com", "VICTIM@example.com", "victim@example.com"])
async def test_the_mail_server_is_told_the_approved_mailbox_exactly(tmp_path, pki, approved):
    with smtp_server(tmp_path, pki) as server:
        await sender_for(server, pki).send(compose_login_email(approved, LINK, timedelta(minutes=10)))

    (received,) = server.messages
    assert received.recipients == [approved]  # the envelope: where the server delivers
    rcpt = [line.split(" ", 1)[1] for line in server.lines if line.upper().startswith("RCPT")]
    assert rcpt == [f"TO:<{approved}>"]  # what went over the wire, byte for byte
    parsed = email.message_from_bytes(received.data, policy=email.policy.default)
    assert parsed["To"] == approved


@pytest.mark.asyncio
@pytest.mark.parametrize("security", ["starttls", "ssl"])
async def test_the_message_is_delivered_over_tls_and_the_login_comes_only_after_it(
    tmp_path, pki, security
):
    with smtp_server(tmp_path, pki, implicit_tls=security == "ssl") as server:
        await sender_for(server, pki, security=security).send(a_message())

    (received,) = server.messages
    assert received.sender == SENDER and received.recipients == [ADDRESS]
    parsed = email.message_from_bytes(received.data, policy=email.policy.default)
    assert LINK in parsed.get_content()
    assert parsed["To"] == ADDRESS
    # everything the server was sent after the greeting arrived under TLS, except the
    # first EHLO and STARTTLS themselves; in particular the login and the message
    for line, secure in zip(server.lines, server.tls_established):
        if line.split(" ")[0] in {"AUTH", "MAIL", "RCPT", "DATA"}:
            assert secure, line
    assert any(line.startswith("AUTH") for line in server.lines)
    if security == "starttls":
        assert server.lines.index("STARTTLS") < min(
            i for i, line in enumerate(server.lines) if line.startswith("AUTH")
        )


@pytest.mark.asyncio
async def test_without_credentials_no_login_is_attempted(tmp_path, pki):
    with smtp_server(tmp_path, pki) as server:
        await sender_for(server, pki, username=None, password=None).send(a_message())

    assert not any(line.startswith("AUTH") for line in server.lines)
    assert len(server.messages) == 1


@pytest.mark.asyncio
async def test_a_server_that_does_not_offer_starttls_gets_nothing_not_even_the_login(tmp_path, pki):
    with smtp_server(tmp_path, pki, offer_starttls=False) as server:
        with pytest.raises(EmailDeliveryError) as caught:
            await sender_for(server, pki).send(a_message())

    assert caught.value.reason == "SMTPNotSupportedError"
    assert not any(line.split(" ")[0] in {"AUTH", "MAIL", "RCPT", "DATA"} for line in server.lines)
    assert server.messages == []
    assert PASSWORD not in " ".join(server.lines)  # the secret was never put on the wire


@pytest.mark.asyncio
@pytest.mark.parametrize("security", ["starttls", "ssl"])
async def test_a_certificate_nobody_trusts_is_a_failure_and_nothing_is_sent(tmp_path, pki, security):
    other_authority = make_pki()
    settings_server = {"implicit_tls": security == "ssl"}
    with smtp_server(tmp_path, other_authority, **settings_server) as server:
        with pytest.raises(EmailDeliveryError) as caught:
            await sender_for(server, pki, security=security).send(a_message())  # trusts `pki` only

    assert caught.value.reason == "SSLCertVerificationError"
    assert server.messages == []
    assert not any(line.split(" ")[0] in {"AUTH", "MAIL", "RCPT", "DATA"} for line in server.lines)


@pytest.mark.asyncio
async def test_a_certificate_for_another_name_is_a_failure(tmp_path):
    wrong_name = make_pki(leaf_names=("smtp.elsewhere.test", ipaddress.ip_address("10.9.8.7")))
    with smtp_server(tmp_path, wrong_name) as server:
        with pytest.raises(EmailDeliveryError) as caught:
            await sender_for(server, wrong_name).send(a_message())  # connects to 127.0.0.1

    assert caught.value.reason == "SSLCertVerificationError"
    assert server.messages == []


@pytest.mark.asyncio
async def test_the_default_context_does_not_trust_a_test_authority(tmp_path, pki):
    with smtp_server(tmp_path, pki) as server:
        sender = SmtpEmailSender(
            email_settings(
                smtp_host="127.0.0.1", smtp_port=server.port, smtp_username="mailer",
                smtp_password=PASSWORD, smtp_timeout=5.0,
            )
        )  # the system's trust store, as in production
        with pytest.raises(EmailDeliveryError) as caught:
            await sender.send(a_message())

    assert caught.value.reason == "SSLCertVerificationError"


@pytest.mark.asyncio
async def test_a_refused_recipient_is_a_failure_that_does_not_quote_the_server(tmp_path, pki):
    with smtp_server(tmp_path, pki, refuse_recipient=True) as server:
        with pytest.raises(EmailDeliveryError) as caught:
            await sender_for(server, pki).send(a_message())

    assert caught.value.reason == "SMTPRecipientsRefused"
    for text in (str(caught.value), repr(caught.value), repr(caught.value.args)):
        assert ADDRESS not in text and "Recipient address rejected" not in text
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_a_refused_login_is_a_failure_that_does_not_carry_the_password(tmp_path, pki):
    with smtp_server(tmp_path, pki) as server:
        with pytest.raises(EmailDeliveryError) as caught:
            await sender_for(server, pki, password="not-the-password").send(a_message())

    assert caught.value.reason == "SMTPAuthenticationError"
    for text in (str(caught.value), repr(caught.value)):
        assert "not-the-password" not in text and PASSWORD not in text
    assert server.messages == []


@pytest.mark.asyncio
async def test_a_server_that_never_answers_is_given_up_on(tmp_path, pki):
    with smtp_server(tmp_path, pki, silent=True) as server:
        started = time.monotonic()
        with pytest.raises(EmailDeliveryError) as caught:
            await sender_for(server, pki, smtp_timeout=0.3).send(a_message())

    assert time.monotonic() - started < 3
    # smtplib reports a read that timed out while waiting for the greeting as a
    # disconnect
    assert caught.value.reason in {"SMTPServerDisconnected", "TimeoutError", "timeout"}


@pytest.mark.asyncio
async def test_a_port_nobody_listens_on_is_a_failure(tmp_path, pki):
    with smtp_server(tmp_path, pki) as server:
        port = server.port
    sender = SmtpEmailSender(
        email_settings(smtp_host="127.0.0.1", smtp_port=port, smtp_timeout=1.0),
        tls_context=client_context_trusting(pki),
    )

    with pytest.raises(EmailDeliveryError) as caught:
        await sender.send(a_message())

    # Linux refuses at once; Windows retries a refused loopback connection until the
    # timeout runs out
    assert caught.value.reason in {"ConnectionRefusedError", "TimeoutError"}


@pytest.mark.asyncio
async def test_the_client_names_itself_by_the_public_host_not_by_the_machine(tmp_path, pki):
    with smtp_server(tmp_path, pki) as server:
        await sender_for(server, pki).send(a_message())

    greetings = [line for line in server.lines if line.upper().startswith("EHLO")]
    assert greetings and all(line.upper() == "EHLO KITCHEN.EXAMPLE.TEST" for line in greetings)
