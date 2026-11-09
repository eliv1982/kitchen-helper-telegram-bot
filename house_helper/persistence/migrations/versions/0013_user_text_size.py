"""profile text size

One column is added to users, holding the size the signed-in web application draws its
text at:

    text_size  'normal' (100%, the default) or 'large' (120% of the browser's size).
               NOT NULL, and bounded by two CHECKs: the column is at most 16 characters
               and holds one of those two values.

Nothing else is created, altered or dropped, and no row is rewritten: every existing
user gets 'normal' from the column's default. The display name the profile edits already
exists (users.display_name, 200 characters); this revision does not touch it.

SQLite cannot add a named CHECK constraint to an existing table in place, and rebuilding
users is not an option: users is the root of the ownership foreign keys, and dropping
the old table inside the migration's transaction would fire ON DELETE CASCADE for every
table that refers to it (0002 explains the same constraint for a table with one referrer;
here every user-owned table would be emptied). SQLite does let a column be added with its
own named CHECK constraints, without rebuilding the table, so that is the SQLite step;
PostgreSQL gets the column and the two constraints as ordinary ALTER TABLE statements.

Downgrade drops the column (and, with it, its two constraints), restoring the 0012
schema exactly. Every other column and row of users, and every other table, stays as it
was. A person's choice is not data the older schema can express, so it is lost; nothing
else is.

Written out in full rather than imported from the models (see 0001). Constraint names are
spelled out (op.f).

Revision ID: 0013_user_text_size
Revises: 0012_shopping_lists
Create Date: 2026-11-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0013_user_text_size"
down_revision: Union[str, None] = "0012_shopping_lists"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_context().dialect.name == "sqlite":
        # One statement: the column, its default, and its two named rules. No rebuild,
        # so nothing that refers to users is cascaded away (see the module docstring).
        op.execute(
            "ALTER TABLE users ADD COLUMN text_size VARCHAR(16) NOT NULL DEFAULT 'normal' "
            "CONSTRAINT ck_users_text_size_max_length CHECK (length(text_size) <= 16) "
            "CONSTRAINT ck_users_text_size_valid CHECK (text_size IN ('normal', 'large'))"
        )
    else:
        op.add_column(
            "users",
            sa.Column(
                "text_size",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'normal'"),
            ),
        )
        op.create_check_constraint(
            op.f("ck_users_text_size_max_length"), "users", "length(text_size) <= 16"
        )
        op.create_check_constraint(
            op.f("ck_users_text_size_valid"), "users", "text_size IN ('normal', 'large')"
        )


def downgrade() -> None:
    op.drop_column("users", "text_size")
