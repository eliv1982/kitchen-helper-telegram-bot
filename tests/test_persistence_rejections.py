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
  * a width error (22001) could come from a DIFFERENT column of the same declared width -
    now the failing statement's bound values must show the expected column, and only it,
    over its declared width.

The one exception kept is documented in the helper itself: psycopg refuses a NUL byte
client-side before PostgreSQL can evaluate a printable-ASCII CHECK.
"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

import house_helper.persistence.models  # noqa: F401  (registers the tables)
from tests.persistence_support import rejected, rejected_width


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
    """The little of an ORM Session that `rejected()`/`rejected_width()` use."""

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


# --- the one place 22001 IS accepted, narrowly -----------------------------------------------


def test_rejected_width_accepts_the_exact_declared_width_of_the_column():
    with rejected_width(
        FakeSession(failure("22001", "value too long for type character varying(200)")),
        "favorite_recipes",
        "title",
    ):
        pass


def test_rejected_width_refuses_another_width():
    with pytest.raises(AssertionError):
        with rejected_width(
            FakeSession(failure("22001", "value too long for type character varying(100)")),
            "favorite_recipes",
            "title",
        ):
            pass


def test_rejected_width_refuses_another_state():
    with pytest.raises(AssertionError):
        with rejected_width(
            FakeSession(failure("23514", "new row violates check constraint", "ck_other")),
            "favorite_recipes",
            "title",
        ):
            pass


def test_a_width_error_from_a_same_width_column_is_refused():
    """R3-04 negative control: oidc_states.nonce and oidc_states.code_verifier are both
    VARCHAR(128). A 22001 caused by the OTHER one must not satisfy the nonce assertion."""
    statement = (
        "INSERT INTO oidc_states (state_hash, provider, nonce, code_verifier, created_at, expires_at) "
        "VALUES (%(state_hash)s, %(provider)s, %(nonce)s, %(code_verifier)s, "
        "%(created_at)s, %(expires_at)s)"
    )
    params = {
        "state_hash": "a" * 64,
        "provider": "google",
        "nonce": "n" * 16,  # within 128
        "code_verifier": "v" * 129,  # the one that overflowed
        "created_at": "2026-10-16 12:00:00.000000",
        "expires_at": "2026-10-16 12:10:00.000000",
    }
    with pytest.raises(AssertionError):
        with rejected_width(
            FakeSession(
                failure(
                    "22001",
                    "value too long for type character varying(128)",
                    statement=statement,
                    params=params,
                )
            ),
            "oidc_states",
            "nonce",
        ):
            pass


def test_a_width_error_with_two_overlong_columns_is_refused():
    statement = "INSERT INTO oidc_states (nonce, code_verifier) VALUES (%(nonce)s, %(code_verifier)s)"
    params = {"nonce": "n" * 129, "code_verifier": "v" * 129}
    with pytest.raises(AssertionError):
        with rejected_width(
            FakeSession(
                failure(
                    "22001",
                    "value too long for type character varying(128)",
                    statement=statement,
                    params=params,
                )
            ),
            "oidc_states",
            "nonce",
        ):
            pass


def test_the_named_column_really_over_its_width_is_accepted():
    statement = "INSERT INTO oidc_states (nonce, code_verifier) VALUES (%(nonce)s, %(code_verifier)s)"
    params = {"nonce": "n" * 129, "code_verifier": "v" * 16}
    with rejected_width(
        FakeSession(
            failure(
                "22001",
                "value too long for type character varying(128)",
                statement=statement,
                params=params,
            )
        ),
        "oidc_states",
        "nonce",
    ):
        pass


def test_a_width_error_whose_statement_cannot_be_read_is_refused():
    with pytest.raises(AssertionError):
        with rejected_width(
            FakeSession(
                failure(
                    "22001",
                    "value too long for type character varying(200)",
                    statement="SELECT 1",
                    params={},
                )
            ),
            "favorite_recipes",
            "title",
        ):
            pass


def test_rejected_width_on_a_text_column_requires_its_named_check():
    with rejected_width(
        FakeSession(
            failure("23514", "new row violates check constraint", "ck_favorite_recipes_notes_max_length")
        ),
        "favorite_recipes",
        "notes",
    ):
        pass
    with pytest.raises(AssertionError):
        with rejected_width(
            FakeSession(failure("23514", "new row violates check constraint", "ck_other")),
            "favorite_recipes",
            "notes",
        ):
            pass


def test_rejected_width_on_sqlite_still_requires_the_named_check():
    with rejected_width(
        FakeSession(
            failure(None, "CHECK constraint failed: ck_favorite_recipes_title_max_length"),
            dialect="sqlite",
        ),
        "favorite_recipes",
        "title",
    ):
        pass
    with pytest.raises(AssertionError):
        with rejected_width(
            FakeSession(failure(None, "CHECK constraint failed: ck_something_else"), dialect="sqlite"),
            "favorite_recipes",
            "title",
        ):
            pass
