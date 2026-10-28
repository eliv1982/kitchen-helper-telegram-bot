"""The schema as declared by the models: its shape, its names, its portability.

Nothing here touches a database file except the extensibility test, which uses
an in-memory one.
"""

import re
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

import house_helper.persistence.models  # noqa: F401  (registers the tables)
from house_helper.persistence.base import NAMING_CONVENTION, Base
from house_helper.persistence.database import create_db_engine

TABLES = {
    "users",
    "allowlist_entries",
    "identity_links",
    "sessions",
    "oidc_states",
    "email_login_challenges",
    "identity_invitations",
    "favorite_recipes",
    "telegram_links",
    "telegram_link_codes",
    "message_attachments",
    "conversations",
    "messages",
    "turn_requests",
}

POSTGRESQL_IDENTIFIER_LIMIT = 63


def test_the_schema_has_exactly_the_fourteen_tables():
    assert set(Base.metadata.tables) == TABLES


def test_every_table_has_a_named_primary_key():
    names = {
        table.name: table.primary_key.name for table in Base.metadata.tables.values()
    }

    assert names == {table: f"pk_{table}" for table in TABLES}


def test_every_unique_constraint_foreign_key_check_and_index_has_a_name():
    unnamed = []
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if constraint.name is None:
                unnamed.append(f"{table.name}: {constraint!r}")
        for index in table.indexes:
            if index.name is None:
                unnamed.append(f"{table.name}: {index!r}")

    assert unnamed == []


def test_every_foreign_key_says_what_happens_on_delete():
    unspecified = [
        f"{table.name}.{fk.parent.name}"
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
        if fk.ondelete is None
    ]

    assert unspecified == []


def test_every_identifier_fits_postgresql():
    identifiers = set(Base.metadata.tables)
    for table in Base.metadata.tables.values():
        identifiers.update(column.name for column in table.columns)
        identifiers.update(c.name for c in table.constraints)
        identifiers.update(i.name for i in table.indexes)

    too_long = sorted(name for name in identifiers if len(name) > POSTGRESQL_IDENTIFIER_LIMIT)

    assert too_long == []


def test_constraint_names_follow_the_convention():
    messages = Base.metadata.tables["messages"]

    assert NAMING_CONVENTION["pk"] == "pk_%(table_name)s"
    assert {c.name for c in messages.constraints} >= {
        "pk_messages",
        "fk_messages_conversation_id_conversations",
        "ck_messages_role_valid",
        "ck_messages_expires_after_created",
    }
    assert {i.name for i in messages.indexes} == {
        "ix_messages_conversation_id_id",
        "ix_messages_expires_at",
    }


def test_the_schema_renders_for_postgresql():
    dialect = postgresql.dialect()
    ddl = "\n".join(
        str(CreateTable(table).compile(dialect=dialect))
        for table in Base.metadata.sorted_tables
    ) + "\n".join(
        str(CreateIndex(index).compile(dialect=dialect))
        for table in Base.metadata.sorted_tables
        for index in table.indexes
    )

    assert ddl.count("CREATE TABLE") == len(TABLES)
    assert "UUID" in ddl  # native, not CHAR(32)
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "JSONB" in ddl
    assert "BIGSERIAL" in ddl  # the message id
    assert "ON DELETE CASCADE" in ddl and "ON DELETE NO ACTION" in ddl
    assert "DEFERRABLE" not in ddl  # the links to messages are checked by the statement
    assert "AUTOINCREMENT" not in ddl  # SQLite-only
    assert "~ '^[!-~]+$'" in ddl  # printable ASCII, the PostgreSQL spelling
    assert "interval '8760 hours'" in ddl  # the 365-day ceiling, the PostgreSQL spelling
    assert "GLOB" not in ddl and "datetime(" not in ddl  # nothing SQLite-only


def _check_texts(dialect) -> dict[str, str]:
    """name -> the CHECK's SQL as it is written for `dialect`."""
    return {
        constraint.name: str(
            constraint.sqltext.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
        )
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


def _functions_in(texts) -> set[str]:
    return {name.lower() for text in texts for name in re.findall(r"([A-Za-z_]+)\(", text)}


# What both databases run with the same meaning. Everything else in a CHECK is a
# comparison, AND/OR/IN/BETWEEN/IS NULL, or one of the constructs below.
PORTABLE_FUNCTIONS = {"length", "lower", "replace", "substr"}
# Used only in the SQLite spelling of the 365-day ceiling and of printable ASCII
# (house_helper.persistence.sqlchecks). PostgreSQL has operators for both, and
# its text columns cannot hold a BLOB, so it has no use for typeof().
SQLITE_ONLY_FUNCTIONS = {"datetime", "coalesce", "cast", "typeof"}
# The position of the "@" in an email subject: instr() in SQLite, strpos() in
# PostgreSQL, each database having only its own (sqlchecks.EmailSubjectNormalized).
POSITION_FUNCTIONS = {"instr": "sqlite", "strpos": "postgresql"}


def test_the_check_constraints_use_only_functions_both_databases_have_apart_from_three_constructs():
    postgres = _functions_in(_check_texts(postgresql.dialect()).values())
    lite = _functions_in(_check_texts(sqlite.dialect()).values())

    assert postgres <= PORTABLE_FUNCTIONS | {"strpos"}  # PostgreSQL's spellings need no special function
    assert lite <= PORTABLE_FUNCTIONS | SQLITE_ONLY_FUNCTIONS | {"instr"}
    assert lite & SQLITE_ONLY_FUNCTIONS == SQLITE_ONLY_FUNCTIONS  # and the allowance is all used
    assert "strpos" in postgres and "strpos" not in lite
    assert "instr" in lite and "instr" not in postgres


def test_only_the_dialect_specific_rules_are_spelled_differently_per_database():
    postgres, lite = _check_texts(postgresql.dialect()), _check_texts(sqlite.dialect())

    differing = {name for name in postgres if postgres[name] != lite[name]}

    assert differing == {
        "ck_allowlist_entries_email_subject_normalized",
        "ck_identity_links_email_subject_normalized",
        "ck_allowlist_entries_subject_printable_ascii",
        "ck_identity_links_subject_printable_ascii",
        "ck_identity_links_email_printable_ascii",
        "ck_turn_requests_idempotency_key_printable_ascii",
        "ck_messages_expires_within_retention",
        "ck_turn_requests_expires_within_retention",
    }


def test_no_check_contains_a_percent_sign():
    # psycopg doubles a percent sign when the SQL goes through it (and Alembic's
    # offline SQL would come out unrunnable), so no CHECK may contain one, in
    # either spelling. LIKE patterns and strftime formats are out for that reason.
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        offenders = [n for n, text in _check_texts(dialect).items() if "%" in text]
        assert offenders == [], dialect.name


def test_the_two_spellings_of_the_dialect_specific_rules_say_what_they_should():
    postgres, lite = _check_texts(postgresql.dialect()), _check_texts(sqlite.dialect())

    assert postgres["ck_messages_expires_within_retention"] == (
        "expires_at <= created_at + interval '8760 hours'"
    )
    assert "datetime(substr(created_at, 1, 19), '+365 days') || substr(created_at, 20)" in (
        lite["ck_messages_expires_within_retention"]
    )
    key = "ck_turn_requests_idempotency_key_printable_ascii"
    assert postgres[key] == "idempotency_key ~ '^[!-~]+$'"
    assert "NOT GLOB '*[^!-~]*'" in lite[key]
    assert "CAST(idempotency_key AS BLOB)" in lite[key]
    # The email rule: a lower-case domain, a local part that is free (only the
    # position of the "@" is spelled per database).
    rule = "ck_allowlist_entries_email_subject_normalized"
    for position, text in (("strpos", postgres[rule]), ("instr", lite[rule])):
        domain = f"substr(subject, {position}(subject, '@'))"
        assert f"{domain} = lower({domain})" in text
        assert "subject = lower(subject)" not in text  # that was the whole address


def test_every_check_prints_with_the_default_dialect():
    # str() uses the default dialect; the custom elements must not raise there
    # either, since tools print constraints that way.
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, sa.CheckConstraint):
                assert str(constraint.sqltext)


# --- SQLite ignores VARCHAR(n): every bounded string column has a real limit --------


def _checks_on(table) -> list[str]:
    return [
        str(c.sqltext.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        for c in table.constraints
        if isinstance(c, sa.CheckConstraint)
    ]


def _is_pinned(table, column) -> bool:
    """Some CHECK limits the column: a length() bound, an equality with a fixed
    length, an equality with a constant, or a closed list of values."""
    name = column.name
    for text in _checks_on(table):
        if re.search(rf"length\({name}\)\s*(<=|=|BETWEEN)", text):
            return True
        if re.fullmatch(rf"{name} = '[^']*'", text):
            return True
        if re.search(rf"(^|[^_a-z]){name} IN \(", text):
            return True
    return False


def test_every_string_column_with_a_declared_width_has_a_check_that_enforces_it():
    unpinned = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, sa.String)
        and not isinstance(column.type, sa.Text)
        and column.type.length is not None
        and not _is_pinned(table, column)
    ]

    assert unpinned == []


def test_the_guard_for_declared_widths_does_see_a_column_that_has_none():
    table = sa.Table("scratch", sa.MetaData(), sa.Column("name", sa.String(5)))

    assert not _is_pinned(table, table.c.name)


def test_lists_of_values_fit_their_columns():
    # A closed list is a limit only if its longest member fits the column.
    from house_helper.persistence.models import (
        AllowlistProvider,
        FailureKind,
        MessageRole,
        TurnStatus,
    )

    messages = Base.metadata.tables["messages"]
    turns = Base.metadata.tables["turn_requests"]
    allowlist = Base.metadata.tables["allowlist_entries"]

    assert max(len(r.value) for r in MessageRole) <= messages.c.role.type.length
    assert max(len(r.value) for r in TurnStatus) <= turns.c.status.type.length
    assert max(len(r.value) for r in FailureKind) <= turns.c.failure_kind.type.length
    assert max(len(r.value) for r in AllowlistProvider) <= allowlist.c.provider.type.length


# --- the shape of the references ---------------------------------------------------


def _composite_foreign_keys(table):
    return {
        fk.name: (
            [c.name for c in fk.columns],
            [element.target_fullname for element in fk.elements],
        )
        for fk in table.constraints
        if isinstance(fk, sa.ForeignKeyConstraint) and len(fk.columns) > 1
    }


def test_turn_requests_link_messages_through_composite_keys_that_pin_conversation_and_role():
    table = Base.metadata.tables["turn_requests"]

    assert _composite_foreign_keys(table) == {
        "fk_turn_requests_user_message": (
            ["conversation_id", "user_message_id", "user_message_role"],
            ["messages.conversation_id", "messages.id", "messages.role"],
        ),
        "fk_turn_requests_assistant_message": (
            ["conversation_id", "assistant_message_id", "assistant_message_role"],
            ["messages.conversation_id", "messages.id", "messages.role"],
        ),
    }


def test_the_links_to_messages_refuse_deletion_and_are_checked_immediately():
    table = Base.metadata.tables["turn_requests"]
    links = [
        fk
        for fk in table.constraints
        if isinstance(fk, sa.ForeignKeyConstraint) and len(fk.columns) > 1
    ]

    assert len(links) == 2
    assert all(fk.ondelete == "NO ACTION" for fk in links)
    assert not any(fk.deferrable for fk in links)


def test_a_request_must_have_its_input_and_a_message_belongs_to_one_request():
    table = Base.metadata.tables["turn_requests"]
    unique = {
        tuple(c.name for c in u.columns)
        for u in table.constraints
        if isinstance(u, sa.UniqueConstraint)
    }

    assert table.c.user_message_id.nullable is False
    assert table.c.assistant_message_id.nullable is True
    assert ("user_message_id",) in unique and ("assistant_message_id",) in unique


def test_messages_offer_the_key_the_links_point_at():
    table = Base.metadata.tables["messages"]
    unique = {
        tuple(c.name for c in u.columns)
        for u in table.constraints
        if isinstance(u, sa.UniqueConstraint)
    }

    assert ("conversation_id", "id", "role") in unique


def test_identity_links_can_only_exist_for_a_provisioned_entry_of_the_same_user():
    links = Base.metadata.tables["identity_links"]
    entries = Base.metadata.tables["allowlist_entries"]

    assert _composite_foreign_keys(links) == {
        "fk_identity_links_allowlist_entry": (
            ["user_id", "provider", "subject"],
            [
                "allowlist_entries.user_id",
                "allowlist_entries.provider",
                "allowlist_entries.subject",
            ],
        )
    }
    unique = {
        tuple(c.name for c in u.columns)
        for u in entries.constraints
        if isinstance(u, sa.UniqueConstraint)
    }
    assert ("user_id", "provider", "subject") in unique and ("provider", "subject") in unique


def test_allowlist_entries_and_identity_links_share_one_set_of_identity_rules():
    shared = [
        "provider_valid",
        "subject_length",
        "subject_printable_ascii",
        "email_subject_normalized",
        "telegram_subject_not_email",
    ]
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        texts = _check_texts(dialect)
        for rule in shared:
            assert texts[f"ck_allowlist_entries_{rule}"] == texts[f"ck_identity_links_{rule}"], rule


def test_user_owned_tables_can_be_added_later_beside_the_foundation():
    # Preferences and favourites arrive in a later increment as ordinary
    # user-owned tables. Sketch one on a copy of the metadata to show that
    # users.id and the naming convention are enough to hang it off a user.
    copy = sa.MetaData(naming_convention=NAMING_CONVENTION)
    for table in Base.metadata.tables.values():
        table.to_metadata(copy)
    favorites = sa.Table(
        "favorites",
        copy,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("recipe", sa.String(200), nullable=False),
        sa.UniqueConstraint("user_id", "recipe"),
    )
    engine = create_db_engine("sqlite://")
    copy.create_all(engine)

    try:
        with engine.begin() as connection:
            user_id = connection.execute(
                sa.insert(copy.tables["users"]).values(
                    id=uuid.uuid4(),
                    created_at=sa.func.current_timestamp(),
                    updated_at=sa.func.current_timestamp(),
                )
            ).inserted_primary_key[0]
            connection.execute(sa.insert(favorites).values(user_id=user_id, recipe="borscht"))
            connection.execute(sa.delete(copy.tables["users"]))

            left = connection.execute(sa.select(sa.func.count()).select_from(favorites)).scalar()
    finally:
        engine.dispose()

    assert left == 0
    assert next(iter(favorites.foreign_keys)).constraint.name == "fk_favorites_user_id_users"
