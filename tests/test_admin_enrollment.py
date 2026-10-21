"""The administrative commands that enroll the initial users: `enroll-user`,
`reissue-invitation`, `revoke-invitation` and `enrollment-status`.

Everything is a SQLite file under pytest's tmp_path, copied from one migrated database
(tests/admin_support.py); nothing leaves the machine. The commands run in this process with
the clock a test gives them, which is how two-hour expiry is shown without waiting. What the
invitation does once presented (verify, claim) is tests/test_persistence_invitations.py; here
it is used only to show that what the command printed is what the database holds.

The properties that matter, each pinned below: a user and their invitation are created in
ONE transaction or not at all; at most two users can be created (a policy of the command,
not of the database), also under concurrency and across processes; a repeated command is not
a second user; reissuing replaces the outstanding invitation atomically; the secret is
printed exactly once, after the commit, on stdout, and is in no refusal, error, log line or
database column; the report shows no Telegram id, digest or secret; a user created here can
sign in nowhere; and provision-email's behaviour and its lock are unchanged.
"""

import io
import itertools
import logging
import re
import shutil
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from house_helper.admin import cli, enrollment, provisioning
from house_helper.admin.cli import INTERRUPTED, ExitCode, main
from house_helper.admin.enrollment import (
    ENROLLMENT_USER_CAP,
    EnrollmentRefusal,
    EnrollmentRefusalReason,
)
from house_helper.admin.provisioning import ADVISORY_LOCK_KEY, Outcome, Refusal, prepare_request, provision_email
from house_helper.persistence import conversations as cv
from house_helper.persistence import invitations as invitations_module
from house_helper.persistence.allowlist import allow, allowed_user, is_allowed, revoke
from house_helper.persistence.authn import LoginDenied, authenticate, complete_login, hash_secret
from house_helper.persistence.database import DATABASE_URL_ENV
from house_helper.persistence.email_login import IssueOutcome, issue_email_login
from house_helper.persistence.invitations import (
    INVITATION_LIFETIME,
    claim_invitation,
    issue_invitation,
    parse_invitation_code,
    revoke_invitations,
    verify_invitation,
)
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
    run_admin,
    set_journal_mode,
    snapshot,
)
from tests.persistence_support import new_conversation, new_message, new_user, stored, utc
from tests.test_admin_provisioning import (
    REPO_ROOT,
    SECRET,
    StubSession,
    clean_environment,
    create_first_user,
    entry_of,
    write,
)
from tests.test_admin_target import database_at
from tests.test_persistence_authn import statements

NOW = utc(2026, 10, 19, 12)
LATER = NOW + timedelta(minutes=5)
TWO_HOURS = timedelta(hours=2)
SUB = "1234123412341234123"
OTHER_SUB = "9876598765987659876"
ROUNDS = 3

USER_ID = re.compile(r"user ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def at(moment):
    return lambda: moment


def enroll(capsys, url, name="Elena", now=NOW):
    return run_admin(capsys, url, "enroll-user", "--display-name", name, clock=at(now))


def reissue(capsys, url, user_id, now=LATER):
    return run_admin(capsys, url, "reissue-invitation", "--user-id", str(user_id), clock=at(now))


def revoke_it(capsys, url, user_id, now=LATER):
    return run_admin(capsys, url, "revoke-invitation", "--user-id", str(user_id), clock=at(now))


def status(capsys, url, now=NOW):
    return run_admin(capsys, url, "enrollment-status", clock=at(now))


def issued_from(result):
    """(user id, invitation id, secret) of a successful enroll-user or reissue-invitation, after
    checking the shape of its output: a status line and, on its own line, the code."""
    lines = result.out.splitlines()
    assert result.code == ExitCode.OK and result.err == "", result.text
    assert len(lines) == 2 and lines[1].startswith("invitation code: "), result.out
    parsed = parse_invitation_code(lines[1][len("invitation code: "):])
    assert parsed is not None, lines[1]
    user_id = uuid.UUID(USER_ID.search(lines[0]).group(1))
    return user_id, parsed[0], parsed[1]


def users_of(url):
    return [uuid.UUID(hex_id) for (hex_id,) in rows(url, "SELECT id FROM users ORDER BY created_at, id")]


def invitations_of(url):
    return rows(
        url,
        "SELECT id, user_id, provider, token_hash, created_at, expires_at, consumed_at, revoked_at "
        "FROM identity_invitations ORDER BY created_at, id",
    )


def outstanding(url, user_id):
    return rows(
        url,
        "SELECT id FROM identity_invitations WHERE user_id = :user AND consumed_at IS NULL "
        "AND revoked_at IS NULL",
        user=user_id.hex,
    )


def write_sql(url, statement, **params):
    engine, _ = open_factory(url)
    try:
        with engine.begin() as connection:
            return connection.execute(sa.text(statement), params).rowcount
    finally:
        engine.dispose()


FIRST_ID = "00000000-0000-4000-8000-000000000001"  # sorts before any other id of a test
LAST_ID = "ffffffff-ffff-4fff-bfff-ffffffffffff"  # and this one after


def insert_invitation(url, user_id, *, created, expires=None, consumed=None, revoked=None, invitation_id=None):
    """One invitation row by plain SQL, past the issuing function: how a state that the commands
    would not write in this order (equal or reversed times, chosen ids) is arranged. The expiry
    is two hours after `created` unless given."""

    def text(moment):
        return None if moment is None else stored(moment)

    write_sql(
        url,
        "INSERT INTO identity_invitations (id, user_id, provider, token_hash, created_at, expires_at, "
        "consumed_at, revoked_at) VALUES (:id, :user, 'telegram', :digest, :created, :expires, "
        ":consumed, :revoked)",
        id=uuid.UUID(invitation_id).hex if invitation_id else uuid.uuid4().hex,
        user=user_id.hex,
        digest=hash_secret(uuid.uuid4().hex),
        created=text(created),
        expires=text(expires or created + TWO_HOURS),
        consumed=text(consumed),
        revoked=text(revoked),
    )


def user_without_invitation(url, name="Elena"):
    with write(url) as db:
        return new_user(db, display_name=name).id


def verifies(url, invitation_id, token, now=LATER):
    engine, factory = open_factory(url)
    try:
        with factory() as db:
            return verify_invitation(db, invitation_id, token, now=now) is not None
    finally:
        engine.dispose()


def all_text(url) -> str:
    """Every value of every table, as one string, to look for a secret in."""
    return "\n".join(f"{table}: {row}" for table, found in everything(url).items() for row in found)


def link_in(url, user_id, subject, invitation_id=None, now=LATER):
    """What the later web claim does, in one transaction: claim the invitation, then sign in."""
    with write(url) as db:
        wanted = invitation_id or db.scalar(
            sa.text("SELECT id FROM identity_invitations WHERE user_id = :u ORDER BY created_at DESC"),
            {"u": user_id.hex},
        )
        claimed = claim_invitation(db, str(uuid.UUID(str(wanted))), subject, now=now)
        assert claimed is not None and claimed.user_id == user_id
        return complete_login(db, "telegram", claimed.subject, now=now)


# --- enroll-user ------------------------------------------------------------------------------------------


def test_enrolling_creates_one_user_and_one_invitation_and_nothing_else(capsys, db_url):
    result = enroll(capsys, db_url, "  Елена  ")

    user_id, invitation_id, token = issued_from(result)
    assert result.out.startswith(f"ok [enrolled]: created user {user_id} and issued a Telegram invitation")
    assert counts(db_url) == {
        "users": 1,
        "allowlist_entries": 0,  # the user can sign in nowhere yet
        "identity_links": 0,
        "sessions": 0,
        "email_login_challenges": 0,
        "identity_invitations": 1,
        "conversations": 0,
        "messages": 0,
    }
    assert rows(db_url, "SELECT id, display_name, disabled_at FROM users") == [(user_id.hex, "Елена", None)]
    # the user and the invitation are from the same clock: the one the command was given
    assert rows(db_url, "SELECT created_at, updated_at FROM users") == [(stored(NOW), stored(NOW))]
    (row,) = invitations_of(db_url)
    assert row[4] == stored(NOW) and row[5] == stored(NOW + TWO_HOURS)
    assert (row[0], row[1], row[2]) == (uuid.UUID(invitation_id).hex, user_id.hex, "telegram")
    assert row[6] is None and row[7] is None


def test_the_printed_code_is_the_stored_invitation_and_ends_two_hours_after_the_clock(capsys, db_url):
    result = enroll(capsys, db_url)

    user_id, invitation_id, token = issued_from(result)
    assert "valid until 2026-10-19T14:00:00Z (2 hours, single use)" in result.out
    (row,) = invitations_of(db_url)
    assert row[3] == hash_secret(token)
    assert verifies(db_url, invitation_id, token, now=NOW + TWO_HOURS - timedelta(microseconds=1))
    assert not verifies(db_url, invitation_id, token, now=NOW + TWO_HOURS)  # exclusive
    assert not verifies(db_url, invitation_id, "A" * 43, now=LATER)  # and only this secret


def test_the_secret_is_printed_once_on_stdout_and_nowhere_else(capsys, db_url):
    result = enroll(capsys, db_url)

    _, _, token = issued_from(result)
    assert result.out.count(token) == 1 and token not in result.err
    assert result.out.splitlines()[1].count(token) == 1  # on its own line, after the status line
    assert hash_secret(token) not in result.text  # the digest is not shown either
    assert token not in all_text(db_url)  # nor stored: only the digest is
    assert hash_secret(token) in all_text(db_url)


def test_the_secret_is_printed_only_after_the_transaction_committed(capsys, db_url, monkeypatch):
    events = []
    real_enroll, real_print = enrollment.enroll_user, cli._print_issued

    def enroll_then_note(*args, **kwargs):
        result = real_enroll(*args, **kwargs)
        events.append("committed")
        return result

    def print_then_note(*args, **kwargs):
        events.append("printing")
        return real_print(*args, **kwargs)

    monkeypatch.setattr(enrollment, "enroll_user", enroll_then_note)
    monkeypatch.setattr(cli, "_print_issued", print_then_note)

    assert enroll(capsys, db_url).code == ExitCode.OK

    assert events == ["committed", "printing"]


def test_two_users_are_two_distinct_users_with_two_different_invitations(capsys, db_url):
    first = issued_from(enroll(capsys, db_url, "Elena"))
    second = issued_from(enroll(capsys, db_url, "Мама"))

    assert len({first[0], second[0]}) == 2 and len({first[1], second[1]}) == 2 and first[2] != second[2]
    assert set(users_of(db_url)) == {first[0], second[0]}  # (the same fake clock: no order to assert)
    owners ={row[0]: row[1] for row in rows(db_url, "SELECT id, user_id FROM identity_invitations")}
    assert owners == {uuid.UUID(first[1]).hex: first[0].hex, uuid.UUID(second[1]).hex: second[0].hex}
    # each code opens only its own invitation
    assert verifies(db_url, first[1], first[2]) and verifies(db_url, second[1], second[2])
    assert not verifies(db_url, first[1], second[2]) and not verifies(db_url, second[1], first[2])


def test_a_user_created_here_can_sign_in_nowhere_until_the_invitation_is_claimed(capsys, db_url):
    user_id, invitation_id, token = issued_from(enroll(capsys, db_url))
    engine, factory = open_factory(db_url)
    try:
        with factory() as db, db.begin():
            assert not is_allowed(db, "telegram", SUB) and allowed_user(db, "telegram", SUB) is None
            with pytest.raises(LoginDenied):
                complete_login(db, "telegram", SUB, now=LATER)  # no entry: refused
        with factory() as db, db.begin():
            outcome = issue_email_login(db, "elena@example.com", now=LATER)
            assert outcome.outcome is IssueOutcome.NOT_ELIGIBLE  # and no email way in either
    finally:
        engine.dispose()
    assert counts(db_url)["allowlist_entries"] == 0 and counts(db_url)["sessions"] == 0


# --- the enrollment limit ---------------------------------------------------------------------------------


def test_a_third_user_is_refused_by_the_limit_and_nothing_changes(capsys, db_url):
    enroll(capsys, db_url, "Elena")
    enroll(capsys, db_url, "Мама")
    before = everything(db_url)

    result = enroll(capsys, db_url, "Third")

    assert ENROLLMENT_USER_CAP == 2
    assert result.code == ExitCode.REFUSED and result.out == ""  # no secret, no user
    assert result.err.startswith("refused [enrollment_full]: 2 users already exist")
    assert "temporary policy" in result.err and "not a database rule" in result.err
    assert everything(db_url) == before


def test_the_limit_counts_a_disabled_user(capsys, db_url):
    enroll(capsys, db_url, "Elena")
    enroll(capsys, db_url, "Мама")
    write_sql(db_url, "UPDATE users SET disabled_at = '2026-10-19 11:00:00.000000'")

    assert enroll(capsys, db_url, "Third").err.startswith("refused [enrollment_full]")


def test_the_limit_counts_the_user_that_provision_email_made(capsys, db_url):
    create_first_user(capsys, db_url, "Elena@example.com", "--display-name", "Elena")

    assert enroll(capsys, db_url, "Мама").code == ExitCode.OK  # the second place
    assert enroll(capsys, db_url, "Third").err.startswith("refused [enrollment_full]")
    assert len(users_of(db_url)) == 2


def test_the_limit_is_a_policy_of_the_command_and_not_of_the_database(capsys, db_url):
    first = issued_from(enroll(capsys, db_url, "Elena"))
    second = issued_from(enroll(capsys, db_url, "Мама"))

    with write(db_url) as db:  # other code can still make a third user, and give it conversations
        third = new_user(db, display_name="Third")
        new_conversation(db, third)
    assert len(users_of(db_url)) == 3
    # reissuing is not enrolling: the limit never blocks an invitation for a user that exists
    result = reissue(capsys, db_url, third.id)
    assert result.code == ExitCode.OK and issued_from(result)[0] == third.id
    assert reissue(capsys, db_url, first[0]).code == ExitCode.OK
    assert counts(db_url)["conversations"] == 1  # and nothing here touched the conversations
    assert second[0] in users_of(db_url)


def test_a_repeated_command_is_not_a_second_user_whatever_the_capitals(capsys, db_url):
    first = issued_from(enroll(capsys, db_url, "Мама"))
    before = everything(db_url)

    for name in ("Мама", "мама", "МАМА", "  мама  "):
        result = enroll(capsys, db_url, name)

        assert result.code == ExitCode.REFUSED and result.out == "", name
        assert result.err.startswith(f"refused [display_name_in_use]: user {first[0]} already has")
        assert "reissue-invitation" in result.err
    assert everything(db_url) == before


def test_a_repeated_command_is_reported_as_a_duplicate_even_when_the_limit_is_reached_too(capsys, db_url):
    enroll(capsys, db_url, "Elena")
    enroll(capsys, db_url, "Мама")

    assert enroll(capsys, db_url, "мама").err.startswith("refused [display_name_in_use]")


def test_a_user_without_a_name_collides_with_nobody(capsys, db_url):
    create_first_user(capsys, db_url, "Elena@example.com")  # no display name

    assert enroll(capsys, db_url, "Elena").code == ExitCode.OK


@pytest.mark.parametrize(
    "value",
    ["", "   ", "x" * 201, "Ele\x00na", "Ele\nna", "Ele\u202ena", "Ele\u200bna", "Ele\ud800na", f"{SECRET}\x07"],
)
def test_an_unusable_name_is_a_usage_error_that_is_not_echoed_and_asks_no_database(capsys, db_url, value):
    result = run_admin(capsys, None, "enroll-user", "--display-name", value)  # no database configured

    assert result.code == ExitCode.USAGE and result.out == ""
    assert result.err.startswith("usage [invalid_value]: ")
    assert SECRET not in result.err
    if value.strip():  # (an empty text is "in" every message)
        assert value.strip() not in result.err
    result = run_admin(capsys, db_url, "enroll-user", "--display-name", value)
    assert result.code == ExitCode.USAGE and counts(db_url)["users"] == 0


def test_the_longest_accepted_name_is_200_characters(capsys, db_url):
    assert enroll(capsys, db_url, "я" * 200).code == ExitCode.OK
    assert rows(db_url, "SELECT length(display_name) FROM users") == [(200,)]


# --- rollback ---------------------------------------------------------------------------------------------


def test_a_failure_after_the_writes_rolls_the_user_and_the_invitation_back(capsys, db_url, monkeypatch):
    real = enrollment.invitations.issue_invitation

    def failing(*args, **kwargs):
        real(*args, **kwargs)  # the user and the invitation have been written (flushed)
        raise RuntimeError(f"SECRET text of the error {SECRET}")

    monkeypatch.setattr(enrollment.invitations, "issue_invitation", failing)
    before = everything(db_url)

    result = enroll(capsys, db_url, "Елена")

    assert result.code == ExitCode.INTERNAL and result.out == ""  # no secret was printed
    assert result.err.startswith("error [internal]: unexpected RuntimeError. ")
    for leaked in (SECRET, "SECRET text", "Traceback", "Елена"):
        assert leaked not in result.text
    assert everything(db_url) == before


def test_a_digest_collision_on_the_second_enrollment_rolls_that_user_back_whole(capsys, db_url, monkeypatch):
    monkeypatch.setattr(invitations_module, "new_secret", lambda nbytes: "A" * 43)
    first = enroll(capsys, db_url, "Elena")
    assert first.code == ExitCode.OK
    before = everything(db_url)

    second = enroll(capsys, db_url, "Мама")  # the unique digest refuses it, after the user was written

    assert second.code == ExitCode.INTERNAL and second.out == ""
    assert second.err.startswith("error [internal]: unexpected IntegrityError. ")
    assert everything(db_url) == before  # the second user is not there: no user without an invitation
    assert len(users_of(db_url)) == 1


# --- reissue-invitation -----------------------------------------------------------------------------------


def test_reissuing_replaces_the_outstanding_invitation_in_one_transaction(capsys, db_url):
    user_id, old_id, old_token = issued_from(enroll(capsys, db_url))

    result = reissue(capsys, db_url, user_id)

    same_user, new_id, new_token = issued_from(result)
    assert same_user == user_id and new_id != old_id and new_token != old_token
    assert result.out.startswith(f"ok [reissued]: issued a new Telegram invitation for user {user_id}")
    assert "1 earlier outstanding invitation(s) revoked" in result.out
    assert "valid until 2026-10-19T14:05:00Z" in result.out  # two hours from the clock of this run
    assert [row[0] for row in outstanding(db_url, user_id)] == [uuid.UUID(new_id).hex]
    assert len(invitations_of(db_url)) == 2
    assert not verifies(db_url, old_id, old_token) and verifies(db_url, new_id, new_token)


def test_the_first_invitation_of_a_user_that_has_none_says_so(capsys, db_url):
    create_first_user(capsys, db_url, "Elena@example.com", "--display-name", "Elena")
    user_id = users_of(db_url)[0]
    entry_before = entry_of(db_url, "Elena@example.com")

    result = reissue(capsys, db_url, user_id)

    assert issued_from(result)[0] == user_id
    assert "No earlier invitation was outstanding" in result.out
    assert entry_of(db_url, "Elena@example.com") == entry_before  # the email identity is untouched
    assert counts(db_url)["users"] == 1


def test_a_user_linked_by_invitation_keeps_the_email_identity_next_to_the_telegram_one(capsys, db_url):
    create_first_user(capsys, db_url, "Elena@example.com")
    user_id = users_of(db_url)[0]
    reissue(capsys, db_url, user_id)

    link_in(db_url, user_id, SUB)

    providers = rows(db_url, "SELECT provider, user_id FROM allowlist_entries ORDER BY provider")
    assert providers == [("email", user_id.hex), ("telegram", user_id.hex)]  # one user, two ways in


def test_an_expired_invitation_is_replaced_and_the_new_one_runs_from_the_new_clock(capsys, db_url):
    user_id, old_id, old_token = issued_from(enroll(capsys, db_url, now=NOW))
    later = NOW + timedelta(hours=3)

    result = reissue(capsys, db_url, user_id, now=later)

    _, new_id, new_token = issued_from(result)
    assert "1 earlier outstanding invitation(s) revoked" in result.out
    assert verifies(db_url, new_id, new_token, now=later + timedelta(hours=1))
    assert not verifies(db_url, new_id, new_token, now=later + TWO_HOURS)
    assert not verifies(db_url, old_id, old_token, now=NOW + timedelta(minutes=1))  # it is dead for good


def test_reissuing_for_one_user_leaves_the_other_users_invitation_alone(capsys, db_url):
    first = issued_from(enroll(capsys, db_url, "Elena"))
    second = issued_from(enroll(capsys, db_url, "Мама"))

    reissue(capsys, db_url, first[0])

    assert verifies(db_url, second[1], second[2])
    assert [row[0] for row in outstanding(db_url, second[0])] == [uuid.UUID(second[1]).hex]


def test_a_failure_while_reissuing_puts_the_earlier_invitation_back(capsys, db_url, monkeypatch):
    user_id, old_id, old_token = issued_from(enroll(capsys, db_url))
    before = everything(db_url)

    def failing(nbytes):
        raise RuntimeError(SECRET)  # after the earlier invitation was revoked, before the new one exists

    monkeypatch.setattr(invitations_module, "new_secret", failing)
    result = reissue(capsys, db_url, user_id)

    assert result.code == ExitCode.INTERNAL and result.out == "" and SECRET not in result.text
    assert everything(db_url) == before
    assert verifies(db_url, old_id, old_token)  # the person still has a working invitation


@pytest.mark.parametrize("scenario", ["unknown", "disabled", "telegram", "telegram_revoked"])
def test_reissuing_refuses_where_an_invitation_would_be_wrong_and_changes_nothing(capsys, db_url, scenario):
    user_id, _, _ = issued_from(enroll(capsys, db_url))
    target = user_id
    if scenario == "unknown":
        target = uuid.uuid4()
    elif scenario == "disabled":
        write_sql(db_url, "UPDATE users SET disabled_at = '2026-10-19 11:00:00.000000'")
    else:
        with write(db_url) as db:
            allow(db, db.get(User, user_id), "telegram", SUB)
            if scenario == "telegram_revoked":
                revoke(db, "telegram", SUB, now=NOW)
    code = {
        "unknown": "unknown_user",
        "disabled": "user_disabled",
        "telegram": "telegram_already_provisioned",
        "telegram_revoked": "telegram_already_provisioned",  # taken away on purpose: not a way around
    }[scenario]
    before = everything(db_url)

    result = reissue(capsys, db_url, target)

    assert result.code == ExitCode.REFUSED and result.out == ""  # no invitation, so no secret
    assert result.err.startswith(f"refused [{code}]: ")
    assert everything(db_url) == before


@pytest.mark.parametrize(
    "value",
    ["", "abc", uuid.uuid4().hex, f"{{{uuid.uuid4()}}}", f"urn:uuid:{uuid.uuid4()}", f"{uuid.uuid4()} ", SECRET],
)
def test_a_malformed_user_id_is_a_usage_error_that_is_not_echoed_and_asks_no_database(capsys, value):
    for command in ("reissue-invitation", "revoke-invitation"):
        result = run_admin(capsys, None, command, "--user-id", value)

        assert result.code == ExitCode.USAGE and result.out == ""
        assert result.err.startswith("usage [invalid_value]: the user id is not valid")
        assert SECRET not in result.err
        if value.strip():  # (an empty text is "in" every message)
            assert value.strip() not in result.err


def test_an_upper_case_user_id_is_accepted(capsys, db_url):
    user_id, _, _ = issued_from(enroll(capsys, db_url))

    result = run_admin(capsys, db_url, "reissue-invitation", "--user-id", str(user_id).upper(), clock=at(LATER))

    assert issued_from(result)[0] == user_id


# --- revoke-invitation ------------------------------------------------------------------------------------


def test_revoking_withdraws_the_invitation_for_good_and_repeating_changes_nothing(capsys, db_url):
    user_id, invitation_id, token = issued_from(enroll(capsys, db_url))

    result = revoke_it(capsys, db_url, user_id)

    assert result.code == ExitCode.OK and result.err == ""
    assert result.out == f"ok [revoked]: revoked the outstanding invitation of user {user_id}.\n"
    assert not verifies(db_url, invitation_id, token)
    assert outstanding(db_url, user_id) == []
    after_first = everything(db_url)

    again = revoke_it(capsys, db_url, user_id, now=LATER + timedelta(hours=1))

    assert again.code == ExitCode.OK and again.out.startswith("ok [unchanged]: ")
    assert everything(db_url) == after_first  # not even the first revocation time moved


def test_a_revoked_invitation_cannot_be_claimed_and_a_reissue_is_a_new_one(capsys, db_url):
    user_id, old_id, old_token = issued_from(enroll(capsys, db_url))
    revoke_it(capsys, db_url, user_id)

    engine, factory = open_factory(db_url)
    try:
        with factory() as db, db.begin():
            assert claim_invitation(db, old_id, SUB, now=LATER) is None
    finally:
        engine.dispose()
    _, new_id, new_token = issued_from(reissue(capsys, db_url, user_id, now=LATER + timedelta(minutes=1)))
    assert new_id != old_id and verifies(db_url, new_id, new_token, now=LATER + timedelta(minutes=2))
    assert not verifies(db_url, old_id, old_token)


def test_revoking_does_not_touch_a_used_invitation(capsys, db_url):
    user_id, invitation_id, _ = issued_from(enroll(capsys, db_url))
    link_in(db_url, user_id, SUB)
    before = everything(db_url)

    result = revoke_it(capsys, db_url, user_id)

    assert result.out.startswith("ok [unchanged]: ") and everything(db_url) == before


@pytest.mark.parametrize(
    "offset, outcome",
    [
        (timedelta(minutes=5), "revoked"),
        (TWO_HOURS - timedelta(microseconds=1), "revoked"),  # the last instant it is still valid
        (TWO_HOURS, "unchanged"),  # exclusive: at expires_at it is over, as for verify and claim
        (TWO_HOURS + timedelta(microseconds=1), "unchanged"),
        (timedelta(hours=30), "unchanged"),
    ],
    ids=["before", "last-instant", "boundary", "just-after", "long-after"],
)
def test_revoking_withdraws_an_invitation_only_while_it_is_valid(capsys, db_url, offset, outcome):
    user_id, invitation_id, token = issued_from(enroll(capsys, db_url, now=NOW))
    before = everything(db_url)

    result = revoke_it(capsys, db_url, user_id, now=NOW + offset)

    assert result.code == ExitCode.OK and result.err == ""
    assert result.out.startswith(f"ok [{outcome}]: ")
    if outcome == "revoked":
        assert rows(db_url, "SELECT revoked_at FROM identity_invitations") == [(stored(NOW + offset),)]
    else:
        assert "has no active invitation" in result.out and "Nothing was changed" in result.out
        assert everything(db_url) == before  # an expired one is not touched: no revoked_at, no change


def test_an_expired_invitation_stays_expired_for_the_report_when_revoking_is_attempted(capsys, db_url):
    user_id, _, _ = issued_from(enroll(capsys, db_url, now=NOW))
    later = NOW + timedelta(hours=3)

    assert revoke_it(capsys, db_url, user_id, now=later).out.startswith("ok [unchanged]: ")

    line = line_of(status(capsys, db_url, now=later), user_id)
    assert "invitation: expired (ended 2026-10-19T14:00:00Z)" in line  # not "revoked"


def test_reissuing_still_supersedes_an_expired_invitation_that_revoking_left_alone(capsys, db_url):
    # The partial unique index allows one outstanding row per user, and an expired one that was
    # never used or withdrawn is outstanding: only the reissue may take it out of the way.
    user_id, old_id, old_token = issued_from(enroll(capsys, db_url, now=NOW))
    later = NOW + timedelta(hours=3)
    revoke_it(capsys, db_url, user_id, now=later)
    assert [row[0] for row in outstanding(db_url, user_id)] == [uuid.UUID(old_id).hex]

    result = reissue(capsys, db_url, user_id, now=later)

    _, new_id, new_token = issued_from(result)
    assert "1 earlier outstanding invitation(s) revoked" in result.out
    assert rows(db_url, "SELECT revoked_at FROM identity_invitations WHERE id = :id", id=uuid.UUID(old_id).hex) == [
        (stored(later),)
    ]
    assert [row[0] for row in outstanding(db_url, user_id)] == [uuid.UUID(new_id).hex]
    assert verifies(db_url, new_id, new_token, now=later + timedelta(minutes=1))
    assert not verifies(db_url, old_id, old_token, now=NOW + timedelta(minutes=1))


def test_revoking_for_an_unknown_user_is_refused(capsys, db_url):
    result = revoke_it(capsys, db_url, uuid.uuid4())

    assert result.code == ExitCode.REFUSED and result.out == ""
    assert result.err.startswith("refused [unknown_user]: ")


def test_revoking_one_users_invitation_leaves_the_other_alone(capsys, db_url):
    first = issued_from(enroll(capsys, db_url, "Elena"))
    second = issued_from(enroll(capsys, db_url, "Мама"))

    revoke_it(capsys, db_url, first[0])

    assert not verifies(db_url, first[1], first[2]) and verifies(db_url, second[1], second[2])


# --- enrollment-status ------------------------------------------------------------------------------------

BSUB, FSUB = "5550000000000000001", "5550000000000000006"
MAIL = "mail-only@example.com"


def build_states(url):
    """Six users in six situations (past the command's own limit, on purpose: the report is for
    whatever the database holds). Returns their ids by name."""
    ids = {}
    with write(url) as db:
        active = new_user(db, display_name="Active")
        issue_invitation(db, active.id, now=NOW)
        linked = new_user(db, display_name="Linked")
        claim_invitation(db, str(issue_invitation(db, linked.id, now=NOW).invitation_id), BSUB, now=NOW + timedelta(minutes=1))
        withdrawn = new_user(db, display_name="Withdrawn")
        issue_invitation(db, withdrawn.id, now=NOW)
        revoke_invitations(db, withdrawn.id, now=NOW + timedelta(minutes=1))
        lapsed = new_user(db, display_name="Lapsed")
        issue_invitation(db, lapsed.id, now=NOW - timedelta(hours=3))
        bare = new_user(db, display_name="Bare")
        allow(db, bare, "email", MAIL)
        gone = new_user(db, display_name="Gone")
        claim_invitation(db, str(issue_invitation(db, gone.id, now=NOW).invitation_id), FSUB, now=NOW + timedelta(minutes=1))
        revoke(db, "telegram", FSUB, now=NOW + timedelta(minutes=2))
        gone.disabled_at = NOW
        ids = {u.display_name: u.id for u in (active, linked, withdrawn, lapsed, bare, gone)}
    return ids


def line_of(result, user_id):
    (line,) = [line for line in result.out.splitlines() if f"user {user_id} " in line]
    return line


def test_the_report_says_where_every_user_stands(capsys, db_url):
    ids = build_states(db_url)

    result = status(capsys, db_url)

    assert result.code == ExitCode.OK and result.err == ""
    assert result.out.splitlines()[0].startswith("ok [status]: 6 user(s). The enrollment limit is 2 users")
    expected = {
        "Active": ("enabled", "telegram: not linked", "invitation: active (until 2026-10-19T14:00:00Z)"),
        "Linked": ("enabled", "telegram: linked", "invitation: used (used 2026-10-19T12:01:00Z)"),
        "Withdrawn": ("enabled", "telegram: not linked", "invitation: revoked (revoked 2026-10-19T12:01:00Z)"),
        "Lapsed": ("enabled", "telegram: not linked", "invitation: expired (ended 2026-10-19T11:00:00Z)"),
        "Bare": ("enabled", "telegram: not linked", "invitation: none"),
        "Gone": ("disabled", "telegram: revoked", "invitation: used (used 2026-10-19T12:01:00Z)"),
    }
    assert len(result.out.splitlines()) == 7
    for name, parts in expected.items():
        line = line_of(result, ids[name])
        assert f'name "{name}"' in line
        for part in parts:
            assert part in line, (name, part)


def test_the_report_shows_the_latest_invitation_of_a_user_who_has_had_several(capsys, db_url):
    user_id, _, _ = issued_from(enroll(capsys, db_url, now=NOW))
    reissue(capsys, db_url, user_id, now=LATER)  # the first one is revoked, the second is the user's now

    result = status(capsys, db_url, now=LATER + timedelta(minutes=1))

    line = line_of(result, user_id)
    assert "invitation: active (until 2026-10-19T14:05:00Z)" in line
    assert "revoked" not in line  # the superseded one is history, not the user's state


def test_the_report_names_the_usable_invitation_after_reissues_whose_clocks_ran_backwards(capsys, db_url):
    # Two reissues that read the clock before the lock: the one that took the lock first had the
    # LATER reading, so the invitation created last carries the EARLIER created_at, and the one
    # it revoked (revoked_at earlier than its own created_at) carries the later.
    user_id, _, _ = issued_from(enroll(capsys, db_url, now=NOW))
    reissue(capsys, db_url, user_id, now=NOW + timedelta(minutes=10))
    last = issued_from(reissue(capsys, db_url, user_id, now=NOW + timedelta(minutes=5)))
    assert len(outstanding(db_url, user_id)) == 1
    created = rows(db_url, "SELECT created_at, revoked_at FROM identity_invitations ORDER BY created_at")
    assert created[-1] == (stored(NOW + timedelta(minutes=10)), stored(NOW + timedelta(minutes=5)))  # revoked, newest

    result = status(capsys, db_url, now=NOW + timedelta(minutes=15))

    line = line_of(result, user_id)
    assert "invitation: active (until 2026-10-19T14:05:00Z)" in line and "revoked" not in line
    assert verifies(db_url, last[1], last[2], now=NOW + timedelta(minutes=15))  # the invitation IS usable


@pytest.mark.parametrize(
    "revoked_id, active_id", [(FIRST_ID, LAST_ID), (LAST_ID, FIRST_ID)], ids=["revoked-id-first", "active-id-first"]
)
def test_equal_issue_times_do_not_let_the_ids_decide_whether_the_usable_invitation_is_reported(
    capsys, db_url, revoked_id, active_id
):
    user_id = user_without_invitation(db_url)
    insert_invitation(db_url, user_id, created=NOW, revoked=LATER, invitation_id=revoked_id)
    insert_invitation(db_url, user_id, created=NOW, invitation_id=active_id)

    result = status(capsys, db_url, now=LATER)

    assert "invitation: active (until 2026-10-19T14:00:00Z)" in line_of(result, user_id)


@pytest.mark.parametrize(
    "revoked_created",
    [NOW - timedelta(minutes=10), NOW, NOW + timedelta(minutes=10)],
    ids=["older", "same-time", "newer"],
)
def test_an_active_invitation_outweighs_a_revoked_one_whenever_the_revoked_one_was_created(
    capsys, db_url, revoked_created
):
    user_id = user_without_invitation(db_url)
    insert_invitation(db_url, user_id, created=revoked_created, revoked=NOW + timedelta(minutes=30))
    insert_invitation(db_url, user_id, created=NOW)

    result = status(capsys, db_url, now=NOW + timedelta(minutes=45))

    line = line_of(result, user_id)
    assert "invitation: active (until 2026-10-19T14:00:00Z)" in line and "revoked" not in line


def test_the_same_invitation_is_reported_as_expired_once_its_two_hours_are_over(capsys, db_url):
    user_id = user_without_invitation(db_url)
    insert_invitation(db_url, user_id, created=NOW - timedelta(minutes=10), revoked=NOW)  # superseded, then
    insert_invitation(db_url, user_id, created=NOW)

    last_instant = status(capsys, db_url, now=NOW + TWO_HOURS - timedelta(microseconds=1))
    over = status(capsys, db_url, now=NOW + TWO_HOURS)  # exclusive

    assert "invitation: active (until 2026-10-19T14:00:00Z)" in line_of(last_instant, user_id)
    assert "invitation: expired (ended 2026-10-19T14:00:00Z)" in line_of(over, user_id)


def test_an_expired_unused_invitation_is_reported_with_its_expiry_next_to_older_revoked_ones(capsys, db_url):
    user_id = user_without_invitation(db_url)
    insert_invitation(db_url, user_id, created=NOW - timedelta(hours=5), revoked=NOW - timedelta(hours=4))
    insert_invitation(db_url, user_id, created=NOW - timedelta(hours=4), revoked=NOW - timedelta(hours=3))
    insert_invitation(db_url, user_id, created=NOW - timedelta(hours=3))  # outstanding, over at 11:00

    result = status(capsys, db_url)

    assert "invitation: expired (ended 2026-10-19T11:00:00Z)" in line_of(result, user_id)


def test_a_used_invitation_is_reported_as_used_next_to_older_revoked_ones(capsys, db_url):
    user_id = user_without_invitation(db_url)
    insert_invitation(db_url, user_id, created=NOW - timedelta(hours=1), revoked=NOW)
    insert_invitation(db_url, user_id, created=NOW, consumed=NOW + timedelta(minutes=1))

    result = status(capsys, db_url, now=LATER)

    assert "invitation: used (used 2026-10-19T12:01:00Z)" in line_of(result, user_id)


@pytest.mark.parametrize("used_id, revoked_id", [(FIRST_ID, LAST_ID), (LAST_ID, FIRST_ID)])
def test_equal_event_times_in_the_history_are_reported_the_same_whatever_the_ids(
    capsys, db_url, used_id, revoked_id
):
    user_id = user_without_invitation(db_url)
    insert_invitation(db_url, user_id, created=NOW - timedelta(hours=1), revoked=NOW, invitation_id=revoked_id)
    insert_invitation(db_url, user_id, created=NOW, consumed=NOW, invitation_id=used_id)

    result = status(capsys, db_url, now=LATER)

    assert "invitation: used (used 2026-10-19T12:00:00Z)" in line_of(result, user_id)


def row_of(created, *, consumed=None, revoked=None, expires=None):
    return SimpleNamespace(
        expires_at=expires or created + TWO_HOURS, consumed_at=consumed, revoked_at=revoked
    )


def reported(rows_, now):
    summary = enrollment._latest_invitation(rows_, now)
    return None if summary is None else (summary.state.value, summary.at)


HOUR = timedelta(hours=1)
HISTORIES = {
    # name: (rows, now, what the report says)
    "revoked-then-revoked-later": (
        [row_of(NOW, revoked=NOW + HOUR), row_of(NOW + 2 * HOUR, revoked=NOW + 3 * HOUR)],
        NOW + 9 * HOUR,
        ("revoked", NOW + 3 * HOUR),
    ),
    "the-later-event-wins-not-the-later-creation": (  # reversed clocks: created last, ended first
        [row_of(NOW + HOUR, revoked=NOW), row_of(NOW, revoked=NOW + 2 * HOUR)],
        NOW + 9 * HOUR,
        ("revoked", NOW + 2 * HOUR),
    ),
    "used-after-revoked": (
        [row_of(NOW, revoked=NOW + HOUR), row_of(NOW + HOUR, consumed=NOW + 2 * HOUR)],
        NOW + 9 * HOUR,
        ("used", NOW + 2 * HOUR),
    ),
    "equal-times-used-beats-revoked-beats-expired": (
        [
            row_of(NOW, revoked=NOW + HOUR),
            row_of(NOW, consumed=NOW + HOUR),
            row_of(NOW, expires=NOW + HOUR),
        ],
        NOW + 9 * HOUR,
        ("used", NOW + HOUR),
    ),
    "equal-times-revoked-beats-expired": (
        [row_of(NOW - HOUR, expires=NOW + HOUR), row_of(NOW, revoked=NOW + HOUR)],
        NOW + 9 * HOUR,
        ("revoked", NOW + HOUR),
    ),
    "expired-after-an-older-revoked": (
        [row_of(NOW, revoked=NOW + HOUR), row_of(NOW + HOUR)],
        NOW + 9 * HOUR,
        ("expired", NOW + 3 * HOUR),
    ),
    "the-usable-one-outweighs-every-later-event": (
        [
            row_of(NOW, revoked=NOW + 5 * HOUR),
            row_of(NOW + HOUR, consumed=NOW + 6 * HOUR),
            row_of(NOW - 2 * HOUR, expires=NOW + 3 * HOUR - timedelta(microseconds=1)),
            row_of(NOW + 4 * HOUR),
        ],
        NOW + 5 * HOUR,
        ("active", NOW + 6 * HOUR),
    ),
}


@pytest.mark.parametrize("name", HISTORIES)
def test_the_reported_invitation_does_not_depend_on_the_order_the_rows_arrive_in(name):
    history, now, expected = HISTORIES[name]

    for arrangement in itertools.permutations(history):
        assert reported(arrangement, now) == expected, name


def test_a_user_without_any_invitation_has_nothing_to_report():
    assert reported([], NOW) is None


# --- the clock is read once the lock is held ----------------------------------------------------------------


@pytest.mark.parametrize("command", ["enroll", "reissue", "revoke"])
def test_a_mutation_reads_the_clock_only_after_the_lock_is_held(capsys, db_url, monkeypatch, command):
    user_id, _, _ = issued_from(enroll(capsys, db_url, "Elena"))
    events = []
    real = provisioning._serialize
    monkeypatch.setattr(provisioning, "_serialize", lambda session: (events.append("lock"), real(session)))

    def clock():
        events.append("clock")
        return LATER

    argv = {
        "enroll": ["enroll-user", "--display-name", "Мама"],
        "reissue": ["reissue-invitation", "--user-id", str(user_id)],
        "revoke": ["revoke-invitation", "--user-id", str(user_id)],
    }[command]

    result = run_admin(capsys, db_url, *argv, clock=clock)

    assert result.code == ExitCode.OK, result.text
    assert events == ["lock", "clock"]  # once, and behind the lock: a later lock holder reads a later time


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_reissues_racing_for_the_lock_are_stamped_in_the_order_they_take_it(tmp_path, migrated_template, round_number):
    url = fresh_database(tmp_path, migrated_template, "stamped.db")
    first = enroll_now(url, "Elena")
    ticks = iter(range(1, 100))
    guard = threading.Lock()

    def clock():  # a clock that only ever moves forward, one second per reading
        with guard:
            return NOW + timedelta(seconds=next(ticks))

    results, errors = run_all_at_once(
        url, [lambda factory: enrollment.reissue_invitation(factory, first.user_id, now=clock) for _ in range(6)]
    )

    assert errors == [] and len(results) == 6
    chain = rows(url, "SELECT created_at, revoked_at FROM identity_invitations ORDER BY created_at")
    assert len(chain) == 7
    # each invitation was revoked by the next one, and at the moment that one was created
    assert [revoked for _, revoked in chain[:-1]] == [created for created, _ in chain[1:]]
    assert chain[-1][1] is None and len(outstanding(url, first.user_id)) == 1
    engine, factory = open_admin_factory(url)
    try:
        (user,) = enrollment.enrollment_status(factory, now=NOW + timedelta(minutes=10)).users
    finally:
        engine.dispose()
    assert user.invitation.state is invitations_module.InvitationState.ACTIVE


@pytest.mark.parametrize("active_first", [True, False])
def test_a_user_with_an_active_and_a_revoked_telegram_entry_is_linked_whatever_the_row_order(
    capsys, db_url, active_first
):
    with write(db_url) as db:
        user = new_user(db, display_name="Both")
        subjects = ["111", "222"] if active_first else ["222", "111"]
        for subject in subjects:
            allow(db, user, "telegram", subject)
        revoke(db, "telegram", "222", now=NOW)  # "111" stays active, whichever was added first

    result = status(capsys, db_url)

    assert "telegram: linked" in line_of(result, user.id)


def test_the_report_of_an_empty_database(capsys, db_url):
    result = status(capsys, db_url)

    assert result.code == ExitCode.OK and result.out.startswith("ok [status]: 0 user(s).")
    assert len(result.out.splitlines()) == 1


def test_the_report_shows_no_telegram_id_digest_secret_or_email_address(capsys, db_url):
    ids = build_states(db_url)
    stored_digests = [row[3] for row in invitations_of(db_url)]

    result = status(capsys, db_url)

    assert result.code == ExitCode.OK
    for secret in (BSUB, FSUB, MAIL, *stored_digests):
        assert secret not in result.text
    for name in ("subject", "token_hash", "digest"):
        assert f"{name}:" not in result.out
    assert "No Telegram id, secret or digest is shown" in result.out
    assert all(str(identifier) in result.out for identifier in ids.values())  # the ids are


def test_the_report_never_selects_a_subject_or_a_digest(capsys, db_url):
    build_states(db_url)
    engine, factory = open_admin_factory(db_url)
    try:
        with statements(engine) as seen:
            report = enrollment.enrollment_status(factory, now=NOW)
    finally:
        engine.dispose()

    assert len(report.users) == 6
    assert seen and all(text.lstrip().upper().startswith("SELECT") for text in seen)  # reads only
    joined = "\n".join(seen).lower()
    for forbidden in ("subject", "token_hash", "email"):
        assert forbidden not in joined


def test_the_report_changes_nothing_not_even_a_file(capsys, db_url, db_path):
    build_states(db_url)
    set_journal_mode(db_path, "delete")  # reading a WAL database would create its -wal and -shm
    before = snapshot(db_path.parent)

    result = status(capsys, db_url)

    assert result.code == ExitCode.OK
    assert snapshot(db_path.parent) == before
    assert journal_mode(db_path) == "delete"


def test_the_report_covers_more_users_than_the_limit(capsys, db_url):
    with write(db_url) as db:
        for number in range(4):
            new_user(db, display_name=f"User {number}")

    result = status(capsys, db_url)

    assert result.out.startswith("ok [status]: 4 user(s).") and len(result.out.splitlines()) == 5


def test_a_stored_name_cannot_forge_a_line_of_the_report(capsys, db_url):
    with write(db_url) as db:
        new_user(db, display_name='x" telegram: linked; invitation: used\nuser fake name "y')
        new_user(db, display_name="bell\x07 and escape\x1b[31m")

    result = status(capsys, db_url)

    lines = result.out.splitlines()
    assert len(lines) == 3  # the header and one line per user: the newline did not split one
    assert "\x07" not in result.out and "\x1b" not in result.out
    # whatever the name says, the real fields come after it and say what the database holds
    assert all(line.endswith("; telegram: not linked; invitation: none") for line in lines[1:])
    assert '\\"' in result.out  # the quote inside the name is escaped


def test_a_display_name_is_shown_in_a_form_the_output_stream_can_carry(monkeypatch):
    legacy = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", legacy)
    assert cli._shown("Мама") == '"\\u041c\\u0430\\u043c\\u0430"'  # escapes, not a crash that ends the report
    assert cli._shown("Zoë") == '"Zoë"'  # what the code page has, stays
    unicode_console = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", unicode_console)
    assert cli._shown("Мама") == '"Мама"'
    assert cli._shown(None) == "(none)"


# --- what is left as it was -------------------------------------------------------------------------------


def test_provision_email_still_refuses_to_make_a_second_user_after_an_enrollment(capsys, db_url):
    enroll(capsys, db_url, "Elena")
    before = everything(db_url)

    one_user = provision(capsys, db_url, "Mama@example.com")

    assert one_user.code == ExitCode.REFUSED and one_user.err.startswith("refused [user_exists]")
    enroll(capsys, db_url, "Мама")
    before = everything(db_url)
    two_users = provision(capsys, db_url, "Mama@example.com")
    assert two_users.code == ExitCode.REFUSED and two_users.err.startswith("refused [multiple_users]")
    assert everything(db_url) == before


def test_provision_email_alone_behaves_as_before_on_the_new_schema(capsys, db_url):
    first = provision(capsys, db_url, "Elena@example.com", "--display-name", "Elena")
    again = provision(capsys, db_url, "Elena@example.com")

    assert first.code == ExitCode.OK and first.out.startswith("ok [created]: ")
    assert again.code == ExitCode.OK and again.out.startswith("ok [unchanged]: ")
    assert counts(db_url)["users"] == 1 and counts(db_url)["identity_invitations"] == 0


def test_two_enrolled_users_have_independent_conversations(capsys, db_url):
    first = issued_from(enroll(capsys, db_url, "Elena"))
    second = issued_from(enroll(capsys, db_url, "Мама"))
    sessions = {
        "first": link_in(db_url, first[0], SUB),
        "second": link_in(db_url, second[0], OTHER_SUB),
    }

    with write(db_url) as db:
        chats = {
            "first": cv.create_conversation(db, first[0], now=LATER).id,
            "second": cv.create_conversation(db, second[0], now=LATER).id,
        }
        new_message(db, db.get(cv.Conversation, chats["first"]), content="Elena's question")
        new_message(db, db.get(cv.Conversation, chats["second"]), content="Mama's question")

    engine, factory = open_factory(db_url)
    try:
        with factory() as db:
            who = {name: authenticate(db, issued.token, now=LATER).user_id for name, issued in sessions.items()}
            assert who == {"first": first[0], "second": second[0]} and first[0] != second[0]
            assert [c.id for c in cv.list_conversations(db, first[0], limit=10).conversations] == [chats["first"]]
            assert [c.id for c in cv.list_conversations(db, second[0], limit=10).conversations] == [chats["second"]]
            assert cv.owns_conversation(db, first[0], chats["first"])
            assert not cv.owns_conversation(db, first[0], chats["second"])  # not the other's, not told apart
            assert not cv.owns_conversation(db, second[0], chats["first"])
            with pytest.raises(cv.ConversationNotFound):
                cv.list_messages(db, first[0], chats["second"], now=LATER, limit=10)
            texts = [m.text for m in cv.list_messages(db, second[0], chats["second"], now=LATER, limit=10).messages]
            assert texts == ["Mama's question"]
    finally:
        engine.dispose()
    # each identity belongs to its own user and to nobody else
    assert rows(db_url, "SELECT subject, user_id FROM allowlist_entries ORDER BY subject") == sorted(
        [(SUB, first[0].hex), (OTHER_SUB, second[0].hex)]
    )


def test_the_owners_telegram_account_cannot_be_linked_to_the_mothers_user(capsys, db_url):
    first = issued_from(enroll(capsys, db_url, "Elena"))
    second = issued_from(enroll(capsys, db_url, "Мама"))
    link_in(db_url, first[0], SUB)  # the owner links their own account

    engine, factory = open_factory(db_url)
    try:
        with factory() as db:  # ... and then opens the mother's link in the same Telegram session
            with pytest.raises(invitations_module.IdentityRefused):
                claim_invitation(db, second[1], SUB, now=LATER + timedelta(minutes=1))
    finally:
        engine.dispose()

    assert verifies(db_url, second[1], second[2], now=LATER + timedelta(minutes=1))  # still hers to use
    assert rows(db_url, "SELECT user_id FROM allowlist_entries") == [(first[0].hex,)]


# --- the first statement and the lock ---------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["enroll", "reissue", "revoke"])
def test_on_sqlite_every_mutation_begins_with_the_write_that_matches_no_row(capsys, db_url, operation):
    user_id, _, _ = issued_from(enroll(capsys, db_url, "Elena"))
    engine, factory = open_admin_factory(db_url)
    try:
        with statements(engine) as seen:
            if operation == "enroll":
                enrollment.enroll_user(factory, "Мама", now=LATER)
            elif operation == "reissue":
                enrollment.reissue_invitation(factory, user_id, now=LATER)
            else:
                enrollment.revoke_invitation(factory, user_id, now=LATER)
    finally:
        engine.dispose()

    assert seen[0].lstrip().upper().startswith("UPDATE USERS")  # provision-email's own first statement
    assert "WHERE 0 = 1" in seen[0]


def test_every_mutation_serialises_through_provision_emails_lock_before_it_looks_at_anything(monkeypatch):
    calls = []
    monkeypatch.setattr(provisioning, "_serialize", lambda session: calls.append("serialize"))
    monkeypatch.setattr(enrollment, "check_schema_at_head", lambda connection: calls.append("schema"))
    stub = StubSession("sqlite")

    enrollment._begin(stub)

    assert calls == ["serialize", "schema"] and stub.calls == [("connection", None)]


def test_on_postgresql_the_lock_is_provision_emails_advisory_lock_at_read_committed(monkeypatch):
    monkeypatch.setattr(enrollment, "check_schema_at_head", lambda connection: None)
    stub = StubSession("postgresql")

    enrollment._begin(stub)

    assert stub.calls[0] == ("connection", {"isolation_level": "READ COMMITTED"})
    compiled = stub.calls[1][1].compile(dialect=postgresql.dialect())
    assert "pg_advisory_xact_lock(" in str(compiled)
    # the same key as provision-email's, so that the two cannot both create "the first user"
    assert list(compiled.params.values()) == [ADVISORY_LOCK_KEY] == [815883989575170308]


def test_the_report_is_not_serialised_and_takes_no_write(capsys, db_url):
    build_states(db_url)
    engine, factory = open_admin_factory(db_url)
    try:
        with statements(engine) as seen:
            enrollment.enrollment_status(factory, now=NOW)
    finally:
        engine.dispose()

    assert not any("UPDATE" in text.upper() for text in seen)


# --- concurrency (SQLite file; one engine and one transaction per thread) ---------------------------------


def run_all_at_once(url, jobs):
    """Each job(factory) on an engine of its own, as separate processes would have, all released
    at the same moment. Returns the results and the errors."""
    barrier = threading.Barrier(len(jobs))

    def one(job):
        engine, factory = open_admin_factory(url)
        try:
            barrier.wait(timeout=30)
            return job(factory)
        finally:
            engine.dispose()

    results, errors = [], []
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(one, job) for job in jobs]
        for future in futures:
            try:
                results.append(future.result(timeout=120))
            except Exception as error:  # collected so the test can say what happened
                errors.append(error)
    return results, errors


def enroll_now(url, name, now=NOW):
    engine, factory = open_admin_factory(url)
    try:
        return enrollment.enroll_user(factory, name, now=now)
    finally:
        engine.dispose()


def fresh_database(tmp_path, migrated_template, name):
    path = tmp_path / name
    shutil.copyfile(migrated_template, path)
    return f"sqlite:///{path}"


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_concurrent_enrollments_with_different_names_create_exactly_two_users(
    tmp_path, migrated_template, round_number
):
    url = fresh_database(tmp_path, migrated_template, "race.db")
    names = [f"User number {number}" for number in range(6)]

    results, errors = run_all_at_once(
        url, [lambda factory, n=n: enrollment.enroll_user(factory, n, now=NOW) for n in names]
    )

    # every run ended in a decision: created, or refused for want of a place; none failed
    # with "database is locked" (what a transaction that reads before it writes gets)
    assert [type(error) for error in errors] == [EnrollmentRefusal] * 4, errors
    assert all(error.reason is EnrollmentRefusalReason.ENROLLMENT_FULL for error in errors)
    assert len(results) == ENROLLMENT_USER_CAP
    counts_now = counts(url)
    assert counts_now["users"] == 2 and counts_now["identity_invitations"] == 2
    assert {item.user_id for item in results} == set(users_of(url))
    for item in results:  # each of the two has exactly one outstanding invitation of its own
        assert [row[0] for row in outstanding(url, item.user_id)] == [item.invitation_id.hex]


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_concurrent_enrollments_of_the_same_name_create_one_user(tmp_path, migrated_template, round_number):
    url = fresh_database(tmp_path, migrated_template, "same.db")

    results, errors = run_all_at_once(
        url, [lambda factory: enrollment.enroll_user(factory, "Мама", now=NOW) for _ in range(6)]
    )

    assert len(results) == 1 and len(errors) == 5
    assert all(error.reason is EnrollmentRefusalReason.DISPLAY_NAME_IN_USE for error in errors), errors
    assert counts(url)["users"] == 1 and counts(url)["identity_invitations"] == 1


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_enrollments_racing_provision_email_never_exceed_the_limit_and_all_end_in_a_decision(
    tmp_path, migrated_template, round_number
):
    # They share one lock: with it, "how many users are there" is answered by one at a time.
    url = fresh_database(tmp_path, migrated_template, "mixed.db")
    jobs = [lambda factory: provision_email(factory, prepare_request("Elena@example.com"))] + [
        (lambda factory, n=n: enrollment.enroll_user(factory, n, now=NOW)) for n in ("Alpha", "Beta", "Gamma")
    ]

    results, errors = run_all_at_once(url, jobs)

    assert all(isinstance(error, (Refusal, EnrollmentRefusal)) for error in errors), errors
    created = [r for r in results if getattr(r, "outcome", None) is Outcome.CREATED] + [
        r for r in results if not hasattr(r, "outcome")
    ]
    assert 1 <= counts(url)["users"] <= ENROLLMENT_USER_CAP
    assert counts(url)["users"] == len(created)
    assert len(results) + len(errors) == 4


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_concurrent_reissues_leave_one_outstanding_invitation(tmp_path, migrated_template, round_number):
    url = fresh_database(tmp_path, migrated_template, "reissue.db")
    first = enroll_now(url, "Elena")

    results, errors = run_all_at_once(
        url, [lambda factory: enrollment.reissue_invitation(factory, first.user_id, now=LATER) for _ in range(6)]
    )

    assert errors == [] and len(results) == 6
    assert len({item.token for item in results}) == 6
    assert len(invitations_of(url)) == 7  # the first and six more
    assert len(outstanding(url, first.user_id)) == 1
    assert sorted(item.superseded for item in results) == [1] * 6  # each revoked the one before it


def run_module(url, *argv):
    return subprocess.Popen(
        [sys.executable, "-m", "house_helper.admin", *argv],
        cwd=REPO_ROOT,
        env=clean_environment(**({DATABASE_URL_ENV: url} if url else {})),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def test_separate_processes_enrolling_at_once_create_exactly_two_users(tmp_path, migrated_template):
    url = fresh_database(tmp_path, migrated_template, "processes.db")

    processes = [run_module(url, "enroll-user", "--display-name", f"Person {n}") for n in range(4)]
    finished = [(p.communicate(timeout=120), p.returncode) for p in processes]

    codes = sorted(code for _, code in finished)
    assert codes == [0, 0, 3, 3], finished
    assert counts(url)["users"] == 2 and counts(url)["identity_invitations"] == 2
    secrets_printed = [
        parse_invitation_code(line[len("invitation code: "):])
        for (out, _), code in finished
        if code == 0
        for line in out.splitlines()
        if line.startswith("invitation code: ")
    ]
    assert len(secrets_printed) == 2 and None not in secrets_printed
    for (out, err), code in finished:  # a refused run printed no secret
        if code == 3:
            assert out == "" and err.startswith("refused [enrollment_full]")


# --- the command line -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["enroll-user"],  # --display-name is required
        ["enroll-user", "--display-name"],
        ["enroll-user", "--display-nam", "x"],  # no abbreviations
        ["enroll-user", "--display-name", "x", "--database-url", f"x://u:{SECRET}@h/d"],
        ["enroll-user", "--display-name", "x", f"{SECRET}-positional"],
        ["enroll-user", "--display-name", "x", "--code", SECRET],
        ["reissue-invitation"],
        ["reissue-invitation", "--user-id"],
        ["revoke-invitation", "--user-id", "x", f"{SECRET}-positional"],
        ["revoke-invitation", f"--no-such-{SECRET}=1", "--user-id", "x"],
        ["enrollment-status", "--user-id", SECRET],
        ["enrollment-status", f"{SECRET}-positional"],
    ],
)
def test_argument_errors_are_a_fixed_message_that_never_quotes_the_text(capsys, argv):
    code = main(argv, environ={})

    captured = capsys.readouterr()
    assert code == ExitCode.USAGE and captured.out == ""
    assert "usage [arguments]: " in captured.err
    assert SECRET not in captured.err and "Traceback" not in captured.err


@pytest.mark.parametrize("command", ["enroll-user", "reissue-invitation", "revoke-invitation", "enrollment-status"])
def test_help_works_and_says_what_the_command_never_does(capsys, command):
    code = main([command, "--help"], environ={})

    out = capsys.readouterr().out
    assert code == 0
    assert DATABASE_URL_ENV in out and "never creates a database" in out and "never migrates" in out
    assert "exit status" in out


def test_the_help_of_enroll_user_states_the_policy(capsys):
    main(["enroll-user", "--help"], environ={})

    out = " ".join(capsys.readouterr().out.split())
    assert f"valid for {int(INVITATION_LIFETIME.total_seconds() // 3600)} hours" in out
    assert f"At most {ENROLLMENT_USER_CAP} users" in out and "not a database rule" in out
    assert "never matches anybody by name, email address or Telegram id" in out
    assert "--display-name" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["enroll-user", "--display-name", "Elena"],
        ["reissue-invitation", "--user-id", str(uuid.uuid4())],
        ["revoke-invitation", "--user-id", str(uuid.uuid4())],
        ["enrollment-status"],
    ],
)
def test_without_a_configured_database_every_command_stops_before_any_engine(capsys, argv):
    result = run_admin(capsys, None, *argv)

    assert result.code == ExitCode.DATABASE and result.out == ""
    assert result.err.startswith("error [not_configured]: ")


@pytest.mark.parametrize("revision", ["0004_email_identity_case", "0003_email_login_challenges"])
@pytest.mark.parametrize(
    "argv",
    [
        ["enroll-user", "--display-name", "Elena"],
        ["reissue-invitation", "--user-id", str(uuid.uuid4())],
        ["revoke-invitation", "--user-id", str(uuid.uuid4())],
        ["enrollment-status"],
    ],
)
def test_a_database_behind_the_head_is_refused_and_not_migrated(capsys, tmp_path, revision, argv):
    url = database_at(tmp_path, revision)
    tables = rows(url, "SELECT name FROM sqlite_master ORDER BY name")

    result = run_admin(capsys, url, *argv)

    assert result.code == ExitCode.DATABASE and result.out == ""
    assert result.err.startswith("error [schema_not_head]: ")
    assert f"revision {revision}" in result.err and "never migrates" in result.err
    assert rows(url, "SELECT name FROM sqlite_master ORDER BY name") == tables  # no table was created


def test_a_keyboard_interrupt_is_reported_without_a_traceback_and_prints_no_secret(capsys, db_url, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(enrollment, "enroll_user", interrupted)

    result = enroll(capsys, db_url)

    assert result.code == INTERRUPTED and result.out == ""
    assert result.err.startswith("error [interrupted]: ") and "Traceback" not in result.err
    assert counts(db_url)["users"] == 0


@pytest.mark.parametrize("command", ["enroll", "reissue", "revoke", "status"])
def test_an_unexpected_failure_prints_its_class_and_none_of_its_text(capsys, db_url, monkeypatch, command):
    user_id, _, _ = issued_from(enroll(capsys, db_url, "Elena"))
    error = sa.exc.IntegrityError(
        "INSERT INTO identity_invitations (token_hash) VALUES (?)",
        {"token_hash": SECRET},
        Exception(f"UNIQUE constraint failed {SECRET}"),
    )

    def failing(*args, **kwargs):
        raise error

    name = {
        "enroll": "enroll_user",
        "reissue": "reissue_invitation",
        "revoke": "revoke_invitation",
        "status": "enrollment_status",
    }[command]
    monkeypatch.setattr(enrollment, name, failing)
    argv = {
        "enroll": ["enroll-user", "--display-name", "Мама"],
        "reissue": ["reissue-invitation", "--user-id", str(user_id)],
        "revoke": ["revoke-invitation", "--user-id", str(user_id)],
        "status": ["enrollment-status"],
    }[command]

    result = run_admin(capsys, db_url, *argv)

    assert result.code == ExitCode.INTERNAL and result.out == ""
    assert result.err.startswith("error [internal]: unexpected IntegrityError. ")
    for leaked in (SECRET, "INSERT", "UNIQUE", "Traceback", "Мама"):
        assert leaked not in result.text


@pytest.mark.parametrize("scenario", ["enrolled", "reissued", "revoked", "refused", "status"])
def test_no_outcome_prints_the_database_location(capsys, db_url, db_path, scenario):
    user_id, _, _ = issued_from(enroll(capsys, db_url, "Elena"))
    result = {
        "enrolled": lambda: enroll(capsys, db_url, "Мама"),
        "reissued": lambda: reissue(capsys, db_url, user_id),
        "revoked": lambda: revoke_it(capsys, db_url, user_id),
        "refused": lambda: reissue(capsys, db_url, uuid.uuid4()),
        "status": lambda: status(capsys, db_url),
    }[scenario]()

    assert result.code in (0, 3)
    for leaked in (str(db_path), db_path.name, "sqlite:", db_url, str(db_path.parent)):
        assert leaked not in result.text


def test_the_real_clock_is_used_unless_a_test_gives_another(capsys, db_url):
    from datetime import datetime, timezone

    before = datetime.now(timezone.utc)
    result = run_admin(capsys, db_url, "enroll-user", "--display-name", "Elena")
    after = datetime.now(timezone.utc)

    _, invitation_id, token = issued_from(result)
    engine, factory = open_factory(db_url)
    try:
        with factory() as db:
            live = verify_invitation(db, invitation_id, token, now=after)
    finally:
        engine.dispose()
    assert before + TWO_HOURS <= live.expires_at <= after + TWO_HOURS


def test_the_module_entry_point_enrolls_and_reports(tmp_path, migrated_template):
    url = fresh_database(tmp_path, migrated_template, "module.db")

    process = run_module(url, "enroll-user", "--display-name", "Elena")
    out, err = process.communicate(timeout=120)
    report = run_module(url, "enrollment-status")
    report_out, report_err = report.communicate(timeout=120)

    assert process.returncode == 0 and err == "" and out.startswith("ok [enrolled]: ")
    assert report.returncode == 0 and report_err == "" and 'name "Elena"' in report_out
    assert parse_invitation_code(out.splitlines()[1][len("invitation code: "):]) is not None
    assert out.splitlines()[1].split(" ", 2)[2] not in report_out  # the report has no secret


# --- the secret is in no log -----------------------------------------------------------------------------


def test_the_secret_is_in_no_log_record_of_any_command_even_with_statement_logging_on(capsys, caplog, db_url):
    caplog.set_level(logging.DEBUG, logger="sqlalchemy.engine")
    caplog.set_level(logging.DEBUG)

    first = issued_from(enroll(capsys, db_url, "Elena"))
    second = issued_from(reissue(capsys, db_url, first[0]))
    revoke_it(capsys, db_url, first[0])
    status(capsys, db_url)

    assert "INSERT INTO identity_invitations" in caplog.text  # the logging really was on
    assert hash_secret(second[2]) in caplog.text  # ... and it shows the digest, which is stored
    assert first[2] not in caplog.text and second[2] not in caplog.text
