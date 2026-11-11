"""personal household measure overrides

Three nullable integer columns are added to users, holding a person's own household
capacities in millilitres:

    measure_glass_ml       50..1000, or NULL (the system default of 250 applies)
    measure_teaspoon_ml     1..20,   or NULL (the system default of 5 applies)
    measure_tablespoon_ml   5..50,   or NULL (the system default of 15 applies)

Each column has its own named CHECK with the approved inclusive range. A NULL passes
(the rule is written `IS NULL OR ... BETWEEN ...`), which is what "no personal value"
means: nothing here seeds, backfills or rewrites a row, so every existing account keeps
the system defaults. The three settings are independent; no rule relates one column to
another, and no personal value can ever be read as a source-recipe capacity.

SQLite gets each column (and its named rule) with its own ALTER TABLE ADD COLUMN, so the
users table is never rebuilt: users is the root of the ownership foreign keys, and a
batch rebuild inside the migration's transaction would cascade-delete every user-owned
table (0002 and 0013 explain the same constraint; 0013 established this exact shape for
a rule attached to a new column). PostgreSQL gets ordinary ALTER TABLE statements.

Downgrade drops the three columns (and, with them, their three constraints), restoring
the 0013 schema exactly. Every other column and row of users, and every other table,
stays as it was. A person's configured capacities are not data the older schema can
express, so they are lost; nothing else is.

Written out in full rather than imported from the models (see 0001). Constraint names are
spelled out (op.f).

Revision ID: 0014_user_measure_overrides
Revises: 0013_user_text_size
Create Date: 2026-11-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0014_user_measure_overrides"
down_revision: Union[str, None] = "0013_user_text_size"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (column, minimum, maximum). The system defaults are not written here: a NULL column
# means "the default applies", and it is the application that resolves the default.
_COLUMNS = (
    ("measure_glass_ml", 50, 1000),
    ("measure_teaspoon_ml", 1, 20),
    ("measure_tablespoon_ml", 5, 50),
)


def upgrade() -> None:
    if op.get_context().dialect.name == "sqlite":
        # One statement per column: the column and its named range rule, without a
        # rebuild, so nothing that refers to users is cascaded away (see 0013).
        for column, minimum, maximum in _COLUMNS:
            op.execute(
                f"ALTER TABLE users ADD COLUMN {column} INTEGER DEFAULT NULL "
                f"CONSTRAINT ck_users_{column}_range CHECK "
                f"({column} IS NULL OR {column} BETWEEN {minimum} AND {maximum})"
            )
    else:
        for column, minimum, maximum in _COLUMNS:
            op.add_column(
                "users",
                sa.Column(column, sa.Integer(), nullable=True, server_default=sa.text("NULL")),
            )
            op.create_check_constraint(
                op.f(f"ck_users_{column}_range"),
                "users",
                f"{column} IS NULL OR {column} BETWEEN {minimum} AND {maximum}",
            )


def downgrade() -> None:
    for column, _, _ in _COLUMNS:
        op.drop_column("users", column)
