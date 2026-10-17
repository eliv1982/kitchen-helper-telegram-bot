"""Shared helpers for the email sign-in tests: a mailer that records instead of
sending, and a harness that runs the real application, with email login on, against a
real (migrated) SQLite file.

Nothing leaves the process: the application is given a FakeMailer through its
email_sender seam, so no SMTP connection is made and no mail is sent. (The SMTP sender
itself is tested separately, against a server on the loopback interface.)

The clock is the same FakeClock as the Telegram tests use, so a test moves the "now"
of links and sessions together.
"""

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Optional
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from house_helper.persistence.allowlist import allow
from house_helper.persistence.database import (
    create_db_engine,
    create_session_factory,
    upgrade_database,
)
from house_helper.persistence.models import User
from house_helper.web.app import create_app
from house_helper.web.mailer import OutgoingEmail
from house_helper.web.settings import EmailSettings, WebSettings
from tests.auth_support import (
    ORIGIN,
    FakeClock,
    FakeTelegram,
    auth_settings,
    cookie_attributes,  # noqa: F401  (re-exported for the tests)
)
from tests.web_support import TIMEOUT, running, sqlite_url

ADDRESS = "elena@example.com"
SENDER = "login@kitchen.example.test"
_URL = re.compile(r"https?://\S+")


class FakeMailer:
    """An EmailSender that keeps what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[OutgoingEmail] = []
        self.attempted: list[OutgoingEmail] = []  # everything send() was given, delivered or not
        self.attempts = 0
        self.failure: Optional[BaseException] = None
        self._gate: Optional[tuple[asyncio.Event, asyncio.Event]] = None

    def hold(self) -> tuple[asyncio.Event, asyncio.Event]:
        """Make send() wait: returns (arrived, release)."""
        self._gate = (asyncio.Event(), asyncio.Event())
        return self._gate

    async def send(self, email: OutgoingEmail) -> None:
        self.attempts += 1
        self.attempted.append(email)
        if self._gate is not None:
            arrived, release = self._gate
            arrived.set()
            await release.wait()
        if self.failure is not None:
            raise self.failure
        self.sent.append(email)

    def last_url(self) -> str:
        """The sign-in link in the latest message."""
        return _URL.findall(self.sent[-1].body)[-1]

    def last_attempted_url(self) -> str:
        """The sign-in link in the latest message given to send(), even if it failed."""
        return _URL.findall(self.attempted[-1].body)[-1]


@dataclass(frozen=True)
class Link:
    url: str
    challenge_id: str
    token: str

    @property
    def path(self) -> str:
        """What a browser requests when it opens the link: no fragment."""
        parts = urlsplit(self.url)
        return f"{parts.path}?{parts.query}"


def parse_link(url: str) -> Link:
    parts = urlsplit(url)
    query = dict(pair.split("=", 1) for pair in parts.query.split("&"))
    return Link(url=url, challenge_id=query["c"], token=parts.fragment)


def email_settings(origin: str = ORIGIN, **options: Any) -> EmailSettings:
    options.setdefault("smtp_host", "smtp.example.test")
    options.setdefault("sender", SENDER)
    return EmailSettings(origin=origin, **options)


@dataclass
class EmailHarness:
    app: Any
    client: httpx.AsyncClient
    mailer: FakeMailer
    clock: FakeClock
    settings: EmailSettings
    factory: Any
    user_id: UUID
    origin: str = ORIGIN
    telegram: Optional[FakeTelegram] = None
    extra_clients: list[httpx.AsyncClient] = field(default_factory=list)

    def new_client(self) -> httpx.AsyncClient:
        """Another browser (or device): its own cookie jar, the same application."""
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=self.origin, timeout=TIMEOUT
        )
        self.extra_clients.append(client)
        return client

    async def ask(self, address: str = ADDRESS, client: Optional[httpx.AsyncClient] = None):
        """POST /auth/email/request, as a browser's script would."""
        return await (client or self.client).post(
            "/auth/email/request", json={"email": address}, headers={"Origin": self.origin}
        )

    async def ask_and_get_link(self, address: str = ADDRESS) -> Link:
        before = len(self.mailer.sent)
        response = await self.ask(address)
        assert response.status_code == 202, response.text
        assert len(self.mailer.sent) == before + 1, "no email was sent"
        return parse_link(self.mailer.last_url())

    async def confirm(
        self,
        link: Link,
        client: Optional[httpx.AsyncClient] = None,
        *,
        token: Optional[str] = None,
        challenge_id: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        """The form POST of the confirmation page."""
        return await (client or self.client).post(
            "/auth/email/confirm",
            data={
                "c": link.challenge_id if challenge_id is None else challenge_id,
                "token": link.token if token is None else token,
            },
            headers={"Origin": self.origin, **(headers or {})},
        )

    async def sign_in(self, client: Optional[httpx.AsyncClient] = None) -> httpx.Response:
        """A complete, honest email sign-in by one browser; the confirm response."""
        link = await self.ask_and_get_link()
        return await self.confirm(link, client)


def seed_email_user(factory, *, address: str = ADDRESS, display_name: str = "Елена") -> UUID:
    """The one provisioned user, with an email entry on the allowlist."""
    with factory() as session, session.begin():
        user = User(display_name=display_name)
        session.add(user)
        session.flush()
        allow(session, user, "email", address)
        return user.id


@asynccontextmanager
async def email_app(
    tmp_path,
    *,
    origin: str = ORIGIN,
    seed: bool = True,
    telegram: bool = False,
    configure: Optional[Callable[[Any], None]] = None,
    mailer: Optional[FakeMailer] = None,
    **options: Any,
):
    """The application with email login on (and Telegram login too, if asked),
    started, and a client for it. `options` go to EmailSettings. `configure(app)` runs
    before the application starts."""
    url = sqlite_url(tmp_path / "email.db")
    engine = create_db_engine(url)
    upgrade_database(engine)
    factory = create_session_factory(engine)
    user_id = seed_email_user(factory) if seed else None
    clock = FakeClock()
    settings = email_settings(origin, **options)
    mailer = mailer or FakeMailer()
    fake_telegram = None
    web = WebSettings(database_url=url, email=settings)
    transport = None
    if telegram:
        telegram_settings = auth_settings(origin)
        fake_telegram = FakeTelegram(clock, telegram_settings)
        transport = fake_telegram.transport()
        web = WebSettings(database_url=url, auth=telegram_settings, email=settings)
    app = create_app(web, provider_transport=transport, clock=clock, email_sender=mailer)
    if configure is not None:
        configure(app)
    try:
        async with running(app):
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=origin, timeout=TIMEOUT
            )
            harness = EmailHarness(
                app, client, mailer, clock, settings, factory, user_id, origin, fake_telegram
            )
            try:
                yield harness
            finally:
                for opened in [client, *harness.extra_clients]:
                    await asyncio.wait_for(opened.aclose(), TIMEOUT)
    finally:
        engine.dispose()


def minutes(count: float) -> timedelta:
    return timedelta(minutes=count)
