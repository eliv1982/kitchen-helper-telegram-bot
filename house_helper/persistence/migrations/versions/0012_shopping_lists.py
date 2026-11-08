"""persistent shopping checklists

Three new tables, all empty; nothing that exists is touched.

1. shopping_lists -- one saved shopping checklist of one user. A list is a complete
   snapshot of a recipe's ingredients, including the ingredients the person already has
   at home, and it is independent of everything it was copied from:

     title                   the source recipe's title (1 to 200 characters);
     status                  'active' while the list is worked on, 'completed' when it
                             is finished. A completed list is kept (it may be reopened by
                             moving it back to 'active'); its items are read-only until
                             then;
     source_kind             'message' (one validated recipe of an assistant answer) or
                             'favorite' (a structured favorite);
     source_message_id       the message the recipe came from, a plain number, NOT a
     source_recipe_index     foreign key; the recipe's zero-based position in that
                             answer. Both are NULL for a favorite source;
     source_favorite_id      the structured favorite copied, a plain UUID, NOT a foreign
     source_favorite_version key; the version that was current when the list was made.
                             Both are NULL for a message source;
     idempotency_key         the client-chosen key of the creation request (1 to 128
                             printable ASCII characters); (user_id, idempotency_key) is
                             UNIQUE, so one key creates at most one list for one user;
     request_fingerprint     the SHA-256 hex digest of the canonical request: the same
                             key with the same request answers the list that exists, a
                             different request is a conflict, never a second list;
     created_at, updated_at  when the list was made and when it last changed.

   user_id is a foreign key to users with ON DELETE CASCADE. There is deliberately NO
   foreign key to conversations, messages, message_recipes or favorite_recipes: deleting
   or editing any of them must not change or delete a saved list. A list has no deadline
   and the retention purge does not know this table.

2. shopping_list_items -- one row per ingredient of the snapshot, in the recipe's order.

     list_id     a foreign key to shopping_lists with ON DELETE CASCADE;
     position    zero-based place in the list; (list_id, position) is UNIQUE, so the
                 order is stable and a manual item is appended at max(position) + 1;
     name        the ingredient's name (1 to 200 characters);
     amount      the ingredient's amount phrase (at most 100 characters), or NULL for a
                 manual item without an amount;
     state       'unchecked' (availability unconfirmed: the default; nothing is assumed),
                 'at_home' (confirmed available at home) or 'purchased' (bought during
                 this shopping). All three are reachable from one another while the list
                 is active; 'at_home' never deletes an item;
     created_at, updated_at  when the item was copied or added and when it last changed.

   The state is deliberately not a boolean: "unknown availability" must never be stored
   as confirmed absence, and "at home" is not "purchased".

3. shopping_list_requests -- the durable reservation of a creation request's
   idempotency key: one row per (user, key), written with the list it created and kept
   for the lifetime of the user account.

     user_id, idempotency_key  whose key it is and the client-chosen key (1 to 128
                               printable ASCII characters); (user_id, idempotency_key)
                               is UNIQUE, so one key is consumed by at most one request
                               for one user, whether or not that list still exists;
     request_fingerprint       the SHA-256 hex digest of the canonical request: it
                               tells a retry of the same request from a different one
                               even after the list is gone;
     list_id                  the list the request created, a foreign key to
                               shopping_lists with ON DELETE SET NULL. Deleting the list
                               leaves the row as a tombstone (list_id NULL): a retry of
                               the same request then answers "already deleted" instead
                               of recreating the list, and the key stays unusable for
                               another request. A new list needs a new key;
     created_at                when the key was consumed.

   The row holds nothing of the shopping list itself: no title, no ingredients, no item
   states. user_id is ON DELETE CASCADE, so deleting the user removes their keys. The
   table has an index on list_id for the SET NULL transition of a deletion.

Nothing that exists is touched. No column, constraint, index or row of users,
conversations, messages, turn requests, favorites, message recipes, Telegram tables or
any other table changes, and the new foreign keys point at primary keys (users.id,
shopping_lists.id), so the tables they point at are not altered. The tables start empty:
the upgrade alone changes no behaviour.

Deployment boundary (Stage A does not change any deployment configuration). The
confirmed production schema after the 8 November 2026 deployment is
0011_telegram_active_selection. The scheduled purge and the off-host backup currently
run from an older retained image tagged 7f811835b864, and the deployed schema guard
(admin.target.check_schema_at_head) requires exactly the repository's head. Upgrading to
this revision therefore requires the image and the schema to move together, and the
scheduled jobs' image pins must be coordinated with them:

  * stop the cleanup timer before the upgrade and start it again with the new image
    (deploy/RUNBOOK.md, section 14.4), so a scheduled purge never runs an image that
    refuses this schema (and the backup uses the image that matches the database);
  * take and verify an off-host backup with the running image BEFORE applying this
    migration (deploy/backup.sh --tag <running image tag>);
  * apply this revision with the new image (alembic upgrade head, then check-schema);
  * start the cleanup and backup jobs with the new image tag.

An image-only rollback is NOT safe once this revision has been applied: an older image
knows none of these tables, and its schema guard refuses the database. Rolling back
means downgrading the schema to 0011 first (which DROPS all three tables and every saved
list in them; there is no way for the older schema to keep them), then running the older
image. Take a backup first, with the web service stopped, like any migration.

Downgrade drops shopping_list_requests, shopping_list_items and shopping_lists, with the
lists, items and consumed keys in them, and restores the 0011 schema exactly. Every other
table and row is untouched.

Written out in full rather than imported from the models (see 0001). Constraint and index
names are spelled out (op.f).

Revision ID: 0012_shopping_lists
Revises: 0011_telegram_active_selection
Create Date: 2026-11-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0012_shopping_lists"
down_revision: Union[str, None] = "0011_telegram_active_selection"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamp() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _message_id() -> sa.types.TypeEngine:
    # BIGINT on PostgreSQL, INTEGER on SQLite: the type of messages.id (see 0001).
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _printable_ascii(column: str) -> str:
    """Only the characters "!" to "~", at least one (see 0001). SQLite: GLOB, the BLOB
    length and typeof(); PostgreSQL: a regular expression."""
    if op.get_context().dialect.name == "sqlite":
        return (
            f"(typeof({column}) IN ('text', 'null') "
            f"AND {column} NOT GLOB '*[^!-~]*' "
            f"AND length({column}) = length(CAST({column} AS BLOB)) "
            f"AND length({column}) >= 1)"
        )
    return f"{column} ~ '^[!-~]+$'"


def upgrade() -> None:
    op.create_table(
        "shopping_lists",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_message_id", _message_id(), nullable=True),
        sa.Column("source_recipe_index", sa.Integer(), nullable=True),
        sa.Column("source_favorite_id", sa.Uuid(), nullable=True),
        sa.Column("source_favorite_version", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("updated_at", _timestamp(), nullable=False),
        sa.CheckConstraint(
            "length(title) <= 200", name=op.f("ck_shopping_lists_title_max_length")
        ),
        sa.CheckConstraint(
            "length(title) >= 1", name=op.f("ck_shopping_lists_title_not_empty")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'completed')", name=op.f("ck_shopping_lists_status_valid")
        ),
        sa.CheckConstraint(
            "source_kind IN ('message', 'favorite')",
            name=op.f("ck_shopping_lists_source_kind_valid"),
        ),
        sa.CheckConstraint(
            "(source_kind = 'message' AND source_message_id IS NOT NULL "
            "AND source_recipe_index IS NOT NULL AND source_favorite_id IS NULL "
            "AND source_favorite_version IS NULL) OR "
            "(source_kind = 'favorite' AND source_message_id IS NULL "
            "AND source_recipe_index IS NULL AND source_favorite_id IS NOT NULL "
            "AND source_favorite_version IS NOT NULL)",
            name=op.f("ck_shopping_lists_source_consistent"),
        ),
        sa.CheckConstraint(
            "source_message_id IS NULL OR source_message_id > 0",
            name=op.f("ck_shopping_lists_source_message_id_positive"),
        ),
        sa.CheckConstraint(
            "source_recipe_index IS NULL OR source_recipe_index BETWEEN 0 AND 4",
            name=op.f("ck_shopping_lists_source_recipe_index_valid"),
        ),
        sa.CheckConstraint(
            "source_favorite_version IS NULL OR source_favorite_version >= 1",
            name=op.f("ck_shopping_lists_source_favorite_version_valid"),
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name=op.f("ck_shopping_lists_idempotency_key_length"),
        ),
        sa.CheckConstraint(
            _printable_ascii("idempotency_key"),
            name=op.f("ck_shopping_lists_idempotency_key_printable_ascii"),
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name=op.f("ck_shopping_lists_fingerprint_length"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_shopping_lists_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shopping_lists")),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name=op.f("uq_shopping_lists_user_id_idempotency_key"),
        ),
    )
    op.create_index(
        op.f("ix_shopping_lists_user_id_created_at"),
        "shopping_lists",
        ["user_id", "created_at"],
    )

    op.create_table(
        "shopping_list_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("list_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("amount", sa.String(length=100), nullable=True),
        sa.Column(
            "state", sa.String(length=16), server_default=sa.text("'unchecked'"), nullable=False
        ),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("updated_at", _timestamp(), nullable=False),
        sa.CheckConstraint("position >= 0", name=op.f("ck_shopping_list_items_position_valid")),
        sa.CheckConstraint(
            "length(name) <= 200", name=op.f("ck_shopping_list_items_name_max_length")
        ),
        sa.CheckConstraint(
            "length(name) >= 1", name=op.f("ck_shopping_list_items_name_not_empty")
        ),
        sa.CheckConstraint(
            "length(amount) <= 100", name=op.f("ck_shopping_list_items_amount_max_length")
        ),
        sa.CheckConstraint(
            "state IN ('unchecked', 'at_home', 'purchased')",
            name=op.f("ck_shopping_list_items_state_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["list_id"],
            ["shopping_lists.id"],
            name=op.f("fk_shopping_list_items_list_id_shopping_lists"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shopping_list_items")),
        sa.UniqueConstraint(
            "list_id", "position", name=op.f("uq_shopping_list_items_list_id_position")
        ),
    )

    op.create_table(
        "shopping_list_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("list_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name=op.f("ck_shopping_list_requests_idempotency_key_length"),
        ),
        sa.CheckConstraint(
            _printable_ascii("idempotency_key"),
            name=op.f("ck_shopping_list_requests_idempotency_key_printable_ascii"),
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name=op.f("ck_shopping_list_requests_fingerprint_length"),
        ),
        sa.ForeignKeyConstraint(
            ["list_id"],
            ["shopping_lists.id"],
            name=op.f("fk_shopping_list_requests_list_id_shopping_lists"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_shopping_list_requests_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shopping_list_requests")),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name=op.f("uq_shopping_list_requests_user_id_idempotency_key"),
        ),
    )
    op.create_index(
        op.f("ix_shopping_list_requests_list_id"), "shopping_list_requests", ["list_id"]
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_shopping_list_requests_list_id"), table_name="shopping_list_requests"
    )
    op.drop_table("shopping_list_requests")
    op.drop_table("shopping_list_items")
    op.drop_index(op.f("ix_shopping_lists_user_id_created_at"), table_name="shopping_lists")
    op.drop_table("shopping_lists")
