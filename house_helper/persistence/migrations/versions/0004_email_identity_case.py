"""email identities keep the case of their local part

Until this revision the application stored an email identity lower-cased, whole:
"Victim@example.com" became "victim@example.com", and the CHECK
email_subject_normalized (allowlist_entries and identity_links) said the same, so
approving one address approved the other. They can be two mailboxes (RFC 5321 leaves
the local part to the receiving host), so an administrator who approved one could hand
a different mailbox access. From this revision only the DOMAIN is lower-cased and the
local part is stored exactly as approved; the CHECK is relaxed to say so (one "@" with
something on both sides, domain lower-case, local part free).

Legacy rows (policy: fail closed). An email entry that exists when this revision runs
was stored lower-cased, and nothing in the database says what the administrator
meant: the stored spelling may not be the mailbox. It is NOT assumed to be. Every
email allowlist entry that is not revoked yet is revoked now (revoked_at is set to
the time of the migration), which is the existing way to say "not allowed": no link is
issued for it (allowlist.allowed_user), no link already issued for it can be redeemed,
and no session opened through it authenticates. Because that is a revocation, the same
three things that allowlist.revoke does are done here, in this transaction:

  1. the entry is revoked;
  2. the sessions opened through the identity links of email entries and not yet
     revoked are revoked;
  3. the emailed sign-in links for email entries that are neither used nor withdrawn
     are withdrawn.

Re-verification is an administrator's act, and the only way back in: allowlist.allow
with the address as the mailbox really is. If that is exactly the stored spelling, the
revocation is lifted on the existing row (the revoked sessions and links are NOT
revived: a person signs in again and gets a new session). If it is another spelling
("Victim@example.com" for a stored "victim@example.com") it is a NEW entry, a different
identity; the stored one stays revoked, and an identity is never moved from one user to
another by asking again (allow refuses it). Nothing is renamed, merged or deleted.
Whether a revoked email entry is one of these or was revoked by an administrator
earlier is not recorded; the first revocation time is kept either way (rows already
revoked are not touched).

Not touched: Telegram entries, links and sessions (nobody who signed in with Telegram
is signed out), users, identity links (kept as rows, with the sessions' provenance),
conversations, messages, turn requests, notes.

Schema. The CHECK is replaced on both tables by one that accepts every row that the
old one accepted (a lower-case address has a lower-case domain) and more, so no
existing row can violate it. PostgreSQL: DROP and ADD CONSTRAINT. SQLite cannot change
a CHECK in place, and the usual way around it, alembic's batch rebuild (create a new
table, copy, drop the old one), is not safe here: allowlist_entries and identity_links
are the parents of ON DELETE CASCADE foreign keys (identity_links, sessions,
email_login_challenges), foreign keys cannot be switched off inside the migration's
transaction (see persistence.database), and DROP TABLE of a parent would delete its
children. So the text of the two CREATE TABLE statements in sqlite_master is edited in
place, which is the procedure that SQLite's documentation gives for a change that only
loosens a constraint and touches no row (https://sqlite.org/lang_altertable.html,
"Making Other Kinds Of Table Schema Changes"): writable_schema on, UPDATE the text,
raise schema_version, writable_schema off, integrity_check. The edit is exact: the old
clause must be found once in each table, or the migration stops, and what is stored
afterwards is read back and compared before the transaction commits. A build of SQLite
that refuses writable_schema (a "defensive" build) makes the migration fail and roll
back; nothing is half applied.

Downgrade puts the old CHECK back and does not undo the revocations (a revocation is
not undone). It refuses, naming how many, if an email identity with an upper-case
letter in its local part exists, because the old CHECK cannot hold it; remove those
entries (and what depends on them) first. Offline (--sql) PostgreSQL output has no
such check, and the database refuses the ADD CONSTRAINT itself.

Written out in full rather than imported from the models (see 0001). Constraint names
are spelled out (op.f).

Revision ID: 0004_email_identity_case
Revises: 0003_email_login_challenges
Create Date: 2026-10-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004_email_identity_case"
down_revision: Union[str, None] = "0003_email_login_challenges"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("allowlist_entries", "identity_links")

# The rule as 0001 wrote it: the whole address lower-case.
_OLD_RULE = (
    "provider <> 'email' OR (subject = lower(subject) "
    "AND length(subject) - length(replace(subject, '@', '')) = 1 "
    "AND substr(subject, 1, 1) <> '@' "
    "AND substr(subject, length(subject), 1) <> '@')"
)


def _new_rule(position_function: str) -> str:
    """The rule from this revision on: everything from the "@" on is lower-case, the
    local part is free. instr on SQLite, strpos on PostgreSQL (sqlchecks.py in the
    application says the same)."""
    domain = f"substr(subject, {position_function}(subject, '@'))"
    return (
        f"provider <> 'email' OR ({domain} = lower({domain}) "
        "AND length(subject) - length(replace(subject, '@', '')) = 1 "
        "AND substr(subject, 1, 1) <> '@' "
        "AND substr(subject, length(subject), 1) <> '@')"
    )


def _is_sqlite() -> bool:
    return op.get_context().dialect.name == "sqlite"


def _name(table: str) -> str:
    return op.f(f"ck_{table}_email_subject_normalized")


def _now() -> sa.ColumnElement:
    """The moment of the migration, in the database's own clock, spelled the way
    the application stores a timestamp: UTC; on SQLite the canonical text
    "YYYY-MM-DD HH:MM:SS.ffffff" (UtcDateTime), on PostgreSQL a timestamptz."""
    if _is_sqlite():
        return sa.literal_column("strftime('%Y-%m-%d %H:%M:%f000', 'now')")
    return sa.literal_column("now()")


# --- the schema: the CHECK, per dialect ------------------------------------------------


def _replace_check_sqlite(old_rule: str, new_rule: str) -> None:
    """Edit the CHECK clause in the stored CREATE TABLE text of both tables (see the
    module docstring for why, and for the procedure). `old_rule` must be there exactly
    once in each; what is stored afterwards is read back."""
    if op.get_context().as_sql:
        raise RuntimeError("this revision edits a SQLite database in place; it has no offline SQL")
    connection = op.get_bind()
    clause = "CHECK ({})"
    texts = dict(
        connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)", _TABLES
        ).all()
    )
    rewritten = {}
    for table in _TABLES:
        sql = texts.get(table)
        if sql is None or sql.count(clause.format(old_rule)) != 1:
            raise RuntimeError(
                f"{table}: the stored definition does not hold the expected CHECK "
                "email_subject_normalized exactly once; this database does not match revision 0003"
            )
        rewritten[table] = sql.replace(clause.format(old_rule), clause.format(new_rule))

    version = connection.exec_driver_sql("PRAGMA schema_version").scalar()
    connection.exec_driver_sql("PRAGMA writable_schema = ON")
    try:
        for table, sql in rewritten.items():
            connection.exec_driver_sql(
                "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?", (sql, table)
            )
        connection.exec_driver_sql(f"PRAGMA schema_version = {int(version) + 1}")
    finally:
        connection.exec_driver_sql("PRAGMA writable_schema = OFF")

    stored = dict(
        connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)", _TABLES
        ).all()
    )
    if stored != rewritten:  # e.g. a build of SQLite that ignores writable_schema
        raise RuntimeError("SQLite did not accept the change to the stored table definitions")
    if connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() != ["ok"]:
        raise RuntimeError("the database failed its integrity check after the change")


def _replace_check_postgresql(new_rule: str) -> None:
    for table in _TABLES:
        op.drop_constraint(_name(table), table, type_="check")
        op.create_check_constraint(_name(table), table, new_rule)


def _replace_check(old_rule: str, new_rule_sqlite: str, new_rule_postgresql: str, *, to_new: bool) -> None:
    if _is_sqlite():
        if to_new:
            _replace_check_sqlite(old_rule, new_rule_sqlite)
        else:
            _replace_check_sqlite(new_rule_sqlite, old_rule)
    else:
        _replace_check_postgresql(new_rule_postgresql if to_new else old_rule)


# --- the data: fail closed -------------------------------------------------------------


def _revoke_legacy_email_identities() -> None:
    """The same three UPDATEs as allowlist.revoke, for every email entry at once
    (see the module docstring). Rows that are already revoked or used keep what they
    have."""
    at = _now()
    entries = sa.table(
        "allowlist_entries", sa.column("provider"), sa.column("revoked_at", sa.DateTime(timezone=True))
    )
    links = sa.table("identity_links", sa.column("id", sa.Uuid()), sa.column("provider"))
    sessions = sa.table(
        "sessions",
        sa.column("identity_link_id", sa.Uuid()),
        sa.column("revoked_at", sa.DateTime(timezone=True)),
    )
    challenges = sa.table(
        "email_login_challenges",
        sa.column("provider"),
        sa.column("consumed_at", sa.DateTime(timezone=True)),
        sa.column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        entries.update()
        .where(entries.c.provider == "email", entries.c.revoked_at.is_(None))
        .values(revoked_at=at)
    )
    op.execute(
        sessions.update()
        .where(
            sessions.c.revoked_at.is_(None),
            sessions.c.identity_link_id.in_(sa.select(links.c.id).where(links.c.provider == "email")),
        )
        .values(revoked_at=at)
    )
    op.execute(
        challenges.update()
        .where(
            challenges.c.provider == "email",
            challenges.c.consumed_at.is_(None),
            challenges.c.revoked_at.is_(None),
        )
        .values(revoked_at=at)
    )


def upgrade() -> None:
    # The data first, so that no email identity is live for a moment under the new
    # rule; both steps are one transaction either way.
    _revoke_legacy_email_identities()
    _replace_check(_OLD_RULE, _new_rule("instr"), _new_rule("strpos"), to_new=True)


def downgrade() -> None:
    if not op.get_context().as_sql:  # offline, there is nothing to count; PostgreSQL then refuses itself
        connection = op.get_bind()
        for table in _TABLES:
            held = connection.exec_driver_sql(
                f"SELECT count(*) FROM {table} WHERE provider = 'email' AND subject <> lower(subject)"
            ).scalar()
            if held:
                raise RuntimeError(
                    f"cannot downgrade: {table} holds {held} email address(es) with an upper-case "
                    "letter, which the old CHECK cannot hold; remove those entries first"
                )
    _replace_check(_OLD_RULE, _new_rule("instr"), _new_rule("strpos"), to_new=False)
