"""telegram links and pairing codes

Two new tables, for talking to the assistant from the Telegram bot as the same person who
uses the website.

telegram_links ties a Telegram account (the numeric id the Bot API shows the bot, which in
a private chat is also the chat's id) to one user: one account per user and one user per
account (both UNIQUE), positive ids only (a group's id is negative), a foreign key to users
(ON DELETE CASCADE) and an optional display name of at most 100 characters. It is NOT the
Telegram sign-in identity and is never derived from it: the OIDC `sub` and the Bot API id
are different values. A link is made by presenting a pairing code.

telegram_link_codes holds those codes: the SHA-256 hex digest of a secret that a signed-in
person asked for on the website (never the secret itself), who asked, when it was made and
when it stops being valid (ten minutes), and when it was used. A used code stays until its
own deadline, like the other single-use secrets, and is deleted with them by the expiry job.

Nothing that exists is touched. Users, allowlist entries, identity links, sessions,
conversations, messages, turn requests and favorites are unchanged, and so is every
signed-in browser. Both tables start empty, so the upgrade alone changes no behaviour:
the code that uses them ships with the revision that needs them (an older web process
simply never touches the tables).

Downgrade drops both tables, with the links in them. The people it concerns are unlinked
and have to pair their Telegram account again; nothing else is lost. Take a backup first
and run it with the web process stopped, like any migration.

Written out in full rather than imported from the models (see 0001). Constraint and
index names are spelled out (op.f).

Revision ID: 0008_telegram_links
Revises: 0007_favorite_recipes
Create Date: 2026-10-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0008_telegram_links"
down_revision: Union[str, None] = "0007_favorite_recipes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamp() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _telegram_id() -> sa.types.TypeEngine:
    # BIGINT on PostgreSQL (an id is already past 2**31), INTEGER on SQLite, which stores
    # up to eight bytes in an INTEGER column.
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "telegram_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", _telegram_id(), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.CheckConstraint(
            "length(display_name) <= 100", name=op.f("ck_telegram_links_display_name_max_length")
        ),
        sa.CheckConstraint(
            "telegram_user_id > 0", name=op.f("ck_telegram_links_telegram_user_id_positive")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_telegram_links_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_links")),
        sa.UniqueConstraint("telegram_user_id", name=op.f("uq_telegram_links_telegram_user_id")),
        sa.UniqueConstraint("user_id", name=op.f("uq_telegram_links_user_id")),
    )
    op.create_table(
        "telegram_link_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("expires_at", _timestamp(), nullable=False),
        sa.Column("consumed_at", _timestamp(), nullable=True),
        sa.CheckConstraint(
            "length(code_hash) = 64", name=op.f("ck_telegram_link_codes_code_hash_length")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_telegram_link_codes_expires_after_created")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_telegram_link_codes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_link_codes")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_telegram_link_codes_code_hash")),
    )
    op.create_index(
        op.f("ix_telegram_link_codes_user_id_created_at"),
        "telegram_link_codes",
        ["user_id", "created_at"],
    )
    op.create_index(
        op.f("ix_telegram_link_codes_expires_at"), "telegram_link_codes", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("telegram_link_codes")
    op.drop_table("telegram_links")
