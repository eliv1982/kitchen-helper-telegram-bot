"""The migration chain on a real PostgreSQL 16 server (HH-DB-01 CI-09, CI-10, CI-12, CI-16).

What the SQLite and offline tests cannot show: that the whole history executes against
the live server from an empty database, one revision at a time; that `alembic check` and
the autogenerate comparison see the LIVE PostgreSQL catalog with no drift from the
models; that an interrupted chain leaves zero tables (PostgreSQL's transactional DDL);
and that revisions 0013 and 0014 apply their PostgreSQL branches, not only the SQLite
ones.

DDL runs as the per-run migration login (session_user = hh_t_..._migrate, current_user =
..._owner); the data a revision is shown to carry is written and read as the restricted
application login (CI-16, CI-17). These tests CREATE and DROP databases on a throwaway
server; see tests/test_admin_postgres.py for the recipe and the safety harness
(tests/postgres_harness.py: loopback only, a fresh database per test, only databases and
roles of this run are dropped). Without HOUSE_HELPER_TEST_POSTGRES_URL they are skipped,
which is not a pass; in CI the variable is set and a skip FAILS the run (tests/conftest.py).
"""

import os
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

import house_helper.persistence.models  # noqa: F401  (registers the tables)
from house_helper.persistence.base import Base
from house_helper.persistence.database import (
    alembic_config,
    downgrade_database,
    upgrade_database,
)
from tests.postgres_harness import POSTGRES_URL_ENV, Scratch, run_from_environment
from tests.revisions import HEAD_REVISION
from tests.test_persistence_schema import TABLES

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_URL_ENV),
        reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL "
        "(see tests/test_admin_postgres.py)",
    ),
]

# The one line of history, named revision by revision (CI-09). Kept here rather than
# imported from the SQLite module so this test states the whole chain it walks.
CHAIN = (
    "0001_persistence_foundation",
    "0002_session_provenance",
    "0003_email_login_challenges",
    "0004_email_identity_case",
    "0005_identity_invitations",
    "0006_oidc_state_invitation",
    "0007_favorite_recipes",
    "0008_telegram_links",
    "0009_message_attachments",
    "0010_structured_recipe_favorites",
    "0011_telegram_active_selection",
    "0012_shopping_lists",
    "0013_user_text_size",
    "0014_user_measure_overrides",
)
HEAD = CHAIN[-1]
BEFORE_PROFILE = "0012_shopping_lists"
PROFILE = "0013_user_text_size"
assert HEAD == HEAD_REVISION  # one spelling of the head in the tests (CI-08)


@pytest.fixture(scope="session")
def pg_run():
    run = run_from_environment()
    try:
        run.prepare_roles()  # inside the guard: a partial preparation is cleaned up (R2-05)
        yield run
    finally:
        run.close()


@pytest.fixture
def empty(pg_run):
    """An empty database of this run (owned by its NOLOGIN owner, behind the fence);
    dropped afterwards."""
    name = pg_run.create_database(runtime_roles=("app",))
    try:
        yield Scratch(pg_run, name)
    finally:
        pg_run.drop_database(name)


def migrate(scratch, revision):
    """Apply `revision` as the migration login, then close the engine. The ACL contract is
    re-applied after every step (default privileges cover the objects a later step creates)."""
    engine = scratch.migrate_engine()
    try:
        upgrade_database(engine, revision)
        scratch.run.apply_privileges(engine)
    finally:
        engine.dispose()


def downgrade(scratch, revision):
    engine = scratch.migrate_engine()
    try:
        downgrade_database(engine, revision)
    finally:
        engine.dispose()


def as_app(scratch, work):
    """`work(engine)` on the restricted application login, engine closed afterwards."""
    engine = scratch.app_engine()
    try:
        return work(engine)
    finally:
        engine.dispose()


def _tables(scratch) -> set:
    return as_app(scratch, lambda engine: set(sa.inspect(engine).get_table_names()))


def _version(scratch) -> list:
    def read(engine):
        with engine.connect() as connection:
            return connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all()

    return as_app(scratch, read)


def _columns(scratch, table: str = "users") -> dict:
    return as_app(
        scratch, lambda engine: {column["name"]: column for column in sa.inspect(engine).get_columns(table)}
    )


def _checks(scratch, table: str = "users") -> dict:
    return as_app(
        scratch,
        lambda engine: {
            check["name"]: check["sqltext"]
            for check in sa.inspect(engine).get_check_constraints(table)
        },
    )


# --- the chain ---------------------------------------------------------------------


def test_the_chain_applies_from_empty_one_revision_at_a_time(empty):
    assert _tables(empty) == set()

    for revision in CHAIN:
        migrate(empty, revision)
        assert _version(empty) == [revision]
        assert "alembic_version" in _tables(empty)

    assert _version(empty) == [HEAD_REVISION]
    assert _tables(empty) == TABLES | {"alembic_version"}


def test_a_second_upgrade_head_is_a_no_op(empty):
    migrate(empty, HEAD_REVISION)
    tables_before, version_before = _tables(empty), _version(empty)

    migrate(empty, HEAD_REVISION)

    assert (_tables(empty), _version(empty)) == (tables_before, version_before)


def test_an_interrupted_chain_leaves_zero_tables(empty):
    # PostgreSQL DDL is transactional, so a migration process that dies before its
    # commit leaves nothing behind (CI-16). The whole chain runs in an outer
    # transaction that this test rolls back; Alembic uses that transaction because the
    # connection already has one (env.py hands its connection to the context).
    engine = empty.migrate_engine()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            config = alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, HEAD_REVISION)
            assert "users" in set(sa.inspect(connection).get_table_names())
            transaction.rollback()
    finally:
        engine.dispose()

    assert _tables(empty) == set()  # not even alembic_version


# --- the live catalog ---------------------------------------------------------------


def test_alembic_check_is_clean_on_the_live_catalog(empty):
    migrate(empty, HEAD_REVISION)

    engine = empty.migrate_engine()
    try:
        config = alembic_config()
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.check(config)  # raises AutogenerateDiffsDetected on any drift
    finally:
        engine.dispose()


def test_the_migrated_schema_has_no_drift_from_the_models_on_postgresql(empty):
    # CI-10: the comparison runs against PostgreSQL's own catalog, not rendered SQL.
    migrate(empty, HEAD_REVISION)

    def compare(engine):
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            return compare_metadata(context, Base.metadata)

    assert as_app(empty, compare) == []


def test_the_comparison_sees_a_planted_difference(empty):
    # Guard the guard: the live comparison must notice a schema that is not the models'.
    migrate(empty, HEAD_REVISION)
    engine = empty.migrate_engine()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE users ADD COLUMN planted_by_the_test INTEGER")
    finally:
        engine.dispose()

    def compare(app):
        with app.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            return compare_metadata(context, Base.metadata)

    drift = as_app(empty, compare)
    assert any("planted_by_the_test" in repr(change) for change in drift), drift


# --- 0013 and 0014, on their PostgreSQL branches ------------------------------------


def test_0013_applies_its_postgresql_branch(empty):
    migrate(empty, BEFORE_PROFILE)
    migrate(empty, PROFILE)

    assert _version(empty) == [PROFILE]
    column = _columns(empty)["text_size"]
    assert column["nullable"] is False
    assert "normal" in str(column["default"])
    checks = _checks(empty)
    assert set(checks) >= {"ck_users_text_size_max_length", "ck_users_text_size_valid"}

    def write(engine):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO users (id, display_name, created_at, updated_at) "
                    "VALUES (:id, :name, now(), now())"
                ),
                {"id": uuid.uuid4(), "name": "Elena"},
            )
            assert connection.execute(sa.text("SELECT text_size FROM users")).scalar() == "normal"
            connection.execute(sa.text("UPDATE users SET text_size = 'large'"))

    as_app(empty, write)

    def refused(engine):
        with pytest.raises(sa.exc.IntegrityError):  # ck_users_text_size_valid
            with engine.begin() as connection:
                connection.execute(sa.text("UPDATE users SET text_size = 'huge'"))

    as_app(empty, refused)


def test_0014_applies_its_postgresql_branch(empty):
    migrate(empty, HEAD_REVISION)

    columns = _columns(empty)
    for column, minimum, maximum in (
        ("measure_glass_ml", 50, 1000),
        ("measure_teaspoon_ml", 1, 20),
        ("measure_tablespoon_ml", 5, 50),
    ):
        assert columns[column]["nullable"] is True, column
        rule = _checks(empty)[f"ck_users_{column}_range"]
        # PostgreSQL renders BETWEEN as two comparisons in pg_get_constraintdef.
        assert f"{column} >= {minimum}" in rule and f"{column} <= {maximum}" in rule, column

    def write(engine):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO users (id, display_name, created_at, updated_at) "
                    "VALUES (:id, :name, now(), now())"
                ),
                {"id": uuid.uuid4(), "name": "Elena"},
            )
            connection.execute(
                sa.text("UPDATE users SET measure_glass_ml = 250, measure_teaspoon_ml = 5, measure_tablespoon_ml = 15")
            )
            assert connection.execute(
                sa.text("SELECT measure_glass_ml FROM users")
            ).scalar() == 250

    as_app(empty, write)

    def refused(engine):
        for statement in (
            "UPDATE users SET measure_glass_ml = 49",  # below the range
            "UPDATE users SET measure_glass_ml = 1001",  # above the range
            "UPDATE users SET measure_teaspoon_ml = 0",
            "UPDATE users SET measure_tablespoon_ml = 51",
        ):
            with pytest.raises(sa.exc.IntegrityError):
                with engine.begin() as connection:
                    connection.execute(sa.text(statement))

    as_app(empty, refused)


def test_0013_and_0014_downgrade_and_upgrade_again(empty):
    migrate(empty, HEAD_REVISION)

    downgrade(empty, BEFORE_PROFILE)

    assert _version(empty) == [BEFORE_PROFILE]
    assert not {"text_size", "measure_glass_ml", "measure_teaspoon_ml", "measure_tablespoon_ml"} & set(
        _columns(empty)
    )

    migrate(empty, HEAD_REVISION)
    assert _version(empty) == [HEAD_REVISION]
    assert {"text_size", "measure_glass_ml", "measure_teaspoon_ml", "measure_tablespoon_ml"} <= set(
        _columns(empty)
    )


# --- 0004's offline refusal is asserted in the dialect tests -----------------------
# (tests/test_persistence_migrations.py: SQLite offline is refused with its documented
# error; PostgreSQL offline renders the CHECK swap. CI-11.)
