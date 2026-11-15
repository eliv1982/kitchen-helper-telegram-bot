"""The strict reading of the purge login's refused `pg_dump` (HH-DB-01 Stage 1 Fix 02A).

Offline: these cases never open a connection and are collected without
`HOUSE_HELPER_TEST_POSTGRES_URL`. They call `assert_the_purge_dump_was_refused` itself - the
function the live test calls - so no second, more forgiving implementation can pass a dump
the live contract must reject.

The predicate parses complete pg_dump 16 records; it never searches stderr for a fragment.
What it accepts is one `pg_dump: error: query failed: ERROR:  permission denied for table
<name>` for the batched table lock this login cannot read, with that lock's own `Query was:
LOCK TABLE ... IN ACCESS SHARE MODE` detail naming exactly one `public.<name>`, and `<name>`
in the fixture's catalog-derived set. Each case below is a way that reading must fail: a
second denial, a timeout, a same-named table in another schema, a denial quoted inside
another error's detail, two primary errors, a denial on an object the login may read, and
every non-privilege way a dump can fail.
"""

import pytest

from tests.test_postgres_roles import assert_the_purge_dump_was_refused

# A stand-in for the fixture: what the purge login may not read. Table names are real
# (users/favorite_recipes/shopping_lists are outside the ten retention tables), and a
# sequence of a table it may not read is unreadable too.
UNREADABLE = frozenset({"users", "favorite_recipes", "shopping_lists", "users_id_seq"})

EXIT_REFUSED = 1

# The one primary server line and its lock context, in pg_dump 16's own wording.
DENIAL = "pg_dump: error: query failed: ERROR:  permission denied for table users"
LOCK = "pg_dump: detail: Query was: LOCK TABLE public.users IN ACCESS SHARE MODE"

# Failures that are not a judgment about privilege, in pg_dump 16's own wording.
NOT_A_REFUSAL = {
    "missing database": (
        b'pg_dump: error: connection to server at "127.0.0.1", port 55432 failed: '
        b'FATAL:  database "house_helper_test_9f3c_1" does not exist\n'
    ),
    "connection refused": (
        b'pg_dump: error: connection to server at "127.0.0.1", port 55432 failed: '
        b"Connection refused\n"
        b"\tIs the server running on that host and accepting TCP/IP connections?\n"
    ),
    "hba rejection": (
        b'pg_dump: error: connection to server at "172.18.0.2", port 5432 failed: '
        b"FATAL:  no pg_hba.conf entry for host \"172.18.0.2\", "
        b'user "hh_t_9f3c_purge", database "house_helper_test_9f3c_1", no encryption\n'
    ),
    "authentication failure": (
        b'pg_dump: error: connection to server at "172.18.0.2", port 5432 failed: '
        b'FATAL:  password authentication failed for user "hh_t_9f3c_purge"\n'
    ),
    "client version mismatch": (
        b"pg_dump: error: server version: 17.2 (Debian 17.2-1); "
        b"pg_dump version: 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)\n"
        b"pg_dump: error: aborting because of server version mismatch\n"
    ),
    "connection timeout": (
        b'pg_dump: error: connection to server at "127.0.0.1", port 55432 failed: '
        b"timeout expired\n"
    ),
    "transport failure": (
        b'pg_dump: error: Dumping the contents of table "users" failed: PQgetResult() failed.\n'
        b"pg_dump: error: Error message from server: server closed the connection unexpectedly\n"
    ),
    "server abort after a lost backend": (
        b"pg_dump: error: server closed the connection unexpectedly\n"
    ),
    "no diagnostic at all": b"",
}


def dump(*records: str) -> bytes:
    """pg_dump's stderr for these records: one line each, with no blank line between them,
    as the logger prints them (`pg_log_generic_v` drops `PQerrorMessage`'s trailing newline
    and writes its own)."""
    return "".join(f"{record}\n\n" for record in records).encode()


def refused(stderr: bytes, *, unreadable=UNREADABLE, returncode=EXIT_REFUSED) -> str:
    return assert_the_purge_dump_was_refused(
        stderr, returncode=returncode, unreadable=unreadable
    )


# --- the one accepted reading -----------------------------------------------------------------


def test_the_real_refusal_shape_passes():
    """Positive control: the two records `die_on_query_failure` prints, adjacent, and exit 1."""
    assert refused(dump(DENIAL, LOCK)) == "users"


def test_the_batched_lock_context_of_the_real_dump_passes():
    """pg_dump locks every plain table in ONE statement; the refusal names the first table it
    cannot lock and the detail carries the whole batch, including relations the login may
    read before and after it."""
    batch = (
        "pg_dump: detail: Query was: LOCK TABLE public.alembic_version, public.sessions, "
        "public.users, public.favorite_recipes IN ACCESS SHARE MODE"
    )
    assert refused(dump(DENIAL, batch)) == "users"


def test_the_acceptance_needs_an_object_the_fixture_calls_unreadable():
    """A denial is not accepted when the fixture says the login may read everything: the
    predicate is about this database's privileges, not about the word `permission`."""
    with pytest.raises(AssertionError):
        refused(dump(DENIAL, LOCK), unreadable=frozenset())


def test_a_zero_exit_is_rejected():
    with pytest.raises(AssertionError):
        refused(dump(DENIAL, LOCK), returncode=0)


# --- the four Codex counterexamples -----------------------------------------------------------


def test_a_second_denial_on_a_retention_table_is_rejected():
    """Codex case 1: the expected `users` denial, then a denial for `sessions`, a retention
    table the purge login may read. Two primary errors are not one."""
    with pytest.raises(AssertionError):
        refused(
            dump(
                DENIAL,
                LOCK,
                "pg_dump: error: query failed: ERROR:  permission denied for table sessions",
                "pg_dump: detail: Query was: LOCK TABLE public.sessions IN ACCESS SHARE MODE",
            )
        )


def test_a_statement_timeout_beside_the_expected_denial_is_rejected():
    """Codex case 2: a statement timeout is not a judgment about privilege, even next to the
    expected denial."""
    with pytest.raises(AssertionError):
        refused(
            dump(
                "pg_dump: error: query failed: ERROR:  canceling statement due to statement timeout",
                LOCK,
                DENIAL,
                LOCK,
            )
        )


def test_a_single_error_that_is_a_statement_timeout_is_rejected():
    with pytest.raises(AssertionError):
        refused(
            dump(
                "pg_dump: error: query failed: ERROR:  canceling statement due to statement timeout",
                LOCK,
            )
        )


def test_a_denial_whose_lock_context_names_another_schema_is_rejected():
    """Codex case 3: the ERROR names `users`, but the statement it failed on is another
    schema's same-named table."""
    with pytest.raises(AssertionError):
        refused(
            dump(
                DENIAL,
                "pg_dump: detail: Query was: LOCK TABLE unrelated_schema.users "
                "IN ACCESS SHARE MODE",
            )
        )


def test_a_lock_context_with_two_same_named_tables_is_rejected():
    """The same table name in two schemas makes the denial's object ambiguous."""
    with pytest.raises(AssertionError):
        refused(
            dump(
                DENIAL,
                "pg_dump: detail: Query was: LOCK TABLE public.users, unrelated_schema.users "
                "IN ACCESS SHARE MODE",
            )
        )


def test_a_denial_quoted_inside_another_records_detail_is_rejected():
    """Codex case 4: the expected denial appears only as DETAIL text under an unrelated
    primary error."""
    with pytest.raises(AssertionError):
        refused(
            dump(
                "pg_dump: error: query failed: ERROR:  canceling statement due to lock timeout",
                "pg_dump: detail: ERROR:  permission denied for table users",
            )
        )


def test_a_server_statement_line_is_not_a_primary_error():
    """A server `STATEMENT:` line is client-visible text, not a primary ERROR record."""
    with pytest.raises(AssertionError):
        refused(
            dump(
                DENIAL,
                "STATEMENT:  LOCK TABLE public.users IN ACCESS SHARE MODE;",
                LOCK,
            )
        )


# --- further rejections -----------------------------------------------------------------------


def test_two_primary_errors_for_two_unreadable_tables_are_rejected():
    with pytest.raises(AssertionError):
        refused(
            dump(
                DENIAL,
                LOCK,
                "pg_dump: error: query failed: ERROR:  permission denied for table favorite_recipes",
                "pg_dump: detail: Query was: LOCK TABLE public.favorite_recipes "
                "IN ACCESS SHARE MODE",
            )
        )


def test_a_primary_denial_for_an_object_the_login_may_read_is_rejected():
    """A table in the retention set: the fixture proves the login may read it, so a denial
    there is a broken fixture, not the expected refusal."""
    with pytest.raises(AssertionError):
        refused(
            dump(
                "pg_dump: error: query failed: ERROR:  permission denied for table sessions",
                "pg_dump: detail: Query was: LOCK TABLE public.sessions IN ACCESS SHARE MODE",
            )
        )


def test_a_denial_of_another_kind_or_a_different_statement_is_rejected():
    """The accepted operation is the batched table lock. A schema denial, a sequence denial,
    a sequence read and a context that is not the lock statement are all rejected - even
    when the named object is one the login may not read."""
    for failure, context in (
        (
            "pg_dump: error: query failed: ERROR:  permission denied for schema public",
            LOCK,
        ),
        (
            "pg_dump: error: query failed: ERROR:  permission denied for sequence users_id_seq",
            "pg_dump: detail: Query was: LOCK TABLE public.users_id_seq IN ACCESS SHARE MODE",
        ),
        (
            DENIAL,
            "pg_dump: detail: Query was: SELECT last_value, is_called FROM public.users",
        ),
        (DENIAL, LOCK + ";"),
    ):
        with pytest.raises(AssertionError):
            refused(dump(failure, context))


def test_a_warning_beside_the_expected_denial_is_rejected():
    with pytest.raises(AssertionError):
        refused(
            dump(
                "pg_dump: warning: there are circular foreign-key constraints on this table",
                DENIAL,
                LOCK,
            )
        )


def test_every_non_privilege_dump_failure_is_rejected():
    """Missing database, authentication, HBA, connection, client-version and transport
    failures, and 'no diagnostic at all': none of them can be the accepted record pair."""
    for case, stderr in NOT_A_REFUSAL.items():
        with pytest.raises(AssertionError):
            refused(stderr)
