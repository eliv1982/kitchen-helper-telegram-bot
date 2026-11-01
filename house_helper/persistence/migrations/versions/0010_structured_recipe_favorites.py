"""structured recipe favorites

Two changes, one transaction.

1. A new table, message_recipes: the validated recipes of an assistant answer, one row per
   accepted recipe, identified by (message_id, position) -- the zero-based position among
   the recipes the protocol accepted for that answer. A row holds the protocol's title,
   servings phrase (NULL when the answer named none), ordered ingredients (JSON objects
   with name and amount) and ordered steps (JSON strings), and when it was recorded. There
   is no user column: access derives from the parent message's conversation. The parent is
   an ASSISTANT message, enforced by the database: message_role is pinned to 'assistant'
   by a CHECK and the composite foreign key (message_id, message_role) points at
   messages (id, role), whose new unique index is the target. The key is ON DELETE
   CASCADE, so the 365-day retention, the purge and the deletion of a user take the
   recipes with the message. The turn runner writes this table in the same transaction
   that stores the assistant message (persistence.turns.complete_turn calls
   message_recipes.record_validated_recipes), when structured generation is switched on;
   with it off no recipe is produced to store.

2. favorite_recipes learns structured favorites, and one answer may now yield several
   favorites (one per recipe) without disturbing the text favorites:

     kind                 'text' (every existing row) or 'structured';
     source_recipe_index  -1 for a text favorite, the recipe's position for a structured
                          one;
     servings             the protocol's servings phrase (structured only);
     ingredients, steps   the protocol's ordered lists as JSON (structured only);
     notes                the person's own note, for either kind;
     version              the optimistic-concurrency token, starting at 1;
     updated_at           when the row last changed, informational.

   The old unique rule (user_id, source_message_id) is replaced by (user_id,
   source_message_id, source_recipe_index). A text favorite is still unique per user and
   message (its index is always -1), and two different recipes of one answer are two
   favorites. The new CHECKs say what each kind may carry.

Preservation. Every existing row of favorite_recipes keeps its id, owner, title, content,
source message id and created_at, byte for byte. The new columns are added with defaults
that make every old row a text favorite: kind 'text', source_recipe_index -1, version 1;
servings, ingredients, steps and notes are NULL, exactly what the text kind requires.
updated_at is added NOT NULL with the database's default (so the rebuild has a value for
every old row) and then filled with each row's own created_at, not the time of the
migration. No text is parsed, and nothing structured is invented from it: a legacy
favorite is not turned into a recipe.

Everything else is unchanged: users, allowlist entries, identity links, sessions, login
states, invitations, conversations, messages, turn requests, Telegram links, attachment
rows, favorites' text and timestamps. The only change to an existing table besides the
favorites' columns and unique rule is one unique index on messages ((id, role)), which
adds no rule that was not already true (id alone is the primary key) and exists only as a
foreign key target. On SQLite favorite_recipes is rebuilt (batch mode, see env.py); it has
no children, so the rebuild cannot cascade into anything, and the table is checked against
the models' by the fingerprint test.

Downgrade. It refuses, changing nothing, while any structured favorite exists: the old
schema has no place for its title/servings/ingredients/steps and no room for more than one
favorite per source message. Delete the structured favorites first (the API's DELETE, or
SQL) if the rollback is really wanted. Once there are none, the way back restores the old
unique rule, drops the new columns and the table message_recipes (with every stored
recipe) and the unique index on messages. A text favorite keeps its id, owner, title, text,
source message and created_at; its personal notes, its version and its updated_at are
dropped with the columns, because the old schema has nowhere to keep them (its title and
text, including an edit made since the upgrade, stay as they are). The stored recipes are
source data that the way back cannot keep: a later upgrade will not bring them back,
because the answer they were parsed from is not re-read. Take a backup before any
downgrade, and run it with the web process stopped. Offline SQL (--sql) cannot run the
check: the operator must establish beforehand that no structured favorite exists, or the
old unique rule will refuse the rendered statement itself.

Rollback of the image, not of the schema, is not a way back. The deployed schema guard
(admin.target.check_schema_at_head) requires exactly the repository's head: an image that
knows 0009 refuses a database at 0010, and the image that knows 0010 refuses one at 0009.
Moving production back therefore requires the downgrade above (destructive for structured
data) as well as the image, and moving forward again requires the upgrade.

Feature switch. Nothing here is turned off by a flag. The switch that exists
(HOUSE_HELPER_STRUCTURED_RECIPES) changes only whether new structured recipes are
produced: with it off the backend is asked for no blocks, so the turn runner has nothing
to store. It deliberately does not disable the favorites routes: GET and the detail read
serve stored favorites of both kinds, POST with recipe_index copies a recipe that is
already stored and makes no provider call, and PATCH and DELETE keep working for a
structured favorite that already exists, because turning generation off must not strand a
stored row. The schema guard stays at this revision, text favorites keep working, and no
stored row is deleted or rewritten by being disabled.

Written out in full rather than imported from the models (see 0001). Constraint and index
names are spelled out (op.f).

Revision ID: 0010_structured_recipe_favorites
Revises: 0009_message_attachments
Create Date: 2026-11-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010_structured_recipe_favorites"
down_revision: Union[str, None] = "0009_message_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FAVORITES = "favorite_recipes"
_OLD_UNIQUE = "uq_favorite_recipes_user_id_source_message_id"
_NEW_UNIQUE = "uq_favorite_recipes_user_message_recipe"
_MESSAGE_INDEX = "uq_messages_id_role"

_FAVORITE_CHECKS = (
    "ck_favorite_recipes_kind_valid",
    "ck_favorite_recipes_source_recipe_index_valid",
    "ck_favorite_recipes_kind_consistent",
    "ck_favorite_recipes_version_valid",
    "ck_favorite_recipes_servings_max_length",
    "ck_favorite_recipes_notes_max_length",
)

_FAVORITE_NEW_COLUMNS = (
    "kind",
    "source_recipe_index",
    "servings",
    "ingredients",
    "steps",
    "notes",
    "version",
    "updated_at",
)


def _timestamp() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _message_id() -> sa.types.TypeEngine:
    # BIGINT on PostgreSQL, INTEGER on SQLite: the type of messages.id (see 0001).
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _json() -> sa.types.TypeEngine:
    # JSON on SQLite, JSONB on PostgreSQL (the same choice as turn_requests.result_payload).
    return sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(none_as_null=True), "postgresql"
    )


def upgrade() -> None:
    # The target of message_recipes' composite foreign key. id alone is already the key;
    # the pair exists so that the database can pin the role of the message.
    op.create_index(op.f(_MESSAGE_INDEX), "messages", ["id", "role"], unique=True)

    op.create_table(
        "message_recipes",
        sa.Column("message_id", _message_id(), nullable=False),
        sa.Column(
            "message_role",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'assistant'"),
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("servings", sa.String(length=100), nullable=True),
        sa.Column("ingredients", _json(), nullable=False),
        sa.Column("steps", _json(), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.CheckConstraint(
            "message_role = 'assistant'",
            name=op.f("ck_message_recipes_message_role_is_assistant"),
        ),
        sa.CheckConstraint(
            "position BETWEEN 0 AND 4", name=op.f("ck_message_recipes_position_valid")
        ),
        sa.CheckConstraint(
            "length(title) <= 200", name=op.f("ck_message_recipes_title_max_length")
        ),
        sa.CheckConstraint(
            "length(title) >= 1", name=op.f("ck_message_recipes_title_not_empty")
        ),
        sa.CheckConstraint(
            "length(servings) <= 100", name=op.f("ck_message_recipes_servings_max_length")
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "message_role"],
            ["messages.id", "messages.role"],
            name="fk_message_recipes_message",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id", "position", name=op.f("pk_message_recipes")),
    )

    # The favorites' new columns, rules and unique key. The defaults make every existing
    # row a text favorite; updated_at is nullable here so that it can be filled with each
    # row's created_at below (a column default cannot name another column).
    with op.batch_alter_table(_FAVORITES) as batch:
        batch.add_column(
            sa.Column(
                "kind", sa.String(length=16), nullable=False, server_default=sa.text("'text'")
            )
        )
        batch.add_column(
            sa.Column(
                "source_recipe_index",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("-1"),
            )
        )
        batch.add_column(sa.Column("servings", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("ingredients", _json(), nullable=True))
        batch.add_column(sa.Column("steps", _json(), nullable=True))
        batch.add_column(sa.Column("notes", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1"))
        )
        batch.add_column(
            sa.Column(
                "updated_at",
                _timestamp(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch.drop_constraint(op.f(_OLD_UNIQUE), type_="unique")
        batch.create_unique_constraint(
            op.f(_NEW_UNIQUE), ["user_id", "source_message_id", "source_recipe_index"]
        )
        batch.create_check_constraint(
            op.f("ck_favorite_recipes_kind_valid"), "kind IN ('text', 'structured')"
        )
        batch.create_check_constraint(
            op.f("ck_favorite_recipes_source_recipe_index_valid"),
            "source_recipe_index BETWEEN -1 AND 4",
        )
        batch.create_check_constraint(
            op.f("ck_favorite_recipes_kind_consistent"),
            "(kind = 'text' AND source_recipe_index = -1 AND servings IS NULL "
            "AND ingredients IS NULL AND steps IS NULL) OR "
            "(kind = 'structured' AND source_recipe_index >= 0 "
            "AND ingredients IS NOT NULL AND steps IS NOT NULL)",
        )
        batch.create_check_constraint(
            op.f("ck_favorite_recipes_version_valid"), "version >= 1"
        )
        batch.create_check_constraint(
            op.f("ck_favorite_recipes_servings_max_length"), "length(servings) <= 100"
        )
        batch.create_check_constraint(
            op.f("ck_favorite_recipes_notes_max_length"), "length(notes) <= 2000"
        )

    favorites = sa.table(
        _FAVORITES,
        sa.column("created_at", _timestamp()),
        sa.column("updated_at", _timestamp()),
    )
    op.execute(favorites.update().values(updated_at=favorites.c.created_at))


def downgrade() -> None:
    if not op.get_context().as_sql:
        structured = op.get_bind().exec_driver_sql(
            f"SELECT count(*) FROM {_FAVORITES} WHERE kind = 'structured'"
        ).scalar()
        if structured:
            raise RuntimeError(
                f"cannot downgrade: {structured} structured favorite(s) exist, and the "
                "schema before this revision can hold neither their fields nor more than "
                "one favorite per source message; delete them first"
            )

    op.drop_table("message_recipes")
    op.drop_index(op.f(_MESSAGE_INDEX), table_name="messages")

    with op.batch_alter_table(_FAVORITES) as batch:
        for name in _FAVORITE_CHECKS:
            batch.drop_constraint(op.f(name), type_="check")
        batch.drop_constraint(op.f(_NEW_UNIQUE), type_="unique")
        batch.create_unique_constraint(
            op.f(_OLD_UNIQUE), ["user_id", "source_message_id"]
        )
        for name in reversed(_FAVORITE_NEW_COLUMNS):
            batch.drop_column(name)
