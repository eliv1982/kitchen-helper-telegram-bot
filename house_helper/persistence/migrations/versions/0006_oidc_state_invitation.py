"""bind an OIDC login to the Telegram invitation that started it

A sign-in that was started by presenting a Telegram invitation has to remember, for the
round trip through Telegram, which invitation it is for. The invitation's secret is checked
when the sign-in starts and is not kept anywhere (it lives only in the URL fragment the
browser never sends), so the server's own record of the login names the invitation by its
id: oidc_states.invitation_id, a nullable foreign key to identity_invitations (ON DELETE
CASCADE: a login bound to an invitation that is deleted goes with it, and does not turn into
an ordinary login). NULL is what every login has always been, and stays: the upgrade changes
no existing row, so a login that is in flight while it runs finishes as before. A column is
all that is added, with its key and an index for the key's cascade; the allowlist, the
identity links, the sessions and the invitations are not touched, and no session is revoked.

SQLite cannot add a foreign key to an existing table in place, so oidc_states is rebuilt
there (alembic batch mode, see env.py); PostgreSQL gets plain ALTER statements. Nothing refers
to oidc_states, so dropping the old table in the rebuild fires no ON DELETE CASCADE of any
other table (foreign keys cannot be switched off inside the migration's transaction; see
0002), and the rows of the table, which hold only logins of the last ten minutes, are copied
into the new definition. The rebuilt table is checked against the models' (tests/
test_persistence_migrations.py compares every column, key, index and CHECK); as in 0002, the
order of the CHECK clauses inside a rebuilt table is not defined, and matters only for which
of several failing CHECKs SQLite names first.

Downgrade first deletes the logins that are bound to an invitation, then removes the index,
the foreign key and the column: a login that was started with an invitation must not outlive
the column as an ordinary one (the code that downgrades to has no notion of the binding).
Such a login lives ten minutes at most; the person starts again. Run it with the web process
stopped, like any migration.

Written out in full rather than imported from the models (see 0001). Constraint and index
names are spelled out (op.f).

Revision ID: 0006_oidc_state_invitation
Revises: 0005_identity_invitations
Create Date: 2026-10-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0006_oidc_state_invitation"
down_revision: Union[str, None] = "0005_identity_invitations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("oidc_states") as batch:
        batch.add_column(sa.Column("invitation_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_oidc_states_invitation_id_identity_invitations"),
            "identity_invitations",
            ["invitation_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index(op.f("ix_oidc_states_invitation_id"), ["invitation_id"])


def downgrade() -> None:
    # The data step first (see the docstring): logins bound to an invitation do not
    # survive as ordinary ones.
    oidc_states = sa.table("oidc_states", sa.column("invitation_id", sa.Uuid()))
    op.execute(oidc_states.delete().where(oidc_states.c.invitation_id.is_not(None)))

    with op.batch_alter_table("oidc_states") as batch:
        batch.drop_index(op.f("ix_oidc_states_invitation_id"))
        batch.drop_constraint(
            op.f("fk_oidc_states_invitation_id_identity_invitations"), type_="foreignkey"
        )
        batch.drop_column("invitation_id")
