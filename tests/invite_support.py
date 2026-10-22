"""Shared helpers for the Telegram invitation sign-in tests.

An invitation is issued the way the operator command does it (persistence.invitations.
issue_invitation, in the application's own clock), and a person follows it with a browser:
the page's script posts the code to /auth/invite/start, the browser goes to Telegram (the
fake one, tests/auth_support.FakeTelegram, which enforces the protocol), and comes back to the
ordinary callback. Browsers are httpx clients with their own cookie jars.
"""

from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

import httpx
import sqlalchemy as sa

from house_helper.persistence import authn
from house_helper.persistence.allowlist import allow
from house_helper.persistence.invitations import issue_invitation
from house_helper.persistence.models import (
    AllowlistEntry,
    IdentityInvitation,
    IdentityLink,
    OidcState,
    User,
)
from house_helper.persistence.models import Session as LoginSession
from tests.auth_support import ORIGIN, FakeTelegram, Harness

HOST = "kitchen.example.test"
MOTHER_SUB = "5550001112223334445"
OWNER_SUB = "7770001112223334446"
THIRD_SUB = "3330001112223334447"


@dataclass(frozen=True)
class Enrolled:
    """A provisioned user and the invitation issued for them (the secret exists only here)."""

    user_id: UUID
    invitation_id: UUID
    token: str
    display_name: str

    @property
    def code(self) -> str:
        return f"{self.invitation_id}.{self.token}"


def enroll(harness: Harness, display_name: str = "Мама", *, now: Optional[Any] = None) -> Enrolled:
    """What `enroll-user` does: a user with no identity and a live invitation."""
    with harness.factory() as session, session.begin():
        user = User(display_name=display_name)
        session.add(user)
        session.flush()
        issued = issue_invitation(session, user.id, now=now or harness.clock())
        return Enrolled(user.id, issued.invitation_id, issued.token, display_name)


async def start(
    client: httpx.AsyncClient, code: Any, *, origin: Optional[str] = ORIGIN, **headers: str
) -> httpx.Response:
    """What the invitation page's script does when the button is pressed."""
    sent = {"origin": origin} if origin is not None else {}
    return await client.post("/auth/invite/start", json={"code": code}, headers={**sent, **headers})


async def come_back(
    client: httpx.AsyncClient, telegram: FakeTelegram, started: httpx.Response, subject: str
) -> httpx.Response:
    """The person approves at Telegram and the browser returns to the callback."""
    assert started.status_code == 200, started.text
    return await client.get(telegram.authorize(started.json()["authorization_url"], subject))


async def follow(
    client: httpx.AsyncClient, telegram: FakeTelegram, enrolled: Enrolled, subject: str
) -> httpx.Response:
    """A complete, honest use of an invitation by one browser; the callback's response."""
    return await come_back(client, telegram, await start(client, enrolled.code), subject)


# --- looking at the outcome ---------------------------------------------------------


def invitation_row(harness: Harness, enrolled: Enrolled):
    with harness.factory() as db:
        return db.execute(
            sa.select(
                IdentityInvitation.consumed_at,
                IdentityInvitation.revoked_at,
                IdentityInvitation.expires_at,
            ).where(IdentityInvitation.id == enrolled.invitation_id)
        ).one()


def is_unused(harness: Harness, enrolled: Enrolled) -> bool:
    row = invitation_row(harness, enrolled)
    return row.consumed_at is None and row.revoked_at is None


def entries_of(harness: Harness, user_id: UUID) -> list[tuple[str, str, bool]]:
    """(provider, subject, revoked?) of the user's allowlist entries."""
    with harness.factory() as db:
        return [
            (row.provider, row.subject, row.revoked_at is not None)
            for row in db.execute(
                sa.select(AllowlistEntry.provider, AllowlistEntry.subject, AllowlistEntry.revoked_at)
                .where(AllowlistEntry.user_id == user_id)
                .order_by(AllowlistEntry.subject)
            )
        ]


def tables(harness: Harness) -> dict[str, int]:
    """Row counts of every table a sign-in may write."""
    with harness.factory() as db:
        return {
            model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model))
            for model in (
                User,
                AllowlistEntry,
                IdentityLink,
                LoginSession,
                OidcState,
                IdentityInvitation,
            )
        }


def sessions_of(harness: Harness, user_id: UUID) -> list[Any]:
    with harness.factory() as db:
        return list(db.scalars(sa.select(LoginSession).where(LoginSession.user_id == user_id)))


def states(harness: Harness) -> list[Any]:
    with harness.factory() as db:
        return list(db.execute(sa.select(OidcState.invitation_id, OidcState.consumed_at)).all())


def sign_in_directly(harness: Harness, client: httpx.AsyncClient, user_id: UUID, address: str) -> str:
    """Give `client` a live session of `user_id` through an email identity (the way a person who
    is already signed in, by some other way than Telegram, would be): the session token."""
    with harness.factory() as db, db.begin():
        allow(db, db.get(User, user_id), "email", address)
        issued = authn.complete_login(
            db, "email", address, now=harness.clock(), lifetime=harness.settings.session_lifetime
        )
    client.cookies.set(harness.settings.session_cookie_name, issued.token, domain=HOST, path="/")
    return issued.token


def shape(response: httpx.Response) -> tuple:
    """Everything of an answer that a caller could tell two answers apart by."""
    kept = ("cache-control", "referrer-policy", "content-type", "content-security-policy")
    return (
        response.status_code,
        response.content,
        tuple((name, response.headers[name]) for name in kept if name in response.headers),
        tuple(sorted(response.headers.get_list("set-cookie"))),
    )
