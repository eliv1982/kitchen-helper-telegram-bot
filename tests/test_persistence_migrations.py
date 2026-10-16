"""The Alembic migrations: upgrade, downgrade, and agreement with the models.

SQLite files under tmp_path only. PostgreSQL is covered up to what can be shown
without a server: the migration is rendered to SQL for that dialect (Alembic's
offline mode needs no driver and no connection).
"""

import ast
import io
import re
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

import house_helper.persistence
import house_helper.persistence.models  # noqa: F401  (registers the tables)
from house_helper.persistence.base import Base
from house_helper.persistence.database import (
    MIGRATIONS_DIR,
    alembic_config,
    create_db_engine,
    create_session_factory,
    downgrade_database,
    upgrade_database,
)
from tests.persistence_support import new_conversation, new_message, new_user
from tests.test_persistence_schema import TABLES

INITIAL = "0001_persistence_foundation"
REPO_ROOT = Path(house_helper.persistence.__file__).resolve().parents[2]


@pytest.fixture
def empty_engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'migrations.db'}")
    yield engine
    engine.dispose()


def _tables(engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _version(engine):
    with engine.connect() as connection:
        return connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all()


# --- the history ------------------------------------------------------------------


def test_the_history_is_one_revision_and_one_head():
    script = ScriptDirectory.from_config(alembic_config())

    assert script.get_heads() == [INITIAL]
    assert [revision.revision for revision in script.walk_revisions()] == [INITIAL]
    assert script.get_revision(INITIAL).down_revision is None


def test_migrations_do_not_import_the_application():
    # A migration is a frozen description of the schema as it was; importing the
    # models would make it change whenever they do.
    offenders = []
    for path in (MIGRATIONS_DIR / "versions").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            offenders += [f"{path.name}: {m}" for m in modules if m.split(".")[0] == "house_helper"]

    assert offenders == []


# --- upgrade ----------------------------------------------------------------------


def test_upgrade_creates_every_table_and_records_the_revision(empty_engine):
    upgrade_database(empty_engine)

    assert _tables(empty_engine) == TABLES | {"alembic_version"}
    assert _version(empty_engine) == [INITIAL]


def test_upgrading_again_changes_nothing(empty_engine):
    upgrade_database(empty_engine)
    upgrade_database(empty_engine)

    assert _tables(empty_engine) == TABLES | {"alembic_version"}
    assert _version(empty_engine) == [INITIAL]


def test_the_migrated_schema_has_no_drift_from_the_models(empty_engine):
    upgrade_database(empty_engine)

    with empty_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        drift = compare_metadata(context, Base.metadata)

    assert drift == []


def test_alembic_check_agrees_when_env_py_is_run_on_its_own(tmp_path):
    # env.py must register the models itself. Run in a fresh interpreter where
    # nothing else has imported them: with an empty Base.metadata the check
    # would report every table as unexpected.
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from alembic import command
from house_helper.persistence.database import alembic_config, create_db_engine, upgrade_database

engine = create_db_engine("sqlite:///" + sys.argv[2])
upgrade_database(engine)
config = alembic_config()
with engine.connect() as connection:
    config.attributes["connection"] = connection
    command.check(config)
print("ok")
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(REPO_ROOT), str(tmp_path / "check.db")],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("ok")


# --- the migrated schema is the model schema, constraint for constraint ------------


def _fingerprint(engine) -> dict:
    """Everything about the schema that a behaviour test could depend on."""
    inspector = sa.inspect(engine)
    result = {}
    for table in sorted(TABLES):
        result[table] = {
            "columns": {
                column["name"]: (str(column["type"]), column["nullable"], column["default"])
                for column in inspector.get_columns(table)
            },
            "pk": inspector.get_pk_constraint(table),
            "uniques": sorted(
                (u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(table)
            ),
            "indexes": sorted(
                (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in inspector.get_indexes(table)
            ),
            "fks": sorted(
                (
                    f["name"],
                    tuple(f["constrained_columns"]),
                    f["referred_table"],
                    tuple(f["referred_columns"]),
                    tuple(sorted(f["options"].items())),  # ON DELETE, DEFERRABLE
                )
                for f in inspector.get_foreign_keys(table)
            ),
            "checks": sorted(
                (c["name"], re.sub(r"\s+", " ", c["sqltext"]).strip())
                for c in inspector.get_check_constraints(table)
            ),
        }
    return result


def test_migrations_and_models_build_the_same_schema(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    modeled = create_db_engine(f"sqlite:///{tmp_path / 'modeled.db'}")
    try:
        upgrade_database(migrated)
        Base.metadata.create_all(modeled)

        assert _fingerprint(migrated) == _fingerprint(modeled)
    finally:
        migrated.dispose()
        modeled.dispose()


def test_the_comparison_sees_the_constraints_it_is_meant_to_compare(empty_engine):
    # Guard the guard: a fingerprint that quietly came back empty would make the
    # equality above meaningless.
    upgrade_database(empty_engine)
    fingerprint = _fingerprint(empty_engine)
    turn_requests = fingerprint["turn_requests"]

    assert len(turn_requests["checks"]) == 20
    assert len(turn_requests["fks"]) == 3
    assert (
        "uq_turn_requests_conversation_id_idempotency_key",
        ("conversation_id", "idempotency_key"),
    ) in turn_requests["uniques"]
    assert ("uq_turn_requests_user_message_id", ("user_message_id",)) in turn_requests["uniques"]
    assert (
        "fk_turn_requests_user_message",
        ("conversation_id", "user_message_id", "user_message_role"),
        "messages",
        ("conversation_id", "id", "role"),
        (),  # NO ACTION is the default and not reported; nothing is deferred
    ) in turn_requests["fks"]
    assert (
        "fk_identity_links_allowlist_entry",
        ("user_id", "provider", "subject"),
        "allowlist_entries",
        ("user_id", "provider", "subject"),
        (("ondelete", "CASCADE"),),
    ) in fingerprint["identity_links"]["fks"]
    assert ("uq_messages_conversation_id_id_role", ("conversation_id", "id", "role")) in (
        fingerprint["messages"]["uniques"]
    )


def test_the_comparison_sees_the_storage_class_and_timestamp_rules(empty_engine):
    # Guard the guard, again: the SQLite-only parts of the printable-ASCII and
    # retention CHECKs are what the fingerprint equality must be able to notice.
    upgrade_database(empty_engine)
    fingerprint = _fingerprint(empty_engine)
    retention = dict(fingerprint["messages"]["checks"])["ck_messages_expires_within_retention"]
    turn_retention = dict(fingerprint["turn_requests"]["checks"])["ck_turn_requests_expires_within_retention"]
    key_rule = dict(fingerprint["turn_requests"]["checks"])["ck_turn_requests_idempotency_key_printable_ascii"]
    email_rule = dict(fingerprint["identity_links"]["checks"])["ck_identity_links_email_printable_ascii"]

    assert "typeof(email) IN ('text', 'null')" in email_rule
    assert "typeof(idempotency_key) IN ('text', 'null')" in key_rule
    for text in (retention, turn_retention):
        for column in ("created_at", "expires_at"):
            assert f"typeof({column}) = 'text'" in text
            assert f"length(CAST({column} AS BLOB)) = 26" in text
            assert f"datetime(substr({column}, 1, 19), '+0 days') = substr({column}, 1, 19)" in text
        assert "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:" in text
        assert "'+365 days'" in text


# --- downgrade --------------------------------------------------------------------


def test_downgrade_removes_every_table_and_empties_the_version_table(empty_engine):
    upgrade_database(empty_engine)

    downgrade_database(empty_engine, "base")

    assert _tables(empty_engine) == {"alembic_version"}
    assert _version(empty_engine) == []


def test_a_downgraded_database_can_be_upgraded_again(empty_engine):
    upgrade_database(empty_engine)
    downgrade_database(empty_engine, "base")
    upgrade_database(empty_engine)

    assert _tables(empty_engine) == TABLES | {"alembic_version"}
    assert _version(empty_engine) == [INITIAL]


def test_downgrading_a_database_that_holds_rows_removes_them_with_their_tables(empty_engine):
    upgrade_database(empty_engine)
    with create_session_factory(empty_engine)() as session:
        new_message(session, new_conversation(session, new_user(session)))
        session.commit()

    downgrade_database(empty_engine, "base")

    assert _tables(empty_engine) == {"alembic_version"}


# --- PostgreSQL, as far as it can be checked without a server ----------------------

POSTGRESQL_URL = "postgresql://localhost/house_helper"  # never connected to


def _offline_sql(direction: str) -> str:
    config = alembic_config(POSTGRESQL_URL)
    config.output_buffer = io.StringIO()
    if direction == "up":
        command.upgrade(config, "head", sql=True)
    else:
        command.downgrade(config, f"{INITIAL}:base", sql=True)
    return config.output_buffer.getvalue()


def test_the_upgrade_renders_as_postgresql_sql():
    sql = _offline_sql("up")

    for table in TABLES:
        assert f"CREATE TABLE {table} (" in sql
    assert "UUID NOT NULL" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "JSONB" in sql
    assert "BIGSERIAL" in sql
    assert "ON DELETE CASCADE" in sql and "ON DELETE NO ACTION" in sql
    assert "DEFERRABLE" not in sql
    assert "AUTOINCREMENT" not in sql
    assert "'system_note'" in sql and "'committing'" in sql and "'cancelled'" in sql
    assert "~ '^[!-~]+$'" in sql and "interval '8760 hours'" in sql
    assert "GLOB" not in sql and "datetime(" not in sql  # the SQLite spellings stay out
    assert "typeof(" not in sql and "CAST(" not in sql and "BLOB" not in sql
    assert "%" not in sql  # a doubled percent sign would make the offline SQL unrunnable
    assert f"INSERT INTO alembic_version (version_num) VALUES ('{INITIAL}')" in sql
    assert sql.count("CREATE INDEX") == 10


def test_the_downgrade_renders_as_postgresql_sql():
    sql = _offline_sql("down")

    for table in TABLES:
        assert f"DROP TABLE {table};" in sql
    assert "DELETE FROM alembic_version" in sql


def test_rendering_sql_offline_creates_no_database_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    _offline_sql("up")

    assert list(tmp_path.iterdir()) == []


# The PostgreSQL schema cannot be created here, so the next best thing is to show
# that the migration and the models say the same thing in PostgreSQL's own SQL:
# every column, constraint, foreign key (with its ON DELETE) and
# index, line for line. The SQLite equivalent is the fingerprint comparison above.


def _tables_in(sql: str) -> dict[str, list[str]]:
    """table name -> its definition lines, normalized and sorted."""
    tables = {}
    for match in re.finditer(r"CREATE TABLE (\w+) \(\n(.*?)\n\)", sql, re.DOTALL):
        name, body = match.groups()
        tables[name] = sorted(
            re.sub(r"\s+", " ", line.strip().rstrip(",")) for line in body.split("\n") if line.strip()
        )
    return tables


def _indexes_in(sql: str) -> list[str]:
    return sorted(
        re.sub(r"\s+", " ", m.group(0)).rstrip(";") for m in re.finditer(r"CREATE INDEX [^\n]*", sql)
    )


def _models_as_postgresql() -> str:
    dialect = postgresql.dialect()
    tables = Base.metadata.sorted_tables
    return "\n\n".join(
        [str(CreateTable(table).compile(dialect=dialect)) for table in tables]
        + [str(CreateIndex(index).compile(dialect=dialect)) for table in tables for index in table.indexes]
    )


def test_the_migration_and_the_models_build_the_same_schema_in_postgresql_sql():
    migrated, modeled = _tables_in(_offline_sql("up")), _tables_in(_models_as_postgresql())
    migrated.pop("alembic_version")

    assert set(migrated) == set(modeled) == TABLES
    for table in sorted(TABLES):
        assert migrated[table] == modeled[table], table


def test_the_migration_and_the_models_create_the_same_indexes_in_postgresql_sql():
    assert _indexes_in(_offline_sql("up")) == _indexes_in(_models_as_postgresql())
    assert len(_indexes_in(_models_as_postgresql())) == 10


def test_the_postgresql_comparison_does_see_a_difference(monkeypatch):
    # Guard the guard: change one CHECK in what the migration renders and the
    # comparison must notice.
    sql = _offline_sql("up").replace("interval '8760 hours'", "interval '9999 hours'")

    assert _tables_in(sql)["messages"] != _tables_in(_models_as_postgresql())["messages"]


# --- no connection details are committed ------------------------------------------


def test_the_migration_setup_commits_no_database_url_or_credential():
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in [*MIGRATIONS_DIR.rglob("*.py"), REPO_ROOT / "alembic.ini"]
    }

    url_option = re.compile(r"^\s*sqlalchemy\.url\s*=", re.MULTILINE)
    credentialed_url = re.compile(r"\w+://[^\s/@]*:[^\s/@]*@")  # scheme://user:password@host
    assert [p.name for p, text in sources.items() if url_option.search(text)] == []
    assert [p.name for p, text in sources.items() if credentialed_url.search(text)] == []
