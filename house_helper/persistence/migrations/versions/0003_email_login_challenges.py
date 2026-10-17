"""email login challenges

One new table, email_login_challenges: the emailed sign-in links that have been
issued and not yet used up. Each row is bound, by a composite foreign key to the
allowlist, to one address that was provisioned in advance and to the very user that
address belongs to (the same rule as identity_links); the secret in the link is
stored only as its SHA-256 digest; a row is single use (consumed_at), short-lived
(expires_at) and withdrawable (revoked_at).

Nothing that exists is touched. Users, allowlist entries, identity links, sessions
(with the provenance 0002 gave them), conversations, messages and turn requests are
unchanged, and so is every signed-in browser: no session is revoked or rewritten.
The table starts empty, so no link is valid until one is requested after the
upgrade, and email login stays off until the web process is given its mail settings
(see README); the upgrade alone changes no behaviour.

Downgrade drops the table, with the links in it (an outstanding link stops working;
nothing else is affected). Run it with the web process stopped, like any migration.

Written out in full rather than imported from the models (see 0001). Constraint and
index names are spelled out (op.f).

Revision ID: 0003_email_login_challenges
Revises: 0002_session_provenance
Create Date: 2026-10-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_email_login_challenges"
down_revision: Union[str, None] = "0002_session_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamp() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "email_login_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("expires_at", _timestamp(), nullable=False),
        sa.Column("consumed_at", _timestamp(), nullable=True),
        sa.Column("revoked_at", _timestamp(), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_email_login_challenges_expires_after_created")
        ),
        sa.CheckConstraint(
            "provider = 'email'", name=op.f("ck_email_login_challenges_provider_is_email")
        ),
        sa.CheckConstraint(
            "length(subject) BETWEEN 1 AND 255", name=op.f("ck_email_login_challenges_subject_length")
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64", name=op.f("ck_email_login_challenges_token_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "provider", "subject"],
            [
                "allowlist_entries.user_id",
                "allowlist_entries.provider",
                "allowlist_entries.subject",
            ],
            name=op.f("fk_email_login_challenges_allowlist_entry"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_email_login_challenges_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_login_challenges")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_email_login_challenges_token_hash")),
    )
    op.create_index(
        op.f("ix_email_login_challenges_identity"),
        "email_login_challenges",
        ["user_id", "provider", "subject", "created_at"],
    )
    op.create_index(
        op.f("ix_email_login_challenges_expires_at"), "email_login_challenges", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("email_login_challenges")
