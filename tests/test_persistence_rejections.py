"""The strictness of `tests.persistence_support.rejected()` - offline (HH-DB-01 R2-04/R3-04).

The shared helper used to accept ANY SQLSTATE 22001 as proof of the expected rule and ANY
CHECK violation as proof of a specifically named one. Both were false positives. These
tests drive the helper with crafted driver errors (no server, no connection) and prove,
one by one, that the previous acceptance is gone and the intended assertions still work.

R3-04 added two more false positives to close, and their negative controls are here:

  * a named CHECK could be "proved" by the constraint's name occurring in the rejected
    row's text (PostgreSQL's DETAIL quotes the row) - now the structured
    `diag.constraint_name` alone decides on PostgreSQL, and so does `diag.column_name`
    for NOT NULL;
  * a width error (22001) could come from a DIFFERENT column of the same declared width.
    `rejected` now accepts no 22001 at all. `refused_width` (R4) writes the row itself:
    one UPDATE of one existing row that assigns one bounded text column, so there is no
    second value to confuse it with, and no SQL text to parse.

The one exception kept is documented in the helper itself: psycopg refuses a NUL byte
client-side before PostgreSQL can evaluate a printable-ASCII CHECK.
"""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import house_helper.persistence.models  # noqa: F401  (registers the tables)
from house_helper.persistence.base import Base
from house_helper.persistence.database import create_session_factory
from house_helper.persistence.models import FavoriteRecipe, User
from tests.persistence_support import (
    declared_width,
    refused_width,
    rejected,
    require_over_width,
    text_over_width,
    width_probe,
)


class Diag:
    def __init__(self, constraint_name=None, column_name=None, message_detail=None):
        self.constraint_name = constraint_name
        self.column_name = column_name
        self.message_detail = message_detail


class Orig(Exception):
    def __init__(self, sqlstate, message, constraint_name=None, column_name=None, detail=None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = Diag(constraint_name, column_name, detail)


class FakeSession:
    """The little of an ORM Session that `rejected()` uses."""

    def __init__(self, error, dialect="postgresql"):
        self.error = error
        self._dialect = dialect

    @contextmanager
    def begin_nested(self):
        yield

    def flush(self):
        raise self.error

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self._dialect))


def failure(
    sqlstate,
    message,
    constraint_name=None,
    column_name=None,
    *,
    statement="INSERT INTO favorite_recipes (title) VALUES (%(title)s)",
    params=None,
    detail=None,
):
    # (The statement and its bound values are what R3-04's width attribution reads.)
    return sa.exc.IntegrityError(
        statement,
        params if params is not None else {"title": "x" * 201},
        Orig(sqlstate, message, constraint_name, column_name, detail),
    )


def accepts(session, constraint):
    with rejected(session, constraint):
        pass


def refuses(session, constraint):
    with pytest.raises(AssertionError):
        with rejected(session, constraint):
            pass


# --- named CHECKs: only the named one counts -------------------------------------------------


def test_the_intended_named_check_is_recognized_from_the_structured_diagnostic():
    # The message deliberately does NOT spell the name: only diag.constraint_name proves it.
    accepts(
        FakeSession(failure("23514", "new row violates check constraint", "ck_users_a_valid")),
        "ck_users_a_valid",
    )


def test_an_unrelated_check_does_not_satisfy_a_named_check():
    refuses(
        FakeSession(failure("23514", "new row violates check constraint", "ck_users_b_valid")),
        "ck_users_a_valid",
    )


def test_a_check_without_a_constraint_name_does_not_satisfy_a_named_check():
    refuses(FakeSession(failure("23514", "new row violates check constraint")), "ck_users_a_valid")


def test_the_generic_check_expectation_still_accepts_a_check_violation():
    accepts(FakeSession(failure("23514", "new row violates check constraint")), "CHECK constraint failed")


def test_the_generic_check_expectation_does_not_accept_another_state():
    with pytest.raises(AssertionError):
        with rejected(FakeSession(failure("23503", "foreign key violation")), "CHECK constraint failed"):
            pass


# --- R3: exactly the two names that necessarily fire together --------------------------------


def test_a_bounded_pair_of_check_names_accepts_either_one():
    for name in (
        "ck_favorite_recipes_kind_valid",
        "ck_favorite_recipes_kind_consistent",
    ):
        accepts(
            FakeSession(failure("23514", "new row violates check constraint", name)),
            ("ck_favorite_recipes_kind_valid", "ck_favorite_recipes_kind_consistent"),
        )


def test_a_bounded_pair_of_check_names_refuses_a_third_rule():
    refuses(
        FakeSession(
            failure(
                "23514",
                "new row violates check constraint",
                "ck_favorite_recipes_version_valid",
            )
        ),
        ("ck_favorite_recipes_kind_valid", "ck_favorite_recipes_kind_consistent"),
    )


def test_a_bounded_pair_on_sqlite_matches_either_reported_name():
    for name in (
        "ck_favorite_recipes_kind_valid",
        "ck_favorite_recipes_kind_consistent",
    ):
        with rejected(
            FakeSession(failure(None, f"CHECK constraint failed: {name}"), dialect="sqlite"),
            ("ck_favorite_recipes_kind_valid", "ck_favorite_recipes_kind_consistent"),
        ):
            pass


# --- R4: a type-width refusal is never proved by `rejected` ---------------------------------
#
# A 22001 is PostgreSQL's refusal of a value wider than a column's declared type. It does
# not say which column, so `rejected` - which decides from the driver's diagnostics alone -
# must never accept it. A test that means a declared width uses `refused_width`, which
# writes the one column itself and can therefore attribute the refusal (see the end of this
# file).


def test_a_22001_never_satisfies_a_named_check():
    refuses(
        FakeSession(
            failure(
                "22001",
                "value too long for type character varying(128)",
                statement=(
                    "INSERT INTO oidc_states (nonce, code_verifier) "
                    "VALUES (%(nonce)s, %(code_verifier)s)"
                ),
                params={"nonce": "n" * 129, "code_verifier": "v" * 16},
            )
        ),
        "ck_oidc_states_nonce_length",
    )


# --- 22001 is no longer a wildcard -----------------------------------------------------------


def test_an_unrelated_22001_does_not_satisfy_a_named_check():
    refuses(
        FakeSession(failure("22001", "value too long for type character varying(64)")),
        "ck_users_display_name_max_length",
    )


def test_an_unrelated_22001_does_not_satisfy_a_not_null_expectation():
    with pytest.raises(AssertionError):
        with rejected(
            FakeSession(failure("22001", "value too long for type character varying(64)")),
            "NOT NULL constraint failed: users.id",
        ):
            pass


# --- the documented NUL exception ------------------------------------------------------------


def test_the_nul_byte_exception_remains_effective():
    accepts(
        FakeSession(
            failure(None, "PostgreSQL text fields cannot contain NUL (0x00) bytes")
        ),
        "ck_users_display_name_printable_ascii",
    )


def test_a_client_side_failure_without_the_nul_text_is_not_excused():
    with pytest.raises(AssertionError):
        with rejected(
            FakeSession(failure(None, "some other client-side failure")),
            "ck_users_display_name_printable_ascii",
        ):
            pass


# --- NOT NULL, foreign keys and column lists stay structured ---------------------------------


def test_not_null_uses_the_column_the_server_names():
    accepts(
        FakeSession(
            failure(
                "23502",
                'null value in column "user_id" violates not-null constraint',
                column_name="user_id",
            )
        ),
        "NOT NULL constraint failed: table.user_id",
    )
    with pytest.raises(AssertionError):
        with rejected(
            FakeSession(
                failure(
                    "23502",
                    'null value in column "other" violates not-null constraint',
                    column_name="other",
                )
            ),
            "NOT NULL constraint failed: table.user_id",
        ):
            pass


def test_the_not_null_column_comes_from_the_structured_diagnostic_not_the_message():
    # The message says the right column; the server's structured diagnostic does not.
    with pytest.raises(AssertionError):
        with rejected(
            FakeSession(
                failure(
                    "23502",
                    'null value in column "user_id" violates not-null constraint',
                    column_name="other",
                )
            ),
            "NOT NULL constraint failed: table.user_id",
        ):
            pass


def test_a_named_check_cannot_be_matched_through_row_data_in_the_message():
    """R3-04 negative control: the expected CHECK fires nowhere; its name merely occurs in
    the rejected row (the DETAIL PostgreSQL prints under a different CHECK's error)."""
    with pytest.raises(AssertionError):
        with rejected(
            FakeSession(
                failure(
                    "23514",
                    'new row violates check constraint "ck_users_b_valid"',
                    "ck_users_b_valid",
                )
            ),
            "ck_users_a_valid",
        ):
            pass


def test_a_foreign_key_expectation_requires_its_state():
    accepts(FakeSession(failure("23503", "violates foreign key constraint")), "FOREIGN KEY")
    with pytest.raises(AssertionError):
        with rejected(FakeSession(failure("23505", "duplicate key")), "FOREIGN KEY"):
            pass


def test_the_key_columns_may_come_from_the_structured_detail():
    accepts(
        FakeSession(
            failure(
                "23505",
                "duplicate key value violates unique constraint",
                "uq_favorite_recipes_user_message_recipe",
                detail="Key (user_id, source_message_id, source_recipe_index)=(a, 7, 0) already exists.",
            )
        ),
        "favorite_recipes.user_id, favorite_recipes.source_message_id, favorite_recipes.source_recipe_index",
    )


def test_the_message_text_alone_does_not_name_the_key_columns():
    # The columns appear only in the rejected row's text; neither the structured
    # constraint name nor the structured DETAIL names them.
    with pytest.raises(AssertionError):
        with rejected(
            FakeSession(
                failure(
                    "23505",
                    'duplicate key value (provider, subject)=(x, y) already exists',
                    "some_other_key",
                    detail="Key (other)=(x) already exists.",
                )
            ),
            "users.provider, users.subject",
        ):
            pass


def test_a_detail_naming_other_columns_does_not_satisfy_the_key():
    with pytest.raises(AssertionError):
        with rejected(
            FakeSession(
                failure(
                    "23505",
                    "duplicate key value",
                    "uq_something",
                    detail="Key (provider)=(x) already exists.",
                )
            ),
            "users.provider, users.subject",
        ):
            pass


def test_a_column_list_needs_a_named_constraint_and_every_column():
    accepts(
        FakeSession(
            failure("23505", "duplicate key value", "users_provider_subject_key")
        ),
        "users.provider, users.subject",
    )
    with pytest.raises(AssertionError):
        with rejected(FakeSession(failure("23505", "duplicate key value")), "users.provider, users.subject"):
            pass
    with pytest.raises(AssertionError):
        with rejected(
            FakeSession(failure("23505", "duplicate key value", "users_provider_key")),
            "users.provider, users.subject",
        ):
            pass


# --- R4: a declared width, proved by a probe the helper writes itself -------------------------
#
# The old reader parsed the failing INSERT. It was wrong in both directions: a literal
# before a positional parameter shifted the values onto the wrong columns, a `::JSONB` or a
# numeric value changed the effective stored representation, and PostgreSQL's acceptance of
# trailing spaces beyond varchar(n) made "len(value) > n" an unreliable test; meanwhile real
# SQLAlchemy statements it did not anticipate were refused. The helper now writes the row:
# one UPDATE of one existing row, keyed by its primary key, assigning exactly one bounded
# text column. The statement has no literal, no cast and no second text value, so a 22001
# can only belong to that column, and it is accepted only for the exact compiled statement.

WIDTH_200 = "value too long for type character varying(200)"
WIDTH_128 = "value too long for type character varying(128)"


class FakeWidthSession:
    """A PostgreSQL session that executes nothing: the probe's statement is compiled and a
    driver 22001 is raised for exactly it (or for a crafted statement, in the negative
    controls)."""

    def __init__(self, *, message=WIDTH_200, state="22001", statement=None, constraint_name=None):
        self.message = message
        self.state = state
        self.statement = statement
        self.constraint_name = constraint_name
        self.dialect = postgresql.dialect()
        self.executed = None

    @contextmanager
    def begin_nested(self):
        yield

    def get_bind(self):
        return SimpleNamespace(dialect=self.dialect)

    def execute(self, statement):
        self.executed = statement
        raised = str(statement.compile(dialect=self.dialect))
        raise sa.exc.DataError(
            raised if self.statement is None else self.statement,
            (),
            Orig(self.state, self.message, self.constraint_name),
        )


@pytest.fixture
def sqlite_session():
    """A real SQLite database with the whole model schema, for the branch that does not
    involve 22001 (SQLite ignores VARCHAR widths; its CHECK is the rule)."""
    engine = sa.create_engine(
        "sqlite://", poolclass=sa.pool.StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    try:
        with create_session_factory(engine)() as session:
            yield session
    finally:
        engine.dispose()


def test_the_probe_writes_exactly_one_bounded_text_column():
    """'Multiple potentially overflowing columns' cannot arise: the statement the helper
    compiles assigns the target and (for favorite_recipes) only the automatic `updated_at`,
    which is a timestamp. The target's value is a plain bind parameter, not an expression."""
    statement = width_probe("favorite_recipes", "title", "x" * 201, {"id": uuid.uuid4()})

    # `_values` is SQLAlchemy's own statement dictionary: the one explicit value. The
    # automatic `updated_at` is added when the statement is compiled, and it has no width.
    assert set(statement._values) == {"title"}
    assert isinstance(statement._values["title"], sa.BindParameter)
    assert all(
        getattr(column.type, "length", None) is None
        for column in Base.metadata.tables["favorite_recipes"].c
        if column.onupdate is not None
    )


def test_the_probe_requires_the_primary_key_and_nothing_else():
    with pytest.raises(AssertionError, match="primary key"):
        width_probe("favorite_recipes", "title", "x" * 201, {"user_id": uuid.uuid4()})


def test_a_column_without_a_declared_width_is_not_a_width_probe():
    with pytest.raises(AssertionError, match="not a bounded text column"):
        declared_width("favorite_recipes", "notes")  # Text: its bound is a CHECK, not a type
    with pytest.raises(AssertionError, match="not a bounded text column"):
        declared_width("message_recipes", "ingredients")  # JSON: a conversion, not a width


def test_a_non_text_column_is_refused_by_the_helper():
    session = FakeWidthSession()
    with pytest.raises(AssertionError, match="not a text column"):
        refused_width(
            session,
            "message_recipes",
            "ingredients",
            "{not even json}",
            key={"message_id": 1, "position": 0},
        )
    assert session.executed is None


def test_trailing_spaces_are_not_an_overflow():
    """PostgreSQL truncates a value whose excess is only spaces instead of refusing it, so
    `len(value) > n` is the wrong test. The probe refuses a value it cannot prove is over."""
    assert text_over_width("x" * 200, 200) is False
    assert text_over_width("x" * 200 + "  ", 200) is False
    assert text_over_width("x" * 200 + " x", 200) is True
    with pytest.raises(AssertionError, match="not provably over"):
        require_over_width("favorite_recipes", "title", "x" * 200 + "  ", 200)


@pytest.mark.parametrize(
    "value",
    [10**100, 1.5, {"title": "x" * 201}, ["x"] * 201, b"x" * 201],
    ids=["int", "float", "dict", "list", "bytes"],
)
def test_a_value_the_server_would_convert_is_refused(value):
    """A number or a JSON document is converted by PostgreSQL before it reaches the column;
    its stored width is not the Python length, so the helper refuses to predict it."""
    with pytest.raises(AssertionError, match="must bind text"):
        require_over_width("oidc_states", "nonce", value, 128)

    session = FakeWidthSession(message=WIDTH_128)
    with pytest.raises(AssertionError, match="must bind text"):
        refused_width(session, "oidc_states", "nonce", value, key={"id": uuid.uuid4()})
    assert session.executed is None


def test_the_probe_statement_with_the_declared_width_is_accepted():
    session = FakeWidthSession(message=WIDTH_200)
    refused_width(session, "favorite_recipes", "title", "x" * 201, key={"id": uuid.uuid4()})
    assert session.executed is not None


def test_a_22001_for_another_statement_is_refused():
    """The original Codex reproduction: a literal before the parameter. The old reader
    shifted the parameter onto `title`; no crafted statement is the probe's own, so it is
    refused before any attribution is attempted. The same rule closes a width-limiting cast,
    a function call or a concatenation: none of them is the statement the helper compiled."""
    session = FakeWidthSession(
        statement="INSERT INTO favorite_recipes (title, content) VALUES ('ok', %(content)s)"
    )
    with pytest.raises(AssertionError, match="not raised by the single-column probe"):
        refused_width(session, "favorite_recipes", "title", "x" * 201, key={"id": uuid.uuid4()})


def test_a_22001_for_another_width_is_refused():
    session = FakeWidthSession(message="value too long for type character varying(100)")
    with pytest.raises(AssertionError):
        refused_width(session, "favorite_recipes", "title", "x" * 201, key={"id": uuid.uuid4()})


def test_a_22001_with_another_state_is_refused():
    session = FakeWidthSession(state="23514", message="new row violates check constraint")
    with pytest.raises(AssertionError):
        refused_width(session, "favorite_recipes", "title", "x" * 201, key={"id": uuid.uuid4()})


def test_sqlite_still_refuses_through_the_named_check(sqlite_session):
    user = User(display_name="owner")
    sqlite_session.add(user)
    sqlite_session.flush()
    favorite = FavoriteRecipe(user_id=user.id, title="ok", content="ok", source_message_id=1)
    sqlite_session.add(favorite)
    sqlite_session.flush()

    refused_width(sqlite_session, "favorite_recipes", "title", "x" * 201, key={"id": favorite.id})

    with pytest.raises(AssertionError):
        refused_width(
            sqlite_session,
            "favorite_recipes",
            "title",
            "x" * 201,
            key={"id": favorite.id},
            check="ck_something_else",
        )
