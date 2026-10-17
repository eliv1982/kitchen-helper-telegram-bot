"""A minimal SMTP server on the loopback interface, with real TLS, for testing the
SMTP sender against something that actually speaks the protocol.

Not a mail server: it accepts one command at a time from one connection at a time,
records everything it is sent, and can be told to misbehave in the ways the sender
has to survive (no STARTTLS on offer, a certificate nobody trusts, a refused recipient,
a refused login, silence). It listens on 127.0.0.1 only (the offline guard in
tests/network_guard.py lets loopback through), and its certificates are made here for
the test run: a throwaway certificate authority and a leaf it signed.
"""

import base64
import datetime
import ipaddress
import socket
import ssl
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass(frozen=True)
class Pki:
    ca_pem: bytes
    leaf_cert_pem: bytes
    leaf_key_pem: bytes


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def make_pki(*, leaf_names: tuple = (ipaddress.ip_address("127.0.0.1"), "localhost")) -> Pki:
    """A certificate authority and a server certificate it signed, valid for
    `leaf_names` (IP addresses and/or host names)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("test CA"))
        .issuer_name(_name("test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    names = [
        x509.IPAddress(name) if isinstance(name, ipaddress._BaseAddress) else x509.DNSName(name)
        for name in leaf_names
    ]
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name("smtp test server"))
        .issuer_name(ca.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    return Pki(
        ca_pem=ca.public_bytes(serialization.Encoding.PEM),
        leaf_cert_pem=leaf.public_bytes(serialization.Encoding.PEM),
        leaf_key_pem=leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def client_context_trusting(pki: Pki) -> ssl.SSLContext:
    """What the sender's tls_context is in tests: the default checks, with the test
    authority as the only trust anchor."""
    context = ssl.create_default_context(cadata=pki.ca_pem.decode("ascii"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def server_context(pki: Pki, directory) -> ssl.SSLContext:
    cert, key = directory / "server.crt", directory / "server.key"
    cert.write_bytes(pki.leaf_cert_pem)
    key.write_bytes(pki.leaf_key_pem)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    return context


@dataclass
class Received:
    sender: str
    recipients: list[str]
    data: bytes


@dataclass
class MiniSmtp:
    context: ssl.SSLContext
    implicit_tls: bool = False
    offer_starttls: bool = True
    users: dict[str, str] = field(default_factory=lambda: {"mailer": "mail-password-0f9e8d7c"})
    refuse_recipient: bool = False
    silent: bool = False  # accept the connection and say nothing
    port: int = 0
    lines: list[str] = field(default_factory=list)  # every command line, in order
    messages: list[Received] = field(default_factory=list)
    tls_established: list[bool] = field(default_factory=list)  # per command line
    handshake_errors: list[str] = field(default_factory=list)
    _listener: Optional[socket.socket] = None
    _thread: Optional[threading.Thread] = None
    _stopped: threading.Event = field(default_factory=threading.Event)

    # --- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._listener is not None:
            self._listener.close()

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._serve(connection)
            except (OSError, ssl.SSLError) as exc:
                self.handshake_errors.append(type(exc).__name__)
            finally:
                try:
                    connection.close()
                except OSError:
                    pass

    # --- the conversation -----------------------------------------------------------

    def _serve(self, connection: socket.socket) -> None:
        connection.settimeout(5)
        if self.silent:
            self._stopped.wait(2)
            return
        tls = False
        if self.implicit_tls:
            connection = self.context.wrap_socket(connection, server_side=True)
            tls = True
        reader = connection.makefile("rb")
        sender, recipients = "", []

        def reply(text: str) -> None:
            connection.sendall(text.encode("ascii") + b"\r\n")

        reply("220 localhost ESMTP test server")
        while True:
            raw = reader.readline()
            if not raw:
                return
            line = raw.decode("ascii", "replace").rstrip("\r\n")
            self.lines.append(line)
            self.tls_established.append(tls)
            verb = line.split(" ", 1)[0].upper()
            if verb == "EHLO":
                features = ["250-localhost"]
                if tls or not self.offer_starttls:
                    features.append("250-AUTH PLAIN LOGIN")
                if self.offer_starttls and not tls:
                    features.append("250-STARTTLS")
                features.append("250 SIZE 1048576")
                reply("\r\n".join(features))
            elif verb == "STARTTLS" and self.offer_starttls and not tls:
                reply("220 go ahead")
                connection = self.context.wrap_socket(connection, server_side=True)
                reader = connection.makefile("rb")
                tls = True
            elif verb == "AUTH":
                parts = line.split(" ")
                if len(parts) >= 3 and parts[1].upper() == "PLAIN":
                    _, user, password = base64.b64decode(parts[2]).decode().split("\0")
                    if self.users.get(user) == password:
                        reply("235 2.7.0 accepted")
                    else:
                        reply("535 5.7.8 authentication failed")
                else:
                    reply("504 5.5.4 mechanism not supported")
            elif verb == "MAIL":
                sender = line.split(":", 1)[1].strip().strip("<>").split(" ")[0].strip("<>")
                recipients = []
                reply("250 ok")
            elif verb == "RCPT":
                recipient = line.split(":", 1)[1].strip().strip("<>")
                if self.refuse_recipient:
                    reply(f"550 5.1.1 <{recipient}>: Recipient address rejected: no such user")
                else:
                    recipients.append(recipient)
                    reply("250 ok")
            elif verb == "DATA":
                reply("354 end with <CRLF>.<CRLF>")
                data = b""
                while not data.endswith(b"\r\n.\r\n"):
                    chunk = reader.readline()
                    if not chunk:
                        return
                    data += chunk
                self.messages.append(Received(sender, recipients, data[:-5]))
                reply("250 2.0.0 queued")
            elif verb == "QUIT":
                reply("221 bye")
                return
            else:
                reply("502 5.5.2 command not recognized")


@contextmanager
def smtp_server(tmp_path, pki: Pki, **options):
    server = MiniSmtp(server_context(pki, tmp_path), **options)
    server.start()
    try:
        yield server
    finally:
        server.stop()
