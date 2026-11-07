"""telegram active conversations and the update admission ledger

Two new tables, both empty; nothing that exists is touched.

1. telegram_active_conversations -- the conversation a person's Telegram chat is explicitly
   working in. One row per user (user_id is its primary key):

     conversation_id  the selected conversation of that user, or NULL when the selected one
                      was deleted. A foreign key to conversations with ON DELETE SET NULL:
                      deleting the conversation leaves the row as the explicit "it is gone"
                      state the bot acts on (a new conversation is made on the next
                      message), not a silent return to the legacy "latest conversation";
     selected_at      when the selection last changed (informational, not an activity time);
     user_id          a foreign key to users with ON DELETE CASCADE.

   No row at all means the legacy behaviour: a Telegram message follows the user's most
   recently active conversation.

2. telegram_update_admissions -- the durable record that a Telegram update of this bot was
   admitted, independent of every user and conversation. (bot_id, update_id) is the primary
   key: the update id is Telegram's own, unique for one bot, and bot_id is the numeric part
   of that bot's token. There is deliberately NO foreign key to users, conversations, turn
   requests or Telegram links: deleting any of those must not erase the fact that an update
   was admitted (that deletion was the hole this table closes).

     kind          'update' for a marker, 'cutover' for the bot's one reserved row;
     admitted_at   when the update was admitted (server time);
     expires_at    admitted_at + 48 hours for a marker, NULL for the cutover row;
     shape_valid   enforces the two shapes: a marker has a positive update id and a
                   deadline after admitted_at; the cutover row has update id 0 (reserved:
                   Telegram's ids are positive) and no deadline;
     bot_id <> 0.

   The 48-hour window is Telegram's documented 24-hour update retention plus a day of
   processing margin. The retention command deletes a marker 48 hours after admitted_at,
   whatever its stored deadline says (admin.retention); the cutover row is kept. Updates
   dated before the cutover instant are pre-migration updates and are refused when no
   surviving turn request or marker proves what became of them. Both rows are written by
   the application, never by this migration.

Nothing that exists is touched. No column, constraint, index or row of users, conversations,
messages, turn requests, favorites, Telegram links or any other table changes, and the two
new foreign keys (both on telegram_active_conversations) do not alter the tables they point
at (users.id and conversations.id are primary keys). The ledger has no foreign key at all. Downgrade drops both tables, with the
selections and the markers in them; the conversations themselves are untouched. Take a
backup first and run it with the web process stopped, like any migration.

Why not a composite (user_id, conversation_id) foreign key for the cursor: it would tie the
selection to its owner in the schema, but its ON DELETE SET NULL would null both key columns,
and user_id is the row's key and cannot be NULL. Ownership is enforced by persistence
(telegram.select_conversation and telegram.conversation_for_message find the conversation
through the user), so the simple key is the shape the deletion rule needs.

Why the ledger is one table rather than two: the cutover instant is the reserved row
(kind = 'cutover', update_id = 0), not a second singleton table. It gives one primary key,
one expiry index and one retention decision.

Written out in full rather than imported from the models (see 0001). Constraint and index
names are spelled out (op.f).

Revision ID: 0011_telegram_active_selection
Revises: 0010_structured_recipe_favorites
Create Date: 2026-11-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
# The identifier is at most 32 characters: alembic_version.version_num is VARCHAR(32),
# and a longer id fails the upgrade on PostgreSQL with SQLSTATE 22001.
revision: str = "0011_telegram_active_selection"
down_revision: Union[str, None] = "0010_structured_recipe_favorites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamp() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _telegram_id() -> sa.BigInteger:
    """BIGINT on PostgreSQL; SQLite stores every integer as 64 bits anyway."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "telegram_active_conversations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("selected_at", _timestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_telegram_active_conversations_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_telegram_active_conversations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_telegram_active_conversations")),
    )
    op.create_index(
        op.f("ix_telegram_active_conversations_conversation_id"),
        "telegram_active_conversations",
        ["conversation_id"],
    )
    op.create_table(
        "telegram_update_admissions",
        sa.Column("bot_id", _telegram_id(), nullable=False),
        sa.Column("update_id", _telegram_id(), nullable=False),
        sa.Column("kind", sa.String(length=16), server_default=sa.text("'update'"), nullable=False),
        sa.Column("admitted_at", _timestamp(), nullable=False),
        sa.Column("expires_at", _timestamp(), nullable=True),
        sa.CheckConstraint(
            "bot_id > 0", name=op.f("ck_telegram_update_admissions_bot_id_positive")
        ),
        sa.CheckConstraint(
            "kind IN ('update', 'cutover')",
            name=op.f("ck_telegram_update_admissions_kind_valid"),
        ),
        sa.CheckConstraint(
            "(kind = 'update' AND update_id > 0 AND expires_at IS NOT NULL "
            "AND expires_at > admitted_at) OR "
            "(kind = 'cutover' AND update_id = 0 AND expires_at IS NULL)",
            name=op.f("ck_telegram_update_admissions_shape_valid"),
        ),
        sa.PrimaryKeyConstraint(
            "bot_id", "update_id", name=op.f("pk_telegram_update_admissions")
        ),
    )
    op.create_index(
        op.f("ix_telegram_update_admissions_expires_at"),
        "telegram_update_admissions",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_telegram_update_admissions_expires_at"),
        table_name="telegram_update_admissions",
    )
    op.drop_table("telegram_update_admissions")
    op.drop_index(
        op.f("ix_telegram_active_conversations_conversation_id"),
        table_name="telegram_active_conversations",
    )
    op.drop_table("telegram_active_conversations")
