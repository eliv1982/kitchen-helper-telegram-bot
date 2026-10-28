"""favorite recipes

One new table, favorite_recipes: the answers of the assistant that a user chose to keep.
Each row belongs to one user (a foreign key, ON DELETE CASCADE) and holds a copy of the
answer's exact text and a short title, with no link to the conversation or the message it
came from: a favorite does not expire with the history (messages and turn requests are
deleted 365 days after they were written) and is not touched by the purge, which does not
know this table. source_message_id is the id of the message it was copied from, a plain
number and not a foreign key; (user_id, source_message_id) is unique, so saving one answer
twice is one favorite. Titles are 1 to 200 characters and the text at least one, as CHECKs.

Nothing that exists is touched. Users, allowlist entries, identity links, sessions, email
challenges, invitations, conversations, messages and turn requests are unchanged, and so is
every signed-in browser: no session is revoked or rewritten. The table starts empty, so the
upgrade alone changes no behaviour: the page and the API that use it ship with the code
that needs this revision (an older web process simply never touches the table).

Downgrade drops the table, with the favorites in it. That is the one destructive step of
this revision: take a backup first, and run it with the web process stopped, like any
migration.

Written out in full rather than imported from the models (see 0001). Constraint and
index names are spelled out (op.f).

Revision ID: 0007_favorite_recipes
Revises: 0006_oidc_state_invitation
Create Date: 2026-10-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0007_favorite_recipes"
down_revision: Union[str, None] = "0006_oidc_state_invitation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamp() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _message_id() -> sa.types.TypeEngine:
    # BIGINT on PostgreSQL, INTEGER on SQLite: the type of messages.id (see 0001).
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "favorite_recipes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_message_id", _message_id(), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.CheckConstraint("length(title) <= 200", name=op.f("ck_favorite_recipes_title_max_length")),
        sa.CheckConstraint("length(title) >= 1", name=op.f("ck_favorite_recipes_title_not_empty")),
        sa.CheckConstraint(
            "length(content) >= 1", name=op.f("ck_favorite_recipes_content_not_empty")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_favorite_recipes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_favorite_recipes")),
        sa.UniqueConstraint(
            "user_id", "source_message_id", name=op.f("uq_favorite_recipes_user_id_source_message_id")
        ),
    )
    op.create_index(
        op.f("ix_favorite_recipes_user_id_created_at"),
        "favorite_recipes",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("favorite_recipes")
