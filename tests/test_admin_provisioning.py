"""`provision-email`: the first user and their approved address, and every refusal.

The command runs in this process against SQLite files copied from a migrated template
(tests/admin_support.py); one test at the end runs the real `python -m` entry point in
subprocesses. PostgreSQL is not available to the suite: what is specific to it (the
advisory lock, the isolation level) is checked here as the statements the command
issues. Against a real server it is checked by tests/test_admin_postgres.py, which is
opt-in (HOUSE_HELPER_TEST_POSTGRES_URL) and is skipped, not run, by CI.

Whenever a test says "nothing changed" it compares every row of every table the command
can touch, before and after (`everything`), not a row count.
"""

import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from house_helper.admin import provisioning
from house_helper.admin.cli import INTERRUPTED, ExitCode, main
from house_helper.admin.provisioning import (
    ADVISORY_LOCK_KEY,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_NOTE_LENGTH,
    InvalidInput,
    Outcome,
    Refusal,
    RefusalReason,
    prepare_request,
    provision_email,
)
from house_helper.persistence.allowlist import allow, allowed_user, revoke
from house_helper.persistence.database import DATABASE_URL_ENV
from house_helper.persistence.email_login import issue_email_login
from house_helper.persistence.models import User
from tests.admin_support import (  # noqa: F401  (fixtures)
    counts,
    db_path,
    db_url,
    everything,
    journal_mode,
    migrated_template,
    open_admin_factory,
    open_factory,
    provision,
    rows,
    run_cli,
    set_journal_mode,
)
from tests.persistence_support import new_identity_link, new_login_session, new_user
from tests.test_persistence_authn import statements

ADDRESS = "Elena@example.com"
REPO_ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def write(url):
    """A committed write on the database, for arranging a state."""
    engine, factory = open_factory(url)
    try:
        with factory() as session, session.begin():
            yield session
    finally:
        engine.dispose()


def entry_of(url, address=ADDRESS):
    found = rows(
        url,
        "SELECT id, user_id, provider, subject, note, revoked_at FROM allowlist_entries "
        "WHERE subject = :subject",
        subject=address,
    )
    assert len(found) == 1
    return found[0]


def create_first_user(capsys, url, address=ADDRESS, *extra):
    result = provision(capsys, url, address, *extra)
    assert result.code == 0, result.text
    return rows(url, "SELECT id FROM users")[0][0]


# --- the first run ------------------------------------------------------------------------


def test_the_first_run_creates_one_user_and_one_approved_email_entry(capsys, db_url):
    result = provision(
        capsys, db_url, "Elena@EXAMPLE.Com", "--display-name", "  Елена  ", "--note", "first user"
    )

    assert result.code == ExitCode.OK
    assert result.err == ""
    assert result.out.startswith("ok [created]: created the initial user ")
    assert "approved Elena@example.com for email sign-in" in result.out
    assert "No Telegram identity was provisioned" in result.out
    assert counts(db_url) == {
        "users": 1,
        "allowlist_entries": 1,
        "identity_links": 0,
        "sessions": 0,
        "email_login_challenges": 0,
        "conversations": 0,
        "messages": 0,
    }
    user_id, display_name, disabled_at = rows(
        db_url, "SELECT id, display_name, disabled_at FROM users"
    )[0]
    assert (display_name, disabled_at) == ("Елена", None)
    _, owner, provider, subject, note, revoked_at = entry_of(db_url, "Elena@example.com")
    assert (owner, provider, subject, note, revoked_at) == (
        user_id,
        "email",
        "Elena@example.com",
        "first user",
        None,
    )
    assert str(uuid.UUID(user_id)) in result.out  # the database holds bare hex, the output dashes


def test_the_entry_is_what_the_allowlist_itself_accepts(capsys, db_url):
    provision(capsys, db_url)

    engine, factory = open_factory(db_url)
    try:
        with factory() as session:
            user = allowed_user(session, "email", ADDRESS)
            assert user is not None
            assert allowed_user(session, "email", "elena@example.com") is None
            assert session.scalar(sa.select(sa.func.count()).select_from(User)) == 1
    finally:
        engine.dispose()


def test_a_display_name_and_a_note_are_optional(capsys, db_url):
    provision(capsys, db_url)

    assert rows(db_url, "SELECT display_name FROM users") == [(None,)]
    assert entry_of(db_url)[4] is None


def test_the_longest_accepted_display_name_and_note_are_stored(capsys, db_url):
    name, note = "n" * MAX_DISPLAY_NAME_LENGTH, "t" * MAX_NOTE_LENGTH

    result = provision(capsys, db_url, ADDRESS, "--display-name", name, "--note", note)

    assert result.code == 0
    assert rows(db_url, "SELECT display_name FROM users") == [(name,)]
    assert entry_of(db_url)[4] == note


# --- one transaction: a failure leaves nothing behind -----------------------------------------


def fail_after(monkeypatch, error, name="allow"):
    """Make provisioning's `allow` do its work and then raise `error`: the user and the
    entry have been written (flushed) when it does."""
    real = getattr(provisioning, name)

    def failing(*args, **kwargs):
        real(*args, **kwargs)
        raise error

    monkeypatch.setattr(provisioning, name, failing)


def test_a_failure_after_the_writes_rolls_the_whole_run_back(capsys, db_url, monkeypatch):
    fail_after(monkeypatch, RuntimeError("SECRET text of the error"))
    before = everything(db_url)

    result = provision(capsys, db_url)

    assert result.code == ExitCode.INTERNAL
    assert everything(db_url) == before
    assert counts(db_url)["users"] == 0 and counts(db_url)["allowlist_entries"] == 0


def test_a_failure_while_reapproving_puts_the_revocation_back(capsys, db_url, monkeypatch):
    create_first_user(capsys, db_url)
    with write(db_url) as session:
        revoke(session, "email", ADDRESS)
    before = everything(db_url)
    fail_after(monkeypatch, RuntimeError("boom"))

    result = provision(capsys, db_url, ADDRESS, "--reapprove-revoked", "--verified-mailbox")

    assert result.code == ExitCode.INTERNAL
    assert everything(db_url) == before
    assert entry_of(db_url)[5] is not None


def test_a_refusal_leaves_every_table_as_it_was(capsys, db_url):
    create_first_user(capsys, db_url)
    before = everything(db_url)

    result = provision(capsys, db_url, "someone.else@example.com")

    assert result.code == ExitCode.REFUSED
    assert everything(db_url) == before


# --- running it again ------------------------------------------------------------------------


def test_repeating_the_command_changes_nothing_not_even_a_differing_name_or_note(capsys, db_url):
    create_first_user(capsys, db_url, ADDRESS, "--display-name", "Елена", "--note", "first")
    before = everything(db_url)

    again = provision(capsys, db_url, ADDRESS, "--display-name", "Другая", "--note", "second")

    assert again.code == ExitCode.OK
    assert again.err == ""
    assert again.out.startswith("ok [unchanged]: ")
    assert "Nothing was changed" in again.out
    assert "--display-name, --note" in again.out  # said to be without effect
    assert everything(db_url) == before
    assert rows(db_url, "SELECT display_name FROM users") == [("Елена",)]
    assert entry_of(db_url)[4] == "first"


def test_the_reapproval_flags_are_without_effect_on_an_address_that_is_not_revoked(capsys, db_url):
    create_first_user(capsys, db_url)
    before = everything(db_url)

    again = provision(capsys, db_url, ADDRESS, "--reapprove-revoked", "--verified-mailbox")

    assert again.code == 0
    assert "ok [unchanged]" in again.out
    assert "--reapprove-revoked, --verified-mailbox" in again.out
    assert everything(db_url) == before


def test_an_unused_flag_on_the_first_run_is_reported_as_without_effect(capsys, db_url):
    result = provision(capsys, db_url, ADDRESS, "--reapprove-revoked", "--verified-mailbox")

    assert result.code == 0
    assert "ok [created]" in result.out
    assert "Without effect in this run: --reapprove-revoked, --verified-mailbox" in result.out


# --- the address: only the domain is lower-cased, nothing is folded ---------------------------------


@pytest.mark.parametrize(
    "typed, stored",
    [
        ("Elena@EXAMPLE.Com", "Elena@example.com"),
        ("  elena@Example.COM \n", "elena@example.com"),
        ("ELENA@example.com", "ELENA@example.com"),
        ("Cook.Name+Tag@GMail.com", "Cook.Name+Tag@gmail.com"),
    ],
)
def test_the_local_part_is_kept_exactly_and_only_the_domain_is_lower_cased(
    capsys, db_url, typed, stored
):
    result = provision(capsys, db_url, typed)

    assert result.code == 0, result.text
    assert rows(db_url, "SELECT subject FROM allowlist_entries") == [(stored,)]
    assert f"approved {stored} for email sign-in" in result.out


def test_the_same_address_in_any_domain_case_is_the_same_address(capsys, db_url):
    create_first_user(capsys, db_url, "Elena@example.com")

    for spelling in ("Elena@EXAMPLE.COM", " Elena@example.com ", "Elena@Example.com"):
        again = provision(capsys, db_url, spelling)
        assert again.code == 0 and "ok [unchanged]" in again.out

    assert counts(db_url)["users"] == 1 and counts(db_url)["allowlist_entries"] == 1


@pytest.mark.parametrize(
    "other",
    [
        "elena@example.com",
        "ELENA@example.com",
        "Elena+tag@example.com",
        "E.lena@example.com",
        "Elena@example.org",
    ],
)
def test_another_spelling_is_another_address_and_is_never_merged(capsys, db_url, other):
    create_first_user(capsys, db_url, "Elena@example.com")
    before = everything(db_url)

    result = provision(capsys, db_url, other)

    assert result.code == ExitCode.REFUSED
    assert result.err.startswith("refused [user_exists]: ")
    assert result.out == ""
    assert everything(db_url) == before


def test_no_provider_specific_alias_folding(capsys, db_url):
    create_first_user(capsys, db_url, "elena@gmail.com")

    for alias in ("e.lena@gmail.com", "elena+kitchen@gmail.com", "elena@googlemail.com"):
        assert provision(capsys, db_url, alias).code == ExitCode.REFUSED

    assert rows(db_url, "SELECT subject FROM allowlist_entries") == [("elena@gmail.com",)]


# --- a user already exists ----------------------------------------------------------------------


def test_an_existing_user_with_a_different_address_is_refused_and_no_second_user_made(
    capsys, db_url
):
    with write(db_url) as session:
        user = new_user(session, display_name="Elena")
        allow(session, user, "telegram", "123456789")
    before = everything(db_url)

    result = provision(capsys, db_url)

    assert result.code == ExitCode.REFUSED
    assert result.err.startswith("refused [user_exists]: ")
    assert everything(db_url) == before
    assert counts(db_url)["users"] == 1


def test_an_existing_user_without_any_entry_is_still_a_user(capsys, db_url):
    with write(db_url) as session:
        new_user(session)

    assert provision(capsys, db_url).code == ExitCode.REFUSED
    assert counts(db_url)["users"] == 1 and counts(db_url)["allowlist_entries"] == 0


@pytest.mark.parametrize("address_belongs_to", ["the first user", "the second user"])
def test_with_several_users_the_command_refuses_everything(capsys, db_url, address_belongs_to):
    with write(db_url) as session:
        first = new_user(session, display_name="first")
        second = new_user(session, display_name="second")
        owner = first if address_belongs_to == "the first user" else second
        allow(session, owner, "email", ADDRESS)
    before = everything(db_url)

    result = provision(capsys, db_url, ADDRESS)

    assert result.code == ExitCode.REFUSED
    assert result.err.startswith("refused [multiple_users]: ")
    assert everything(db_url) == before


def insert_orphan_entry(path: Path, user_id: str, subject: str) -> None:
    """An allowlist entry for a user that does not exist: what a database without
    enforced foreign keys can hold (this engine enforces them, so it is written with
    the sqlite3 module and the pragma off)."""
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO allowlist_entries (id, user_id, provider, subject, created_at) "
            "VALUES (?, ?, 'email', ?, '2026-10-16 12:00:00.000000')",
            (uuid.uuid4().hex, user_id, subject),
        )
        connection.commit()
    finally:
        connection.close()


def test_an_address_listed_for_a_user_who_is_not_the_user_is_refused(capsys, db_url, db_path):
    insert_orphan_entry(db_path, uuid.uuid4().hex, ADDRESS)  # no user at all
    before = everything(db_url)

    none_at_all = provision(capsys, db_url)

    assert none_at_all.code == ExitCode.REFUSED
    assert none_at_all.err.startswith("refused [identity_other_user]: ")
    assert everything(db_url) == before

    with write(db_url) as session:  # now there is one user, and the entry is not theirs
        new_user(session)
    before = everything(db_url)

    someone_else = provision(capsys, db_url)

    assert someone_else.code == ExitCode.REFUSED
    assert someone_else.err.startswith("refused [identity_other_user]: ")
    assert everything(db_url) == before


# --- a revoked address ----------------------------------------------------------------------------


def revoke_with_traces(url, address=ADDRESS):
    """Give the user an email identity link, a session through it and an unused emailed
    sign-in link, then revoke the address (which ends the last two). Returns their ids."""
    with write(url) as session:
        user = session.scalar(sa.select(User))
        link = new_identity_link(session, user, "email", address)
        login = new_login_session(session, user, identity_link_id=link.id)
        issued = issue_email_login(session, address, now=datetime.now(timezone.utc))
        assert issued.link is not None
        assert revoke(session, "email", address) is True
        return login.id.hex, issued.link.challenge_id.hex


def test_a_revoked_address_stays_revoked_by_default(capsys, db_url):
    create_first_user(capsys, db_url)
    with write(db_url) as session:
        revoke(session, "email", ADDRESS)
    before = everything(db_url)

    result = provision(capsys, db_url)

    assert result.code == ExitCode.REFUSED
    assert result.err.startswith("refused [email_revoked]: ")
    assert "--reapprove-revoked" in result.err and "--verified-mailbox" in result.err
    assert everything(db_url) == before


@pytest.mark.parametrize("flag", ["--reapprove-revoked", "--verified-mailbox"])
def test_one_reapproval_flag_alone_is_a_usage_error_and_touches_no_database(capsys, flag):
    result = provision(capsys, None, ADDRESS, flag)  # no database configured: not asked

    assert result.code == ExitCode.USAGE
    assert result.err.startswith("usage [arguments]: ")
    assert "together" in result.err


def test_one_reapproval_flag_alone_does_not_reapprove(capsys, db_url):
    create_first_user(capsys, db_url)
    with write(db_url) as session:
        revoke(session, "email", ADDRESS)
    before = everything(db_url)

    for flag in ("--reapprove-revoked", "--verified-mailbox"):
        assert provision(capsys, db_url, ADDRESS, flag).code == ExitCode.USAGE

    assert everything(db_url) == before


@pytest.mark.parametrize("flags", [{"reapprove_revoked": True}, {"verified_mailbox": True}])
def test_the_function_itself_needs_both_flags_and_does_not_rely_on_the_command_line(
    capsys, db_url, flags
):
    create_first_user(capsys, db_url)
    with write(db_url) as session:
        revoke(session, "email", ADDRESS)
    before = everything(db_url)
    engine, factory = open_factory(db_url)
    try:
        with pytest.raises(Refusal) as refused:
            provision_email(factory, prepare_request(ADDRESS, **flags))
    finally:
        engine.dispose()

    assert refused.value.reason is RefusalReason.EMAIL_REVOKED
    assert everything(db_url) == before


def test_both_flags_lift_the_revocation_on_the_same_row_and_revive_nothing(capsys, db_url):
    user_id = create_first_user(capsys, db_url, ADDRESS, "--display-name", "Елена", "--note", "old")
    session_id, challenge_id = revoke_with_traces(db_url)
    entry_before = entry_of(db_url)
    assert entry_before[5] is not None
    sessions_before = rows(db_url, "SELECT id, revoked_at FROM sessions")
    challenges_before = rows(db_url, "SELECT id, revoked_at FROM email_login_challenges")
    links_before = rows(db_url, "SELECT * FROM identity_links")

    result = provision(
        capsys,
        db_url,
        ADDRESS,
        "--reapprove-revoked",
        "--verified-mailbox",
        "--note",
        "re-verified by phone",
        "--display-name",
        "Ignored",
    )

    assert result.code == ExitCode.OK, result.text
    assert result.out.startswith("ok [reapproved]: ")
    assert "stay revoked" in result.out
    assert "Without effect in this run: --display-name" in result.out
    # The same row, the same user, no new user:
    entry_after = entry_of(db_url)
    assert entry_after[:4] == entry_before[:4]
    assert entry_after[5] is None
    assert entry_after[4] == "re-verified by phone"  # a given note replaces the stored one
    assert rows(db_url, "SELECT id, display_name FROM users") == [(user_id, "Елена")]
    assert counts(db_url)["users"] == 1 and counts(db_url)["allowlist_entries"] == 1
    # Nothing that the revocation ended is alive again:
    assert rows(db_url, "SELECT id, revoked_at FROM sessions") == sessions_before
    assert rows(db_url, "SELECT id, revoked_at FROM email_login_challenges") == challenges_before
    assert rows(db_url, "SELECT * FROM identity_links") == links_before
    assert [row[0] for row in sessions_before] == [session_id]
    assert [row[0] for row in challenges_before] == [challenge_id]
    assert all(revoked_at is not None for _, revoked_at in sessions_before + challenges_before)
    # And the address is allowed again, exactly this spelling:
    engine, factory = open_factory(db_url)
    try:
        with factory() as session:
            assert allowed_user(session, "email", ADDRESS) is not None
            assert allowed_user(session, "email", ADDRESS.lower()) is None
    finally:
        engine.dispose()


def test_reapproving_without_a_note_keeps_the_stored_one(capsys, db_url):
    create_first_user(capsys, db_url, ADDRESS, "--note", "kept")
    with write(db_url) as session:
        revoke(session, "email", ADDRESS)

    result = provision(capsys, db_url, ADDRESS, "--reapprove-revoked", "--verified-mailbox")

    assert result.code == 0 and "ok [reapproved]" in result.out
    assert entry_of(db_url)[4] == "kept"


def test_reapproving_one_spelling_does_not_approve_another(capsys, db_url):
    create_first_user(capsys, db_url, "elena@example.com")
    with write(db_url) as session:
        revoke(session, "email", "elena@example.com")
    before = everything(db_url)

    result = provision(
        capsys, db_url, "Elena@example.com", "--reapprove-revoked", "--verified-mailbox"
    )

    assert result.code == ExitCode.REFUSED  # not on the list: a user exists, this is no entry
    assert result.err.startswith("refused [user_exists]: ")
    assert everything(db_url) == before


# --- a disabled user --------------------------------------------------------------------------------


def disable_user(url):
    rows_changed = None
    engine, _ = open_factory(url)
    try:
        with engine.begin() as connection:
            rows_changed = connection.execute(
                sa.text("UPDATE users SET disabled_at = '2026-10-16 12:00:00.000000'")
            ).rowcount
    finally:
        engine.dispose()
    assert rows_changed == 1


def test_a_disabled_user_is_refused(capsys, db_url):
    create_first_user(capsys, db_url)
    disable_user(db_url)
    before = everything(db_url)

    result = provision(capsys, db_url)

    assert result.code == ExitCode.REFUSED
    assert result.err.startswith("refused [user_disabled]: ")
    assert everything(db_url) == before


def test_a_disabled_user_is_not_enabled_by_reapproval_either(capsys, db_url):
    create_first_user(capsys, db_url)
    with write(db_url) as session:
        revoke(session, "email", ADDRESS)
    disable_user(db_url)
    before = everything(db_url)

    result = provision(capsys, db_url, ADDRESS, "--reapprove-revoked", "--verified-mailbox")

    assert result.code == ExitCode.REFUSED
    assert result.err.startswith("refused [user_disabled]: ")
    assert everything(db_url) == before
    assert entry_of(db_url)[5] is not None


def test_a_disabled_user_blocks_a_different_address_too(capsys, db_url):
    create_first_user(capsys, db_url)
    disable_user(db_url)

    assert provision(capsys, db_url, "other@example.com").code == ExitCode.REFUSED
    assert counts(db_url)["users"] == 1


# --- invalid values never reach the database --------------------------------------------------------

SECRET = "SECRETvalue"
LONG_DOMAIN = "d" * 250 + ".example.com"

INVALID_ADDRESSES = [
    "",
    "   ",
    f"{SECRET}-no-at-sign",
    f"{SECRET}@@example.com",
    f"{SECRET}@a@example.com",
    f"@{SECRET}.example.com",
    f"{SECRET}@",
    f"{SECRET} space@example.com",
    f"{SECRET}@exam ple.com",
    f"{SECRET}\n@example.com",
    f"{SECRET}\u0415\u043b\u0435\u043d\u0430@example.com",  # Cyrillic in the local part
    f"{SECRET}@exampl\u0435.com",  # Cyrillic "e" in the domain
    f"{SECRET}@exam{chr(0x212A)}.com",  # Kelvin sign: str.lower() would make it a "k"
    f"{SECRET}@{LONG_DOMAIN}",  # longer than the 255 an entry may hold
    f'{SECRET}"quoted"@example.com',  # allowlistable, but no mail is ever sent to it
    f"{SECRET},second@example.com",
    f"{SECRET}@localhost",
    f"{'x' * 65}{SECRET}@example.com",
    f"{SECRET}..dots@example.com",
    f"{SECRET}@-bad.example.com",
]


@pytest.mark.parametrize("address", INVALID_ADDRESSES, ids=range(len(INVALID_ADDRESSES)))
def test_an_invalid_address_is_a_usage_error_that_is_not_echoed_and_asks_no_database(
    capsys, address
):
    result = provision(capsys, None, address)  # no database configured: not asked

    assert result.code == ExitCode.USAGE
    assert result.out == ""
    assert result.err.startswith("usage [invalid_value]: the email address ")
    assert SECRET not in result.text


@pytest.mark.parametrize(
    "option, value",
    [
        ("--display-name", ""),
        ("--display-name", "   "),
        ("--display-name", "x" * (MAX_DISPLAY_NAME_LENGTH + 1)),
        ("--display-name", f"{SECRET}\nline two"),
        ("--display-name", f"{SECRET}\ttab"),
        ("--display-name", f"{SECRET}\x00nul"),
        ("--display-name", f"{SECRET}\u200bzero width"),
        ("--display-name", f"{SECRET}\u202eoverride"),
        ("--display-name", f"{SECRET}\udc80undecodable"),
        ("--note", ""),
        ("--note", "y" * (MAX_NOTE_LENGTH + 1)),
        ("--note", f"{SECRET}\rreturn"),
        ("--note", f"{SECRET}\x1b[31mred"),
    ],
)
def test_an_unusable_name_or_note_is_a_usage_error_that_is_not_echoed(capsys, option, value):
    result = provision(capsys, None, ADDRESS, option, value)

    assert result.code == ExitCode.USAGE
    assert result.err.startswith("usage [invalid_value]: ")
    assert SECRET not in result.text


def test_a_bad_command_line_does_not_create_or_open_the_database(capsys, tmp_path):
    missing = tmp_path / "nowhere" / "kitchen.db"

    result = provision(capsys, f"sqlite:///{missing}", "not an address")

    assert result.code == ExitCode.USAGE
    assert not missing.parent.exists()


def test_prepare_request_normalizes_through_the_persistence_rules():
    request = prepare_request(" Cook@Example.COM ", " Name ", " note ")

    assert (request.email, request.display_name, request.note) == (
        "Cook@example.com",
        "Name",
        "note",
    )
    with pytest.raises(InvalidInput):
        prepare_request(None)


# --- the command line itself ------------------------------------------------------------------------


def test_the_exit_statuses_are_stable():
    assert {code.name: int(code) for code in ExitCode} == {
        "OK": 0,
        "INTERNAL": 1,
        "USAGE": 2,
        "REFUSED": 3,
        "DATABASE": 4,
    }
    assert INTERRUPTED == 130


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["no-such-command"],
        ["provision-email"],  # --email is required
        ["provision-email", "--email"],
        ["provision-email", "--em", "a@example.com"],  # no abbreviations
        ["provision-email", "--email", "a@example.com", "--database-url", f"x://u:{SECRET}@h/d"],
        ["provision-email", "--email", "a@example.com", f"{SECRET}-positional"],
        ["provision-email", f"--no-such-{SECRET}=1", "--email", "a@example.com"],
    ],
)
def test_argument_errors_are_a_fixed_message_that_never_quotes_the_text(capsys, argv):
    code = main(argv, environ={})

    captured = capsys.readouterr()
    assert code == ExitCode.USAGE
    assert captured.out == ""
    assert "usage [arguments]: " in captured.err
    assert SECRET not in captured.err
    assert "Traceback" not in captured.err


def test_help_works_and_documents_the_exit_statuses(capsys):
    code = main(["provision-email", "--help"], environ={})

    out = capsys.readouterr().out
    assert code == 0
    assert "--reapprove-revoked" in out and "--verified-mailbox" in out
    assert DATABASE_URL_ENV in out and "never creates a database" in out


def test_a_keyboard_interrupt_is_reported_without_a_traceback(capsys, db_url, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(provisioning, "allow", interrupted)

    result = provision(capsys, db_url)

    assert result.code == INTERRUPTED
    assert "Traceback" not in result.err
    assert counts(db_url)["users"] == 0


# --- what is never printed --------------------------------------------------------------------------


def test_an_unexpected_failure_prints_its_class_and_none_of_its_text(capsys, db_url, monkeypatch):
    error = sa.exc.IntegrityError(
        "INSERT INTO users (display_name) VALUES (?)",
        {"display_name": SECRET},
        Exception(f"UNIQUE constraint failed {SECRET}"),
    )
    fail_after(monkeypatch, error)

    result = provision(capsys, db_url, ADDRESS, "--display-name", "Елена")

    assert result.code == ExitCode.INTERNAL
    assert result.out == ""
    assert result.err.startswith("error [internal]: unexpected IntegrityError. ")
    for leaked in (SECRET, "INSERT", "UNIQUE", "Traceback", "Елена"):
        assert leaked not in result.text


@pytest.mark.parametrize(
    "scenario", ["created", "unchanged", "refused", "revoked", "disabled", "reapproved"]
)
def test_no_outcome_prints_the_database_location(capsys, db_url, db_path, scenario):
    arrange = {
        "created": lambda: None,
        "unchanged": lambda: create_first_user(capsys, db_url),
        "refused": lambda: create_first_user(capsys, db_url, "other@example.com"),
        "revoked": lambda: (create_first_user(capsys, db_url), _revoke(db_url)),
        "disabled": lambda: (create_first_user(capsys, db_url), disable_user(db_url)),
        "reapproved": lambda: (create_first_user(capsys, db_url), _revoke(db_url)),
    }[scenario]
    arrange()
    extra = ("--reapprove-revoked", "--verified-mailbox") if scenario == "reapproved" else ()

    result = provision(capsys, db_url, ADDRESS, *extra)

    assert result.code in (0, 3)
    for leaked in (str(db_path), db_path.name, "sqlite:", db_url, str(db_path.parent)):
        assert leaked not in result.text


def _revoke(url):
    with write(url) as session:
        revoke(session, "email", ADDRESS)


# --- the first statement of the transaction -----------------------------------------------------------


def test_on_sqlite_the_transaction_starts_with_a_write(db_url):
    engine, factory = open_factory(db_url)
    try:
        with statements(engine) as seen:
            provision_email(factory, prepare_request(ADDRESS))
    finally:
        engine.dispose()

    assert seen[0].lstrip().upper().startswith("UPDATE USERS")  # the no-op write: the lock
    assert "WHERE 0 = 1" in seen[0]


def test_the_noop_write_changes_nothing_when_the_run_is_refused(capsys, db_url):
    create_first_user(capsys, db_url)
    engine, factory = open_factory(db_url)
    try:
        before = everything(db_url)
        with pytest.raises(Refusal) as refused:
            provision_email(factory, prepare_request("other@example.com"))
    finally:
        engine.dispose()

    assert refused.value.reason is RefusalReason.USER_EXISTS
    assert everything(db_url) == before


class StubSession:
    """Just enough of a Session to see what `_serialize` asks of it."""

    def __init__(self, dialect: str) -> None:
        self.calls: list = []
        self._dialect = dialect

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self._dialect))

    def connection(self, execution_options=None):
        self.calls.append(("connection", execution_options))

    def execute(self, statement):
        self.calls.append(("execute", statement))


def test_on_postgresql_the_run_takes_a_transaction_scoped_advisory_lock_first():
    stub = StubSession("postgresql")

    provisioning._serialize(stub)

    kinds = [kind for kind, _ in stub.calls]
    assert kinds == ["connection", "execute"]  # the isolation level is chosen before the lock
    assert stub.calls[0][1] == {"isolation_level": "READ COMMITTED"}
    compiled = stub.calls[1][1].compile(dialect=postgresql.dialect())
    assert "pg_advisory_xact_lock(" in str(compiled)
    assert "pg_advisory_lock" not in str(compiled)  # never the session-scoped lock
    assert list(compiled.params.values()) == [ADVISORY_LOCK_KEY]
    # Typed as a bigint, so that a driver which binds server-side sends an int8 (psycopg
    # renders the cast; the generic dialect only knows the type).
    assert isinstance(compiled.binds["param_1"].type, sa.BigInteger)


def test_the_advisory_lock_key_is_fixed_and_fits_a_signed_64_bit_integer():
    assert 0 < ADVISORY_LOCK_KEY < 2**63
    # Pinned: two versions of the command with different keys would not exclude each other.
    assert ADVISORY_LOCK_KEY == 815883989575170308


def test_on_sqlite_the_serialising_statement_is_a_write_that_matches_no_row():
    stub = StubSession("sqlite")

    provisioning._serialize(stub)

    assert [kind for kind, _ in stub.calls] == ["execute"]
    sql = str(stub.calls[0][1].compile(dialect=sqlite.dialect()))
    assert sql.strip() == "UPDATE users SET updated_at=users.updated_at WHERE 0 = 1"


def test_any_other_database_type_is_refused_by_the_run_itself():
    with pytest.raises(provisioning.TargetError) as refused:
        provisioning._serialize(StubSession("mysql"))

    assert refused.value.code == "unsupported_backend"


# --- concurrent first runs --------------------------------------------------------------------------------

SPELLINGS = [
    "Elena@example.com",
    "elena@example.com",
    "ELENA@example.com",
    "Elena@EXAMPLE.COM",
    "elena+kitchen@example.com",
    "e.lena@example.com",
    "Elena@example.org",
    "elena@Example.com",
]
ROUNDS = 3


def run_all_at_once(url, spellings, open_engine=open_factory):
    """One provisioning run per spelling, each on an engine of its own (as separate
    processes would have), all released at the same moment. Returns the results and the
    errors."""
    barrier = threading.Barrier(len(spellings))

    def one(address):
        engine, factory = open_engine(url)
        try:
            request = prepare_request(address)
            barrier.wait(timeout=30)
            return provision_email(factory, request)
        finally:
            engine.dispose()

    results, errors = [], []
    with ThreadPoolExecutor(max_workers=len(spellings)) as pool:
        futures = [pool.submit(one, address) for address in spellings]
        for future in futures:
            try:
                results.append(future.result(timeout=120))
            except Exception as error:  # collected so the test can say what happened
                errors.append(error)
    return results, errors


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_concurrent_first_runs_with_different_spellings_create_exactly_one_user(
    tmp_path, migrated_template, round_number
):
    import shutil

    path = tmp_path / "race.db"
    shutil.copyfile(migrated_template, path)
    url = f"sqlite:///{path}"

    results, errors = run_all_at_once(url, SPELLINGS)

    # Every run ended in a decision: created, unchanged, or refused. None failed with
    # "database is locked" (what a transaction that reads before it writes gets).
    assert [type(error) for error in errors] == [Refusal] * len(errors), errors
    assert len(results) + len(errors) == len(SPELLINGS)
    created = [result for result in results if result.outcome is Outcome.CREATED]
    assert len(created) == 1
    assert counts(url)["users"] == 1
    assert counts(url)["allowlist_entries"] == 1
    winner = created[0].email
    assert rows(url, "SELECT subject FROM allowlist_entries") == [(winner,)]
    # The runs that spelled the winner's address the same way agree with it; every other
    # spelling was refused as a different address.
    same = [s for s in SPELLINGS if prepare_request(s).email == winner]
    assert len(results) == len(same)
    assert all(result.user_id == created[0].user_id for result in results)
    assert all(error.reason is RefusalReason.USER_EXISTS for error in errors)


def test_concurrent_runs_of_the_same_address_agree_on_one_user(tmp_path, migrated_template):
    import shutil

    path = tmp_path / "same.db"
    shutil.copyfile(migrated_template, path)
    url = f"sqlite:///{path}"

    results, errors = run_all_at_once(url, [ADDRESS] * 6)

    assert errors == []
    assert sorted(result.outcome.value for result in results) == ["created"] + ["unchanged"] * 5
    assert counts(url)["users"] == 1


def test_concurrent_first_runs_on_a_rollback_journal_database_still_create_exactly_one_user(
    tmp_path, migrated_template
):
    # The command leaves the journal mode as it finds it, so it must serialise in
    # SQLite's default mode too, not only in the WAL mode the applications set.
    import shutil

    path = tmp_path / "race-delete.db"
    shutil.copyfile(migrated_template, path)
    set_journal_mode(path, "delete")
    url = f"sqlite:///{path}"

    results, errors = run_all_at_once(url, SPELLINGS, open_admin_factory)

    assert journal_mode(path) == "delete"  # (before counts(): reading it back sets WAL)
    assert [p.name for p in tmp_path.iterdir()] == [path.name]
    assert [type(error) for error in errors] == [Refusal] * len(errors), errors
    assert len(results) + len(errors) == len(SPELLINGS)
    assert [result.outcome for result in results].count(Outcome.CREATED) == 1
    assert counts(url)["users"] == 1 and counts(url)["allowlist_entries"] == 1


def clean_environment(**extra) -> dict:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name
        not in {DATABASE_URL_ENV, "BOT_TOKEN", "OPENAI_API_KEY"}
        and not name.startswith("HOUSE_HELPER_")
    }
    environment.update(extra)
    return environment


def run_module(url, *argv):
    return subprocess.Popen(
        [sys.executable, "-m", "house_helper.admin", "provision-email", *argv],
        cwd=REPO_ROOT,
        env=clean_environment(**({DATABASE_URL_ENV: url} if url else {})),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def test_separate_processes_racing_with_different_spellings_make_one_user(
    tmp_path, migrated_template
):
    import shutil

    path = tmp_path / "processes.db"
    shutil.copyfile(migrated_template, path)
    url = f"sqlite:///{path}"
    spellings = ["Elena@example.com", "elena@example.com", "ELENA@example.com", "Elena@EXAMPLE.COM"]

    processes = [run_module(url, "--email", spelling) for spelling in spellings]
    finished = [(p.communicate(timeout=120), p.returncode) for p in processes]

    codes = sorted(code for _, code in finished)
    assert codes[0] == 0 and set(codes) <= {0, 3}, finished
    assert counts(url)["users"] == 1
    assert counts(url)["allowlist_entries"] == 1


def test_the_module_entry_point_reports_a_missing_database_and_prints_no_traceback(tmp_path):
    process = run_module(f"sqlite:///{tmp_path / 'absent.db'}", "--email", ADDRESS)

    out, err = process.communicate(timeout=120)

    assert process.returncode == ExitCode.DATABASE
    assert out == ""
    assert err.startswith("error [database_missing]: ")
    assert "Traceback" not in err
    assert not (tmp_path / "absent.db").exists()


def test_the_module_entry_point_prints_help(tmp_path):
    completed = subprocess.run(
        [sys.executable, "-m", "house_helper.admin", "--help"],
        cwd=REPO_ROOT,
        env=clean_environment(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0
    assert "provision-email" in completed.stdout
