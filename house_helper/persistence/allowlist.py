"""The allowlist: who may sign in while onboarding is closed.

The product has ONE initial user, created first, and two ways for that user to
sign in: Telegram OIDC (primary) and a verified email (backup). Each way is an
entry that belongs to the user (AllowlistEntry.user_id), keyed by
(provider, subject):

  telegram  the `sub` of Telegram's OIDC ID token, compared exactly. Never a
            username: usernames can be changed and taken over.
  email     a verified address, trimmed, with its DOMAIN lower-cased and its local
            part exactly as the administrator approved it: "Cook@example.com" and
            "cook@example.com" may be two mailboxes (RFC 5321), so they are two
            entries, and approving one never approves the other. Nothing more is
            folded together: "cook+tag@example.com" and "cook@example.com" are
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

Revoking an identity ends the sessions it opened, in the same transaction (see
revoke): a session records the identity link it came through, so the revocation
is exactly as wide as the identity, no wider (the user's other identities, and the
sessions they opened, are untouched) and no narrower. It also withdraws the
emailed sign-in links that were issued for the identity and not yet used. Lifting
a revocation (allow) does NOT bring those sessions or links back: they were
revoked, as rows, and stay so. A person who signs in again through the re-allowed
identity gets a new session (by a new link, for an email identity). This holds for the functions of this module. A direct SQL UPDATE of
allowlist_entries.revoked_at is not supported and cannot be assumed to do any of
it: the database has no trigger to end the sessions, so such a revocation is only
enforced while it lasts (authn.authenticate reads the entry on every request), and
clearing it again would revive the sessions that were opened through the identity.

None of the functions commits; the caller owns the transaction.
"""

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.base import utcnow
from house_helper.persistence.identifiers import MAX_SUBJECT_LENGTH, normalize_identity
from house_helper.persistence.models import AllowlistEntry, EmailLoginChallenge, IdentityLink, User
from house_helper.persistence.models import Session as LoginSession

__all__ = ["MAX_SUBJECT_LENGTH", "allow", "allowed_user", "is_allowed", "normalize", "revoke"]

normalize = normalize_identity  # the (provider, subject) pair as it is stored, or ValueError


def _entry(session: OrmSession, provider: str, subject: str) -> Optional[AllowlistEntry]:
    return session.scalar(
        sa.select(AllowlistEntry).where(
            AllowlistEntry.provider == provider, AllowlistEntry.subject == subject
        )
    )


def allowed_user(
    session: OrmSession,
    provider: object,
    subject: object,
    *,
    lock: bool = False,
    exclusive: bool = False,
) -> Optional[User]:
    """The user this identity may sign in as, or None.

    None unless there is an entry for exactly this (provider, subject), it is not
    revoked, and its user is not disabled.

    lock=True is for the callers that create something on the strength of the
    answer: a login, and the issuing of an emailed sign-in link. On PostgreSQL it
    takes a shared row lock on the entry until the end of the transaction (FOR
    SHARE), which makes a concurrent revoke() wait for the login to commit, and then
    end the session it created; or, if the revoke came first, makes the login see
    the revocation and be refused. Without it a login could read "allowed", the
    revocation could commit, and the login could then commit a live session that
    nobody revokes. SQLite has one writer at a time and ignores the clause
    (SQLAlchemy leaves it out): the write that a login starts with already
    serializes it against revoke().

    exclusive=True (with lock=True) takes the lock FOR UPDATE instead: for the one
    caller that must also be the only one deciding something about the identity at
    a time, issuing a link (the cooldown between two of them is a decision that two
    concurrent requests would otherwise both make).
    """
    try:
        provider, subject = normalize(provider, subject)
    except ValueError:
        return None
    return session.scalar(_allowed_user_query(provider, subject, lock, exclusive))


def _allowed_user_query(
    provider: str, subject: str, lock: bool, exclusive: bool = False
) -> sa.Select:
    query = (
        sa.select(User)
        .join(AllowlistEntry, AllowlistEntry.user_id == User.id)
        .where(
            AllowlistEntry.provider == provider,
            AllowlistEntry.subject == subject,
            AllowlistEntry.revoked_at.is_(None),
            User.disabled_at.is_(None),
        )
    )
    if lock:
        # Only the entry: it is what revoke() changes. (OF, so the user's row is
        # not locked too.)
        query = query.with_for_update(read=not exclusive, of=AllowlistEntry)
    return query


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

    This is also how an email entry that migration 0004 revoked is re-verified: it
    was stored lower-cased by the code before, so its spelling may not be the mailbox
    that was meant, and nothing but an administrator naming the address as it really
    is can say so. The same spelling lifts the revocation on that row (the sessions
    and links revoked with it stay revoked); another spelling is a different identity
    and a new entry, and the old one stays revoked.
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
    """Withdraw an identity's access, end the sessions it opened and withdraw the
    sign-in links emailed for it (ValueError if it is not a valid identity).

    True if an active entry was revoked; False if there was none or it was
    already revoked (the first revocation time is kept, on the entry and on every
    session). The rows stay.

    Three UPDATEs in the caller's transaction, so all happen or none:

      1. the entry: revoked_at is set where it is still NULL, in one statement (no
         read first, so the answer is the number of rows it changed, and two
         concurrent revocations cannot both say True). It is also the first
         statement, which makes this a write-first transaction (authn explains why
         that matters on SQLite) and takes the row lock that login's
         allowed_user(lock=True) waits on, on PostgreSQL.
      2. the sessions: those opened through this identity's link and not yet
         revoked get the same revoked_at. Sessions of the user's other identities
         are not touched. This runs even when the entry was already revoked, so
         the post-condition "no live session came through this identity" holds
         whatever route led to the revocation (it is a no-op in the normal case).
      3. the emailed sign-in links not yet used: those issued for this identity
         (email only) and neither used nor withdrawn get the same revoked_at. A
         link that was sent but not opened is a sign-in that has not happened yet,
         and it must not come back to life when the address is allowed again, any
         more than a revoked session does. It runs after step 1, so that it also
         sees a link that a concurrent request was issuing while the entry was
         locked (issuing takes the entry's row lock first: see email_login).

    Idempotent: a second call changes nothing. Re-allowing the identity later
    (allow) does not undo steps 2 and 3.

    The caller's ORM objects for the entry are kept in step; sessions and
    challenges are changed with a Core UPDATE, so objects already loaded in the
    caller's transaction are stale until expired (the same as authn's other
    UPDATEs).
    """
    provider, subject = normalize(provider, subject)
    at = now if now is not None else utcnow()
    revoked = session.execute(
        sa.update(AllowlistEntry)
        .where(
            AllowlistEntry.provider == provider,
            AllowlistEntry.subject == subject,
            AllowlistEntry.revoked_at.is_(None),
        )
        .values(revoked_at=at)
    )
    session.execute(
        sa.update(LoginSession)
        .where(
            LoginSession.revoked_at.is_(None),
            LoginSession.identity_link_id.in_(
                sa.select(IdentityLink.id).where(
                    IdentityLink.provider == provider, IdentityLink.subject == subject
                )
            ),
        )
        .values(revoked_at=at)
        .execution_options(synchronize_session=False)
    )
    session.execute(
        sa.update(EmailLoginChallenge)
        .where(
            EmailLoginChallenge.provider == provider,
            EmailLoginChallenge.subject == subject,
            EmailLoginChallenge.revoked_at.is_(None),
            EmailLoginChallenge.consumed_at.is_(None),
        )
        .values(revoked_at=at)
        .execution_options(synchronize_session=False)
    )
    return revoked.rowcount == 1
