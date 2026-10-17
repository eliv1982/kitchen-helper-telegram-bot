"""session provenance

A login session now records the identity link it was opened through
(sessions.identity_link_id), tied by a composite foreign key to a link of the
session's own user. Authentication then asks the allowlist about that one
identity, and revoking an identity can end exactly the sessions it opened.

Legacy sessions (policy: fail closed). A session created before this revision
carries no record of the identity it was opened through, and nothing else in the
database says which one it was: the user may have more than one link, and a link
belongs to a user, not to a session. Provenance is therefore NOT inferred, not
even when the user has a single link. Every session that exists when this
revision runs is revoked (revoked_at is set to the time of the migration where it
was not set already) and keeps a NULL identity_link_id. Their tokens stop working
at once and can never work again: a CHECK (provenance_or_revoked) allows a session
without provenance only while it is revoked. The effect on people: everyone who is
signed in when the migration runs is signed out and signs in again once, through
the normal login, which creates a session with provenance. Nothing else is touched:
users, allowlist entries, identity links, conversations, messages and turn requests
are unchanged.

The data step runs before the schema step, so that the CHECK is added to rows that
already satisfy it (on SQLite the table is rebuilt and the rows copied into the
new definition).

SQLite cannot add a foreign key or a CHECK to an existing table in place, so
sessions is rebuilt there (alembic batch mode, see env.py); PostgreSQL gets plain
ALTER statements. Two choices keep the rebuild harmless:

  * Only sessions is rebuilt. Foreign keys cannot be switched off inside the
    migration's transaction (see persistence.database), so dropping the old table
    would fire ON DELETE CASCADE for any table that refers to it; nothing refers to
    sessions, and the table is rebuilt once, here. identity_links, which sessions
    will refer to, is NOT rebuilt: its new key (user_id, id), the target of the
    composite foreign key, is a unique index, which SQLite and PostgreSQL both accept
    as a foreign key target and which is added without touching the table.
  * The rebuilt table is checked against the models' (tests/test_persistence_
    migrations.py compares every column, key, index and CHECK). One thing is not
    defined: alembic copies the CHECK constraints in the order of a Python set, so
    the order of the CHECK clauses inside the rebuilt sessions table can differ
    between runs. It matters only for which of several failing CHECKs SQLite names
    first in an error message.

Downgrade removes the column, the foreign key, the CHECK and the index. It does
not bring the revoked sessions back: a revocation is not undone.

Written out in full rather than imported from the models (see 0001). Constraint
and index names are spelled out (op.f).

Revision ID: 0002_session_provenance
Revises: 0001_persistence_foundation
Create Date: 2026-10-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_session_provenance"
down_revision: Union[str, None] = "0001_persistence_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _now() -> sa.ColumnElement:
    """The moment of the migration, in the database's own clock, spelled the way
    the application stores a timestamp: UTC; on SQLite the canonical text
    "YYYY-MM-DD HH:MM:SS.ffffff" (UtcDateTime), on PostgreSQL a timestamptz."""
    if op.get_context().dialect.name == "sqlite":
        return sa.literal_column("strftime('%Y-%m-%d %H:%M:%f000', 'now')")
    return sa.literal_column("now()")


def upgrade() -> None:
    # 1. Fail closed: no existing session can be shown to have come from any
    #    identity, so none stays live.
    sessions = sa.table("sessions", sa.column("revoked_at", sa.DateTime(timezone=True)))
    op.execute(
        sessions.update().where(sessions.c.revoked_at.is_(None)).values(revoked_at=_now())
    )

    # 2. The target of the composite foreign key (see the docstring: an index, so
    #    that identity_links is not rebuilt).
    op.create_index(
        op.f("uq_identity_links_user_id_id"), "identity_links", ["user_id", "id"], unique=True
    )

    # 3. The provenance column, its key, its index and the rule that makes a session
    #    without provenance harmless.
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("identity_link_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_sessions_identity_link"),
            "identity_links",
            ["user_id", "identity_link_id"],
            ["user_id", "id"],
            ondelete="CASCADE",
        )
        batch.create_index(op.f("ix_sessions_identity_link_id"), ["identity_link_id"])
        batch.create_check_constraint(
            op.f("ck_sessions_provenance_or_revoked"),
            "identity_link_id IS NOT NULL OR revoked_at IS NOT NULL",
        )


def downgrade() -> None:
    # The reverse order: sessions first, so that nothing refers to the unique
    # index when it goes.
    with op.batch_alter_table("sessions") as batch:
        batch.drop_constraint(op.f("ck_sessions_provenance_or_revoked"), type_="check")
        batch.drop_index(op.f("ix_sessions_identity_link_id"))
        batch.drop_constraint(op.f("fk_sessions_identity_link"), type_="foreignkey")
        batch.drop_column("identity_link_id")

    op.drop_index(op.f("uq_identity_links_user_id_id"), table_name="identity_links")
