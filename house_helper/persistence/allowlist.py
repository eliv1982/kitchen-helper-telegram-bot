"""The allowlist: who may sign in while onboarding is closed.

The product has ONE initial user, created first, and two ways for that user to
sign in: Telegram OIDC (primary) and a verified email (backup). Each way is an
entry that belongs to the user (AllowlistEntry.user_id), keyed by
(provider, subject):

  telegram  the `sub` of Telegram's OIDC ID token, compared exactly. Never a
            username: usernames can be changed and taken over.
  email     a verified address, trimmed and lower-cased. Nothing more is folded
            together: "cook+tag@example.com" and "cook@example.com" are
            different entries, because treating aliases as one would let anyone
            who can create an alias walk in.

Both logins therefore resolve to the same User through an exact match on an
entry that was provisioned in advance: nothing here (or in the schema) links an
identity to a user by a name, a display name or any claim that is not itself on
the list. The rules for what a subject may look like are in identifiers.py and
are shared with IdentityLink, whose CHECKs and composite foreign key keep the
two tables consistent.

The caller passes only what the provider has vouched for: the `sub` of a
validated ID token, an address whose ownership was just proven. Deciding that
is the sign-in increment's job; this module only answers "who, if anyone, may
this identity sign in as".

Fail closed: allowed_user returns a user in one case only, an existing entry
whose revoked_at is NULL and whose user is not disabled. A malformed or unknown
value gives None (it never raises on its input, so a caller cannot turn an
exception into access); a database error propagates and must be treated as a
refusal. Nothing here deletes an entry: revoking sets revoked_at, and lifting a
revocation clears it on the same row.

Revoking does not end sessions that already exist. The code that validates a
session must check allowed_user again (or revoke the user's sessions); that
belongs to the sign-in increment.

None of the functions commits; the caller owns the transaction.
"""

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.base import utcnow
from house_helper.persistence.identifiers import MAX_SUBJECT_LENGTH, normalize_identity
from house_helper.persistence.models import AllowlistEntry, User

__all__ = ["MAX_SUBJECT_LENGTH", "allow", "allowed_user", "is_allowed", "normalize", "revoke"]

normalize = normalize_identity  # the (provider, subject) pair as it is stored, or ValueError


def _entry(session: OrmSession, provider: str, subject: str) -> Optional[AllowlistEntry]:
    return session.scalar(
        sa.select(AllowlistEntry).where(
            AllowlistEntry.provider == provider, AllowlistEntry.subject == subject
        )
    )


def allowed_user(session: OrmSession, provider: object, subject: object) -> Optional[User]:
    """The user this identity may sign in as, or None.

    None unless there is an entry for exactly this (provider, subject), it is not
    revoked, and its user is not disabled.
    """
    try:
        provider, subject = normalize(provider, subject)
    except ValueError:
        return None
    return session.scalar(
        sa.select(User)
        .join(AllowlistEntry, AllowlistEntry.user_id == User.id)
        .where(
            AllowlistEntry.provider == provider,
            AllowlistEntry.subject == subject,
            AllowlistEntry.revoked_at.is_(None),
            User.disabled_at.is_(None),
        )
    )


def is_allowed(session: OrmSession, provider: object, subject: object) -> bool:
    """True if this identity has an allowlist entry that has not been revoked,
    for a user who has not been disabled."""
    return allowed_user(session, provider, subject) is not None


def allow(
    session: OrmSession,
    user: User,
    provider: object,
    subject: object,
    note: Optional[str] = None,
) -> AllowlistEntry:
    """Provision a way for `user` to sign in (ValueError if it is not a valid one).

    Already provisioned for this user: nothing changes. Revoked: the revocation
    is lifted on the existing row, so the pair stays unique. `note` (who asked,
    why) replaces the stored note when given. An identity that is on the list for
    a different user is refused (ValueError): an identity is never moved from one
    user to another by asking for it again.
    """
    provider, subject = normalize(provider, subject)
    if user.id is None:  # a user that is still pending gets its id on flush
        session.flush()
    entry = _entry(session, provider, subject)
    if entry is None:
        entry = AllowlistEntry(user_id=user.id, provider=provider, subject=subject, note=note)
        session.add(entry)
    elif entry.user_id != user.id:
        raise ValueError("this identity is already on the allowlist for another user")
    else:
        entry.revoked_at = None
        if note is not None:
            entry.note = note
    session.flush()
    return entry


def revoke(
    session: OrmSession, provider: object, subject: object, now: Optional[datetime] = None
) -> bool:
    """Withdraw an identity's access (ValueError if it is not a valid one).

    True if an active entry was revoked; False if there was none or it was
    already revoked (the first revocation time is kept). The row stays.
    """
    provider, subject = normalize(provider, subject)
    entry = _entry(session, provider, subject)
    if entry is None or entry.revoked_at is not None:
        return False
    entry.revoked_at = now if now is not None else utcnow()
    session.flush()
    return True
