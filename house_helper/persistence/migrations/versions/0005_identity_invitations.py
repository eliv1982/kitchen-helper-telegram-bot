"""identity invitations

One new table, identity_invitations: the one-time invitations with which a user that
an operator provisioned links their Telegram identity. Each row belongs to one user
(a foreign key, ON DELETE CASCADE); the secret is stored only as its SHA-256 digest; a
row is single use (consumed_at), short-lived (expires_at) and withdrawable
(revoked_at), and a CHECK keeps the last two apart, so a withdrawn invitation can never
be used up. A partial unique index allows at most one outstanding invitation (neither
consumed nor revoked) per user: issuing a new one revokes the old, and the database
refuses two live ones at once.

Nothing that exists is touched. Users, allowlist entries, identity links, sessions,
email challenges, conversations, messages and turn requests are unchanged, and so is
every signed-in browser: no session is revoked or rewritten. The table starts empty, so
no invitation exists until an operator issues one (python -m house_helper.admin
enroll-user); the upgrade alone changes no behaviour. No constraint on the number of
users is added: the two-user enrollment limit is a policy of the operator command, kept
out of the schema on purpose.

The partial index uses the same predicate on both databases (sqlite_where and
postgresql_where); SQLite and PostgreSQL both support them and neither needs a table
rebuild here, since the table is new.

Downgrade drops the table, with the invitations in it (an outstanding invitation stops
working; nothing else is affected). Run it with the web process stopped, like any
migration.

Written out in full rather than imported from the models (see 0001). Constraint and
index names are spelled out (op.f).

Revision ID: 0005_identity_invitations
Revises: 0004_email_identity_case
Create Date: 2026-10-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0005_identity_invitations"
down_revision: Union[str, None] = "0004_email_identity_case"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamp() -> sa.DateTime:
    return sa.DateTime(timezone=True)


_OUTSTANDING = "consumed_at IS NULL AND revoked_at IS NULL"


def upgrade() -> None:
    op.create_table(
        "identity_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("expires_at", _timestamp(), nullable=False),
        sa.Column("consumed_at", _timestamp(), nullable=True),
        sa.Column("revoked_at", _timestamp(), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_identity_invitations_expires_after_created")
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR revoked_at IS NULL",
            name=op.f("ck_identity_invitations_not_consumed_and_revoked"),
        ),
        sa.CheckConstraint(
            "provider = 'telegram'", name=op.f("ck_identity_invitations_provider_is_telegram")
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64", name=op.f("ck_identity_invitations_token_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_identity_invitations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_identity_invitations_token_hash")),
    )
    op.create_index(
        op.f("ix_identity_invitations_user_id_created_at"),
        "identity_invitations",
        ["user_id", "created_at"],
    )
    op.create_index(
        op.f("ix_identity_invitations_expires_at"), "identity_invitations", ["expires_at"]
    )
    op.create_index(
        op.f("uq_identity_invitations_one_outstanding"),
        "identity_invitations",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text(_OUTSTANDING),
        postgresql_where=sa.text(_OUTSTANDING),
    )


def downgrade() -> None:
    op.drop_table("identity_invitations")
