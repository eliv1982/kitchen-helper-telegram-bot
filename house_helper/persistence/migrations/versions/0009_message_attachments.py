"""message attachments (photos)

One new table, message_attachments: a photo that belongs to a message of a conversation, kept
in the database beside the message so that it is in every backup and goes by the same purge.

A row is owned by a user (a foreign key, ON DELETE CASCADE) and is attached to at most one
message (message_id, a foreign key to messages, ON DELETE CASCADE, UNIQUE, so a message has at
most one picture). It is made before the message exists (message_id NULL, a short deadline: an
upload that is never sent goes away) and attached in the transaction that creates the message,
which also gives it the message's deadline. The database holds the rules a writer must not
break on its own: the kind of picture is one of three, its size is 1 byte to 5 MiB and equals
the length of the data, the digest is 64 characters, and the deadline is after the creation.

Nothing that exists is touched. Users, allowlist entries, identity links, sessions,
conversations, messages, turn requests, favorites and the Telegram links of revision 0008 are
unchanged, and so is every signed-in browser. The table starts empty, so the upgrade alone
changes no behaviour: the code that uses it ships with the revision that needs it (an older web
process simply never touches the table).

Downgrade drops the table, with every photo in it. Take a backup first and run it with the web
process stopped, like any migration. The messages that came with the photos stay; they lose the
picture.

Written out in full rather than imported from the models (see 0001). Constraint and index
names are spelled out (op.f).

Revision ID: 0009_message_attachments
Revises: 0008_telegram_links
Create Date: 2026-10-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0009_message_attachments"
down_revision: Union[str, None] = "0008_telegram_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamp() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _message_id() -> sa.types.TypeEngine:
    # BIGINT on PostgreSQL, INTEGER on SQLite: the type of messages.id (see 0001).
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "message_attachments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", _message_id(), nullable=True),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("expires_at", _timestamp(), nullable=False),
        sa.CheckConstraint(
            "content_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name=op.f("ck_message_attachments_content_type_valid"),
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 1 AND 5242880", name=op.f("ck_message_attachments_size_bytes_valid")
        ),
        sa.CheckConstraint(
            "length(data) = size_bytes", name=op.f("ck_message_attachments_data_matches_size")
        ),
        sa.CheckConstraint(
            "length(sha256) = 64", name=op.f("ck_message_attachments_sha256_length")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_message_attachments_expires_after_created")
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_attachments_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_message_attachments_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_attachments")),
        sa.UniqueConstraint("message_id", name=op.f("uq_message_attachments_message_id")),
    )
    op.create_index(
        op.f("ix_message_attachments_user_id_created_at"),
        "message_attachments",
        ["user_id", "created_at"],
    )
    op.create_index(
        op.f("ix_message_attachments_expires_at"), "message_attachments", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("message_attachments")
