"""A person's profile: their display name, their text size, and the ways they sign in.

The profile is the account's own small set of settings, not a second identity. It reuses
users.display_name (the 200-character column every user already has; migration 0013 adds
users.text_size) and it never says who the request is for: every function takes the user
id of the server-side session and finds a row only through it. There is no user id in a
request body and nothing here reads one.

Display name. Trimmed, 1 to 200 Unicode code points, and no character of Unicode
category C (control, format -- zero-width and bidirectional marks included -- surrogate,
private-use or unassigned): the same kind of rule the administrative commands apply to a
name they type, implemented here for this path rather than imported from them, because a
profile name is not an enrollment name. Duplicates ARE allowed: two people may have the
same display name, and a name never identifies anybody -- the session does. Reusing the
column means the name an operator set at enrollment is the name the person starts with.

Text size. One of TextSize: 'normal' (100%) or 'large' (120%). It is applied by the page
to the authenticated application only. The database's CHECK is the closed list; the
application validates before writing so a bad request is refused with a clear error
rather than a database failure.

Sign-in methods. Read, never written here: the identity_links of the user, each joined to
its allowlist entry to tell an active identity from a revoked one. Only identities that
have actually been used to sign in have a link, so the list is "the ways this account has
signed in", not "the ways an administrator provisioned". A revoked entry keeps its link
(revoking withdraws access; it does not erase the record), so it appears with status
'revoked'. The Telegram subject is used only to find the allowlist entry and is never put
into a returned value: for an email identity the address is returned (the person's own
address, which is what the subject is); for Telegram nothing but the provider name is.

Writers write first, as everywhere in this layer: update_profile is one UPDATE whose WHERE
is the user id, so ownership is the statement's own condition. It never commits; the
caller owns the transaction (web.database.WebDatabase.run). Reads write nothing. Results
are plain values, not ORM objects.
"""

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.models import (
    AllowlistEntry,
    AllowlistProvider,
    IdentityLink,
    TextSize,
    User,
)

# The column's width, in Unicode code points (users.display_name).
MAX_DISPLAY_NAME_LENGTH = 200

# Which text sizes exist. The database has the same closed list as a CHECK.
TEXT_SIZE_VALUES = frozenset(member.value for member in TextSize)

# The status a sign-in method is reported with.
LOGIN_ACTIVE = "active"
LOGIN_REVOKED = "revoked"

# Distinguishes "leave this field as it is" from "set it to this value" (None is not a
# value either field may take: display_name is never empty and text_size is never NULL).
_UNSET = object()


class ProfileNotFound(Exception):
    """No user with this id. A live session cannot normally reach it (a session is
    deleted with its user), but the read and the write report it rather than guessing."""


class InvalidDisplayName(ValueError):
    """The display name cannot be stored: empty, too long, or a forbidden character."""


class InvalidTextSize(ValueError):
    """The text size is not one of TEXT_SIZE_VALUES."""


@dataclass(frozen=True)
class LoginMethod:
    """One way the account has signed in, as the profile shows it.

    `address` is the verified email address for an email identity and None for every
    other provider: a Telegram subject is an internal identifier and is deliberately not
    part of this value (see the module docstring).
    """

    provider: str
    address: Optional[str]
    status: str  # LOGIN_ACTIVE or LOGIN_REVOKED


@dataclass(frozen=True)
class ProfileView:
    """What the profile API answers: the two editable settings and the ways in."""

    display_name: Optional[str]
    text_size: str
    login_methods: tuple[LoginMethod, ...]


def normalize_display_name(raw: str) -> str:
    """The display name as it is stored, or InvalidDisplayName.

    Trimmed the way every other stored text is (str.strip), non-empty, at most
    MAX_DISPLAY_NAME_LENGTH code points, and no Unicode category C character. The
    rule is the same one the administrative commands use, written here so that the
    profile path does not depend on them (and so a later change to one does not
    silently change the other).
    """
    if not isinstance(raw, str):
        raise InvalidDisplayName("the display name must be text")
    text = raw.strip()
    if not text:
        raise InvalidDisplayName("the display name must not be empty")
    if len(text) > MAX_DISPLAY_NAME_LENGTH:
        raise InvalidDisplayName(
            f"the display name is at most {MAX_DISPLAY_NAME_LENGTH} characters"
        )
    # Control, format (zero-width and bidirectional marks), surrogate, private-use and
    # unassigned characters: what a page, a log or a terminal could be made to
    # misrepresent. Combining marks are not category C and are ordinary text.
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise InvalidDisplayName(
            "the display name must not contain control or invisible characters"
        )
    return text


def normalize_text_size(raw: str) -> str:
    """'normal' or 'large', or InvalidTextSize."""
    if not isinstance(raw, str) or raw not in TEXT_SIZE_VALUES:
        raise InvalidTextSize("the text size must be 'normal' or 'large'")
    return raw


def get_profile(session: OrmSession, user_id: UUID) -> ProfileView:
    """The profile of `user_id`, read through the session's user id and nothing else."""
    user = session.get(User, user_id)
    if user is None:
        raise ProfileNotFound(str(user_id))
    return _view(session, user)


def update_profile(
    session: OrmSession,
    user_id: UUID,
    *,
    display_name: Any = _UNSET,
    text_size: Any = _UNSET,
    now: datetime,
) -> ProfileView:
    """Change the fields that are given (at least one), and return the profile.

    Ownership is the UPDATE's own WHERE: there is no separate read-check-write step. A
    field that is not named keeps its value. Raises InvalidDisplayName / InvalidTextSize
    before writing, and ProfileNotFound when the user does not exist.
    """
    values: dict[str, Any] = {}
    if display_name is not _UNSET:
        values["display_name"] = normalize_display_name(display_name)
    if text_size is not _UNSET:
        values["text_size"] = normalize_text_size(text_size)
    if not values:
        raise ValueError("an update must name at least one field")
    values["updated_at"] = now
    result = session.execute(
        sa.update(User).where(User.id == user_id).values(**values)
    )
    if result.rowcount != 1:
        raise ProfileNotFound(str(user_id))
    # The row may already be in this session's identity map (the profile's caller may
    # have read it): read it again from the database so the answer is the new values.
    user = session.get(User, user_id, populate_existing=True)
    if user is None:  # (not reachable after a one-row UPDATE; kept for the type checker)
        raise ProfileNotFound(str(user_id))
    return _view(session, user)


def _view(session: OrmSession, user: User) -> ProfileView:
    return ProfileView(
        display_name=user.display_name,
        text_size=user.text_size or TextSize.NORMAL.value,
        login_methods=_login_methods(session, user.id),
    )


def _login_methods(session: OrmSession, user_id: UUID) -> tuple[LoginMethod, ...]:
    """The user's identities that have signed in, oldest link first.

    The email address is selected only for an email identity; the Telegram subject is
    used by the join and never selected into a result (see the module docstring).
    """
    rows = session.execute(
        sa.select(
            IdentityLink.provider,
            sa.case(
                (
                    IdentityLink.provider == AllowlistProvider.EMAIL.value,
                    IdentityLink.subject,
                ),
                else_=sa.null(),
            ).label("address"),
            AllowlistEntry.id.label("entry_id"),
            AllowlistEntry.revoked_at,
        )
        .outerjoin(
            AllowlistEntry,
            sa.and_(
                AllowlistEntry.user_id == IdentityLink.user_id,
                AllowlistEntry.provider == IdentityLink.provider,
                AllowlistEntry.subject == IdentityLink.subject,
            ),
        )
        .where(IdentityLink.user_id == user_id)
        .order_by(IdentityLink.created_at, IdentityLink.id)
    ).all()
    methods = []
    for provider, address, entry_id, revoked_at in rows:
        status = LOGIN_ACTIVE if entry_id is not None and revoked_at is None else LOGIN_REVOKED
        methods.append(LoginMethod(provider=provider, address=address, status=status))
    return tuple(methods)
