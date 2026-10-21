"""The Alembic migrations: upgrade, downgrade, and agreement with the models.

SQLite files under tmp_path only. PostgreSQL is covered up to what can be shown
without a server: the migration is rendered to SQL for that dialect (Alembic's
offline mode needs no driver and no connection).
"""

import ast
import io
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
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
from house_helper.persistence import authn as authn_module
from house_helper.persistence.allowlist import allow, allowed_user, is_allowed, revoke
from house_helper.persistence.authn import authenticate, complete_login, hash_secret, new_secret
from house_helper.persistence.email_login import (
    IssueOutcome,
    issue_email_login,
    preview_email_login,
    redeem_email_login,
)
from house_helper.persistence.models import AllowlistEntry, EmailLoginChallenge, IdentityLink, User
from house_helper.persistence.models import Session as LoginSession
from tests.persistence_support import (
    CREATED,
    new_conversation,
    new_identity_link,
    new_message,
    new_user,
    raw_insert,
    stored,
    utc,
)
from tests.test_persistence_schema import TABLES

INITIAL = "0001_persistence_foundation"
PROVENANCE = "0002_session_provenance"
CHALLENGES = "0003_email_login_challenges"
EMAIL_CASE = "0004_email_identity_case"
HEAD = "0005_identity_invitations"
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


def test_the_history_is_one_line_with_one_head():
    script = ScriptDirectory.from_config(alembic_config())

    assert script.get_heads() == [HEAD]
    assert [revision.revision for revision in script.walk_revisions()] == [
        HEAD,
        EMAIL_CASE,
        CHALLENGES,
        PROVENANCE,
        INITIAL,
    ]
    assert script.get_revision(HEAD).down_revision == EMAIL_CASE
    assert script.get_revision(EMAIL_CASE).down_revision == CHALLENGES
    assert script.get_revision(CHALLENGES).down_revision == PROVENANCE
    assert script.get_revision(PROVENANCE).down_revision == INITIAL
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
    assert _version(empty_engine) == [HEAD]


def test_upgrading_again_changes_nothing(empty_engine):
    upgrade_database(empty_engine)
    upgrade_database(empty_engine)

    assert _tables(empty_engine) == TABLES | {"alembic_version"}
    assert _version(empty_engine) == [HEAD]


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
    for table in sorted(TABLES & set(inspector.get_table_names())):  # an older revision has fewer
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


def test_the_comparison_sees_the_email_rule_of_both_identity_tables(empty_engine):
    # Guard the guard: the one CHECK that revision 0004 replaced must be what the
    # fingerprint equality compares, in the migrated schema as in the models'.
    upgrade_database(empty_engine)
    fingerprint = _fingerprint(empty_engine)

    for table in ("allowlist_entries", "identity_links"):
        rule = dict(fingerprint[table]["checks"])[f"ck_{table}_email_subject_normalized"]
        assert "substr(subject, instr(subject, '@')) = lower(substr(subject, instr(subject, '@')))" in rule
        assert "subject = lower(subject)" not in rule  # the whole address is no longer lower-cased


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


# --- 0002 on a database that holds R3A data -----------------------------------------
#
# Policy under test: a session that exists when the column is added has no
# provenance that can be shown, so it is revoked, never matched to a link, and can
# never be live again; everything else is left as it was.

TOKEN_NAMES = ("live", "revoked_earlier", "expired", "owner_of_a_single_link")
EARLIER_REVOCATION = utc(2026, 10, 16, 13)
SOME_TABLES = ("users", "allowlist_entries", "identity_links", "conversations", "messages")


def _r3a_database(engine) -> dict[str, str]:
    """A database as R3A left it (revision 0001 only), holding two users with
    three identity links and four sessions, none of which says which link it came
    through, and a conversation. Returns the sessions' tokens by name."""
    upgrade_database(engine, INITIAL)
    tokens = {name: new_secret() for name in TOKEN_NAMES}
    sessions = {
        # name: (owner, revoked_at)
        "live": ("elena", None),
        "revoked_earlier": ("elena", stored(EARLIER_REVOCATION)),
        "expired": ("elena", None),
        "owner_of_a_single_link": ("other", None),
    }
    with create_session_factory(engine)() as db:
        elena, other = new_user(db, display_name="Elena"), new_user(db, display_name="Other")
        owners = {"elena": elena, "other": other}
        new_identity_link(db, elena, "telegram", "42", last_login_at=CREATED)
        new_identity_link(db, elena, "email", "elena@example.com")
        new_identity_link(db, other, "telegram", "77")  # their only link: still not inferred
        new_message(db, new_conversation(db, elena))
        for name, (owner, revoked_at) in sessions.items():
            expires = CREATED + timedelta(days=1 if name == "expired" else 14)
            raw_insert(
                db,
                "sessions",
                id=uuid.uuid4().hex,
                user_id=owners[owner].id.hex,
                token_hash=hash_secret(tokens[name]),
                created_at=stored(CREATED),
                last_seen_at=stored(CREATED),
                expires_at=stored(expires),
                revoked_at=revoked_at,
            )
        db.commit()
    return tokens


def _counts(engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar()
            for table in SOME_TABLES
        }


def test_the_provenance_migration_revokes_every_existing_session_and_infers_nothing(empty_engine):
    tokens = _r3a_database(empty_engine)
    before = datetime.now(timezone.utc) - timedelta(seconds=2)

    upgrade_database(empty_engine)

    after = datetime.now(timezone.utc) + timedelta(seconds=2)
    with create_session_factory(empty_engine)() as db:
        by_hash = {row.token_hash: row for row in db.scalars(sa.select(LoginSession))}
        assert len(by_hash) == 4
        for name, token in tokens.items():
            row = by_hash[hash_secret(token)]
            assert row.identity_link_id is None, name  # not even for the owner of a single link
            assert row.revoked_at is not None, name
            if name == "revoked_earlier":
                assert row.revoked_at == EARLIER_REVOCATION  # the first revocation is kept
            else:
                assert before <= row.revoked_at <= after, name  # the time of the migration
            # R3A's rules would have accepted the first, third and fourth until their
            # expiry; none can authenticate now
            assert authenticate(db, token, now=CREATED + timedelta(hours=1)) is None, name


def test_the_provenance_migration_leaves_every_other_row_alone(empty_engine):
    _r3a_database(empty_engine)
    before = _counts(empty_engine)
    with empty_engine.connect() as connection:
        links_before = connection.exec_driver_sql(
            "SELECT id, user_id, provider, subject, created_at, last_login_at "
            "FROM identity_links ORDER BY id"
        ).all()

    upgrade_database(empty_engine)

    assert _counts(empty_engine) == before == {
        "users": 2,
        "allowlist_entries": 3,
        "identity_links": 3,
        "conversations": 1,
        "messages": 1,
    }
    with empty_engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT id, user_id, provider, subject, created_at, last_login_at "
            "FROM identity_links ORDER BY id"
        ).all() == links_before
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]


def test_after_the_migration_a_person_signs_in_again_and_gets_a_session_with_provenance(empty_engine):
    _r3a_database(empty_engine)
    upgrade_database(empty_engine)
    now = CREATED + timedelta(days=2)

    with create_session_factory(empty_engine)() as db, db.begin():
        issued = complete_login(db, "telegram", "42", now=now)
    with create_session_factory(empty_engine)() as db:
        assert authenticate(db, issued.token, now=now) is not None
        link = db.scalars(sa.select(IdentityLink).where(IdentityLink.subject == "42")).one()
        assert db.get(LoginSession, issued.session_id).identity_link_id == link.id
        assert link.last_login_at == now


def test_a_session_that_the_migration_revoked_cannot_be_made_live_again(empty_engine):
    _r3a_database(empty_engine)
    upgrade_database(empty_engine)

    with pytest.raises(sa.exc.IntegrityError, match="ck_sessions_provenance_or_revoked"):
        with empty_engine.begin() as connection:
            connection.exec_driver_sql("UPDATE sessions SET revoked_at = NULL")


def test_downgrading_the_provenance_keeps_every_row_and_the_old_schema(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    pristine = create_db_engine(f"sqlite:///{tmp_path / 'pristine.db'}")
    try:
        tokens = _r3a_database(migrated)
        upgrade_database(pristine, INITIAL)
        upgrade_database(migrated)
        with create_session_factory(migrated)() as db, db.begin():
            issued = complete_login(db, "telegram", "42", now=CREATED + timedelta(days=2))
        before = _counts(migrated)

        downgrade_database(migrated, INITIAL)

        assert _version(migrated) == [INITIAL]
        assert _fingerprint(migrated) == _fingerprint(pristine)  # the 0001 schema, exactly
        assert _counts(migrated) == before
        with migrated.connect() as connection:
            revoked = dict(
                connection.exec_driver_sql("SELECT token_hash, revoked_at IS NOT NULL FROM sessions").all()
            )
        # the revocations stand (a downgrade does not bring a session back), and the
        # session opened since is still there and live
        assert [revoked[hash_secret(token)] for token in tokens.values()] == [1, 1, 1, 1]
        assert revoked[hash_secret(issued.token)] == 0

        upgrade_database(migrated)  # and it can go forward again; the provenance is gone, so
        with create_session_factory(migrated)() as db:
            assert authenticate(db, issued.token, now=CREATED + timedelta(days=2)) is None
    finally:
        migrated.dispose()
        pristine.dispose()


# --- 0003 on a database that holds R3B-1 data -----------------------------------------
#
# Policy under test: the table is added and nothing else is touched. Sessions with
# their provenance stay live, identity links stay, and the new table starts empty.
# (These stop at 0003: 0004, which follows it, revokes the email identities; see below.)

ALL_TABLES_BEFORE_0003 = TABLES - {"email_login_challenges", "identity_invitations"}


def _r3b1_database(engine):
    """A database as R3B-1 left it (revision 0002): one user with a Telegram and an
    email entry, a conversation, and a live session opened through the Telegram link.
    Returns the session that was issued."""
    upgrade_database(engine, PROVENANCE)
    with create_session_factory(engine)() as db, db.begin():
        elena = new_user(db, display_name="Elena")
        allow(db, elena, "telegram", "42")
        allow(db, elena, "email", "elena@example.com")
        issued = complete_login(db, "telegram", "42", now=CREATED)
        new_message(db, new_conversation(db, elena))
    return issued


def _snapshot(engine) -> dict[str, list]:
    with engine.connect() as connection:
        return {
            table: sorted(connection.exec_driver_sql(f"SELECT * FROM {table}").all(), key=repr)
            for table in sorted(ALL_TABLES_BEFORE_0003)
        }


def test_the_email_challenge_migration_adds_one_empty_table_and_changes_nothing_else(empty_engine):
    issued = _r3b1_database(empty_engine)
    before = _snapshot(empty_engine)
    assert _version(empty_engine) == [PROVENANCE]

    upgrade_database(empty_engine, CHALLENGES)

    assert _version(empty_engine) == [CHALLENGES]
    assert _tables(empty_engine) == ALL_TABLES_BEFORE_0003 | {"email_login_challenges", "alembic_version"}
    assert _snapshot(empty_engine) == before  # every row of every old table, byte for byte
    with empty_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM email_login_challenges").scalar() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]
    with create_session_factory(empty_engine)() as db:
        # nobody was signed out, and the session still says which identity it came through
        assert authenticate(db, issued.token, now=CREATED + timedelta(hours=1)) is not None
        row = db.get(LoginSession, issued.session_id)
        link = db.scalars(sa.select(IdentityLink).where(IdentityLink.provider == "telegram")).one()
        assert row.identity_link_id == link.id and row.revoked_at is None


def test_after_the_migration_an_allowlisted_address_signs_in_beside_the_old_session(empty_engine):
    issued = _r3b1_database(empty_engine)
    upgrade_database(empty_engine, CHALLENGES)
    now = CREATED + timedelta(hours=2)

    with create_session_factory(empty_engine)() as db, db.begin():
        link = issue_email_login(db, "elena@example.com", now=now).link
    with create_session_factory(empty_engine)() as db, db.begin():
        by_email = redeem_email_login(db, str(link.challenge_id), link.token, now=now)

    with create_session_factory(empty_engine)() as db:
        assert authenticate(db, by_email.token, now=now) is not None
        assert authenticate(db, issued.token, now=now) is not None
        providers = {
            db.get(LoginSession, token.session_id).identity_link_id: None for token in (issued, by_email)
        }
        assert len(providers) == 2  # two sessions, two identity links, one user
        assert db.scalars(sa.select(sa.func.count(sa.distinct(LoginSession.user_id)))).one() == 1


def test_downgrading_the_email_challenges_drops_only_that_table(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    pristine = create_db_engine(f"sqlite:///{tmp_path / 'pristine.db'}")
    try:
        issued = _r3b1_database(migrated)
        upgrade_database(pristine, PROVENANCE)
        upgrade_database(migrated, CHALLENGES)
        with create_session_factory(migrated)() as db, db.begin():
            issue_email_login(db, "elena@example.com", now=CREATED)  # a link is outstanding
        before = _snapshot(migrated)

        downgrade_database(migrated, PROVENANCE)

        assert _version(migrated) == [PROVENANCE]
        assert _tables(migrated) == ALL_TABLES_BEFORE_0003 | {"alembic_version"}
        assert _fingerprint(migrated) == _fingerprint(pristine)  # the 0002 schema, exactly
        assert _snapshot(migrated) == before
        with create_session_factory(migrated)() as db:
            assert authenticate(db, issued.token, now=CREATED + timedelta(hours=1)) is not None

        upgrade_database(migrated, CHALLENGES)  # and forward again: an empty table, as it began
        with migrated.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM email_login_challenges").scalar() == 0
    finally:
        migrated.dispose()
        pristine.dispose()


# --- 0004 on a database that holds R3B-2 data -----------------------------------------
#
# Policy under test (fail closed): every email identity that exists is revoked, with
# what revoking it means (its sessions, its unused links); Telegram and everything else
# is left exactly as it was; re-verification by an administrator is the only way back and
# brings nothing that was revoked back; nothing moves between users.

LATER = CREATED + timedelta(minutes=3)


def _r3b2_database(engine) -> dict:
    """A database as 0003 left it, with email logins in use: Elena (a Telegram identity
    and a live session through it, an email identity with a live session through it and
    an unused link) and Other (an email identity with a live session, one revoked by an
    administrator earlier with its own revoked session), a conversation and a message.
    Returns the tokens and ids the tests need."""
    upgrade_database(engine, CHALLENGES)
    found = {}
    with create_session_factory(engine)() as db, db.begin():
        elena, other = new_user(db, display_name="Elena"), new_user(db, display_name="Other")
        found["elena"], found["other"] = elena.id, other.id
        for owner, provider, subject in [
            (elena, "telegram", "42"),
            (elena, "email", "elena@example.com"),
            (other, "telegram", "77"),
            (other, "email", "other@example.com"),
            (other, "email", "gone@example.com"),
        ]:
            allow(db, owner, provider, subject)
        found["telegram"] = complete_login(db, "telegram", "42", now=CREATED)
        for name, address in [("elena_email", "elena@example.com"), ("other_email", "other@example.com")]:
            link = issue_email_login(db, address, now=CREATED).link
            found[name] = redeem_email_login(db, str(link.challenge_id), link.token, now=CREATED)
        found["unused_link"] = issue_email_login(db, "elena@example.com", now=CREATED + timedelta(minutes=2)).link
        new_message(db, new_conversation(db, elena))
    with create_session_factory(engine)() as db, db.begin():
        # "gone@" was revoked by an administrator before; a session it had is revoked with it
        complete_login(db, "email", "gone@example.com", now=CREATED)
        revoke(db, "email", "gone@example.com", now=EARLIER_REVOCATION)
    return found


def _rows_of(engine, table: str, omit: tuple[str, ...] = ()) -> list:
    with engine.connect() as connection:
        result = connection.exec_driver_sql(f"SELECT * FROM {table}")
        keep = [i for i, name in enumerate(result.keys()) if name not in omit]
        return sorted(([row[i] for i in keep] for row in result.all()), key=repr)


def _entries(db) -> dict[tuple[str, str], AllowlistEntry]:
    return {(row.provider, row.subject): row for row in db.scalars(sa.select(AllowlistEntry))}


def test_the_email_case_migration_revokes_every_email_identity_and_what_came_through_it(empty_engine):
    found = _r3b2_database(empty_engine)
    before = datetime.now(timezone.utc) - timedelta(seconds=2)

    upgrade_database(empty_engine)

    after = datetime.now(timezone.utc) + timedelta(seconds=2)
    assert _version(empty_engine) == [HEAD]
    with create_session_factory(empty_engine)() as db:
        entries = _entries(db)
        for key in [("email", "elena@example.com"), ("email", "other@example.com")]:
            assert before <= entries[key].revoked_at <= after, key  # the time of the migration
        assert entries[("email", "gone@example.com")].revoked_at == EARLIER_REVOCATION  # kept
        # nobody can ask for a link for any of them, or be signed in by what they held
        for address in ("elena@example.com", "other@example.com", "gone@example.com"):
            assert issue_email_login(db, address, now=LATER).outcome is IssueOutcome.NOT_ELIGIBLE, address
        assert authenticate(db, found["elena_email"].token, now=LATER) is None
        assert authenticate(db, found["other_email"].token, now=LATER) is None
        row = db.get(LoginSession, found["elena_email"].session_id)
        assert before <= row.revoked_at <= after  # ended, as revoke() would have ended it
        # the link that was out is withdrawn: not shown, not redeemable
        unused = found["unused_link"]
        assert preview_email_login(db, str(unused.challenge_id), now=LATER) is None
        challenge = db.get(EmailLoginChallenge, unused.challenge_id)
        assert before <= challenge.revoked_at <= after and challenge.consumed_at is None
        used = [c for c in db.scalars(sa.select(EmailLoginChallenge)) if c.consumed_at is not None]
        assert len(used) == 2 and all(c.revoked_at is None for c in used)  # what was used stays as it was
    with create_session_factory(empty_engine)() as db, db.begin():
        assert redeem_email_login(db, str(unused.challenge_id), unused.token, now=LATER) is None
        with pytest.raises(authn_module.LoginDenied):
            complete_login(db, "email", "elena@example.com", now=LATER)


def test_the_email_case_migration_does_not_touch_telegram_or_anything_else(empty_engine):
    found = _r3b2_database(empty_engine)
    unchanged = ("users", "identity_links", "conversations", "messages", "turn_requests")
    before = {table: _rows_of(empty_engine, table) for table in unchanged}
    entries_before = _rows_of(empty_engine, "allowlist_entries", omit=("revoked_at",))
    telegram_before = _rows_of(empty_engine, "allowlist_entries")
    telegram_before = [row for row in telegram_before if "telegram" in row]
    sessions_before = _rows_of(empty_engine, "sessions", omit=("revoked_at",))

    upgrade_database(empty_engine)

    for table in unchanged:
        assert _rows_of(empty_engine, table) == before[table], table  # byte for byte
    # an entry keeps its id, its owner, its spelling, its note and its creation time;
    # only revoked_at of the email ones changes, and nothing is created, moved or deleted
    assert _rows_of(empty_engine, "allowlist_entries", omit=("revoked_at",)) == entries_before
    assert [row for row in _rows_of(empty_engine, "allowlist_entries") if "telegram" in row] == telegram_before
    assert _rows_of(empty_engine, "sessions", omit=("revoked_at",)) == sessions_before
    with create_session_factory(empty_engine)() as db:
        entries = _entries(db)
        assert entries[("telegram", "42")].revoked_at is None and entries[("telegram", "77")].revoked_at is None
        # nobody who signed in with Telegram was signed out
        assert authenticate(db, found["telegram"].token, now=LATER) is not None
        telegram_links = db.scalars(sa.select(IdentityLink).where(IdentityLink.provider == "telegram")).all()
        assert len(telegram_links) == 1
        assert entries[("email", "elena@example.com")].user_id == found["elena"]  # nothing changed owner
        assert entries[("email", "other@example.com")].user_id == found["other"]
    with empty_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]


def test_re_verifying_the_stored_spelling_lifts_the_revocation_and_revives_nothing(empty_engine):
    found = _r3b2_database(empty_engine)
    upgrade_database(empty_engine)
    unused = found["unused_link"]

    with create_session_factory(empty_engine)() as db, db.begin():
        legacy = _entries(db)[("email", "elena@example.com")]
        again = allow(db, db.get(User, found["elena"]), "email", "elena@example.com")  # the administrator says so
        assert again.id == legacy.id and again.revoked_at is None  # the same row, not a new identity

    with create_session_factory(empty_engine)() as db:
        # the address can be mailed again ...
        assert is_allowed(db, "email", "elena@example.com")
        # ... but nothing from before comes back: not the session, not the link
        assert authenticate(db, found["elena_email"].token, now=LATER) is None
        assert preview_email_login(db, str(unused.challenge_id), now=LATER) is None
        assert authenticate(db, found["other_email"].token, now=LATER) is None  # "other@" was not re-verified
    with create_session_factory(empty_engine)() as db, db.begin():
        assert redeem_email_login(db, str(unused.challenge_id), unused.token, now=LATER) is None
    with create_session_factory(empty_engine)() as db, db.begin():
        fresh = issue_email_login(db, "elena@example.com", now=LATER + timedelta(minutes=5)).link
        assert fresh is not None
    with create_session_factory(empty_engine)() as db, db.begin():
        signed_in = redeem_email_login(db, str(fresh.challenge_id), fresh.token, now=LATER + timedelta(minutes=5))
        assert signed_in is not None and signed_in.user_id == found["elena"]
    with create_session_factory(empty_engine)() as db:
        later = LATER + timedelta(minutes=6)
        assert authenticate(db, signed_in.token, now=later) is not None  # a NEW session works
        assert authenticate(db, found["elena_email"].token, now=later) is None  # the old one never does
        assert db.get(LoginSession, signed_in.session_id).identity_link_id == db.get(
            LoginSession, found["elena_email"].session_id
        ).identity_link_id  # the same identity link, a different session


def test_re_verifying_another_spelling_is_a_new_identity_and_the_legacy_one_stays_revoked(empty_engine):
    found = _r3b2_database(empty_engine)
    upgrade_database(empty_engine)

    with create_session_factory(empty_engine)() as db, db.begin():
        approved = allow(db, db.get(User, found["elena"]), "email", "Elena@example.com")

    with create_session_factory(empty_engine)() as db:
        entries = _entries(db)
        legacy = entries[("email", "elena@example.com")]
        assert approved.id != legacy.id and approved.subject == "Elena@example.com"
        assert legacy.revoked_at is not None  # the lower-case spelling was never approved again
        assert not is_allowed(db, "email", "elena@example.com")
        assert is_allowed(db, "email", "Elena@example.com")
        assert issue_email_login(db, "elena@example.com", now=LATER).outcome is IssueOutcome.NOT_ELIGIBLE
        assert issue_email_login(db, "Elena@example.com", now=LATER).outcome is IssueOutcome.ISSUED
        assert authenticate(db, found["elena_email"].token, now=LATER) is None  # the old session is not this one


def test_the_legacy_spelling_is_not_handed_to_another_user_by_asking_again(empty_engine):
    found = _r3b2_database(empty_engine)
    upgrade_database(empty_engine)

    with create_session_factory(empty_engine)() as db, db.begin():
        with pytest.raises(ValueError, match="another user"):
            allow(db, db.get(User, found["other"]), "email", "elena@example.com")  # Elena's, revoked or not

    with create_session_factory(empty_engine)() as db:
        entry = _entries(db)[("email", "elena@example.com")]
        assert entry.user_id == found["elena"] and entry.revoked_at is not None


def test_after_the_migration_two_case_distinct_addresses_belong_to_two_users(empty_engine):
    upgrade_database(empty_engine)  # an empty database: the schema is what is shown

    with create_session_factory(empty_engine)() as db, db.begin():
        first, second = new_user(db, display_name="First"), new_user(db, display_name="Second")
        allow(db, first, "email", "Victim@example.com")
        allow(db, second, "email", "victim@example.com")
        new_identity_link(db, first, "email", "Victim@example.com")
        new_identity_link(db, second, "email", "victim@example.com")
    with create_session_factory(empty_engine)() as db:
        assert allowed_user(db, "email", "Victim@example.com").display_name == "First"
        assert allowed_user(db, "email", "victim@example.com").display_name == "Second"
        assert allowed_user(db, "email", "VICTIM@example.com") is None
    for table in ("allowlist_entries", "identity_links"):
        with pytest.raises(sa.exc.IntegrityError, match="ck_" + table + "_email_subject_normalized"):
            with empty_engine.begin() as connection:  # a domain that is not lower-case is still refused
                connection.exec_driver_sql(f"UPDATE {table} SET subject = 'Victim@Example.com' WHERE subject = 'Victim@example.com'")


def test_the_migration_is_refused_whole_when_the_stored_definition_is_not_the_expected_one(empty_engine):
    found = _r3b2_database(empty_engine)
    with empty_engine.begin() as connection:  # a database that is not what 0003 left
        connection.exec_driver_sql("PRAGMA writable_schema = ON")
        connection.exec_driver_sql(
            "UPDATE sqlite_master SET sql = replace(sql, 'subject = lower(subject)', 'lower(subject) = subject') "
            "WHERE type = 'table' AND name = 'identity_links'"
        )
        version = connection.exec_driver_sql("PRAGMA schema_version").scalar()
        connection.exec_driver_sql(f"PRAGMA schema_version = {version + 1}")
        connection.exec_driver_sql("PRAGMA writable_schema = OFF")

    with pytest.raises(RuntimeError, match="identity_links"):
        upgrade_database(empty_engine)

    # nothing is half applied: the data step is rolled back with the schema step
    assert _version(empty_engine) == [CHALLENGES]
    with create_session_factory(empty_engine)() as db:
        assert all(
            entry.revoked_at is None
            for key, entry in _entries(db).items()
            if key[0] == "email" and key[1] != "gone@example.com"
        )
        assert authenticate(db, found["elena_email"].token, now=LATER) is not None


@pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "setconfig"), reason="sqlite3.Connection.setconfig needs Python 3.12"
)
def test_a_sqlite_that_refuses_to_edit_its_schema_stops_the_migration_whole(empty_engine):
    # A "defensive" build (or connection) does not let sqlite_master be written. The
    # migration then fails, and the data step, which ran first, is rolled back with it.
    found = _r3b2_database(empty_engine)
    empty_engine.dispose()  # connections made from here on are the defensive ones

    def defensive(dbapi_connection, _record):
        dbapi_connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)

    sa.event.listen(empty_engine, "connect", defensive)
    with pytest.raises((sa.exc.OperationalError, RuntimeError)):
        upgrade_database(empty_engine)

    assert _version(empty_engine) == [CHALLENGES]
    with create_session_factory(empty_engine)() as db:
        assert all(
            entry.revoked_at is None
            for key, entry in _entries(db).items()
            if key[0] == "email" and key[1] != "gone@example.com"
        )
        assert authenticate(db, found["elena_email"].token, now=LATER) is not None


def test_downgrading_the_email_case_restores_the_0003_schema_and_keeps_the_revocations(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    pristine = create_db_engine(f"sqlite:///{tmp_path / 'pristine.db'}")
    modeled = create_db_engine(f"sqlite:///{tmp_path / 'modeled.db'}")
    try:
        found = _r3b2_database(migrated)
        upgrade_database(pristine, CHALLENGES)
        upgrade_database(migrated)

        downgrade_database(migrated, CHALLENGES)

        assert _version(migrated) == [CHALLENGES]
        assert _fingerprint(migrated) == _fingerprint(pristine)  # the 0003 schema, exactly
        with create_session_factory(migrated)() as db:
            entries = _entries(db)
            assert entries[("email", "elena@example.com")].revoked_at is not None  # not undone
            assert entries[("telegram", "42")].revoked_at is None
            assert authenticate(db, found["telegram"].token, now=LATER) is not None
            assert authenticate(db, found["elena_email"].token, now=LATER) is None
        with migrated.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        upgrade_database(migrated)  # and forward again: the models' schema once more
        assert _version(migrated) == [HEAD]
        Base.metadata.create_all(modeled)
        assert _fingerprint(migrated) == _fingerprint(modeled)
    finally:
        migrated.dispose()
        pristine.dispose()
        modeled.dispose()


def test_downgrading_is_refused_while_an_address_with_a_capital_in_its_local_part_exists(empty_engine):
    upgrade_database(empty_engine)
    with create_session_factory(empty_engine)() as db, db.begin():
        allow(db, new_user(db, display_name="Victim"), "email", "Victim@example.com")
        allow(db, new_user(db, display_name="Other"), "email", "fine@example.com")

    with pytest.raises(RuntimeError, match=r"cannot downgrade: allowlist_entries holds 1 email"):
        downgrade_database(empty_engine, CHALLENGES)

    assert _version(empty_engine) == [HEAD]  # nothing was changed
    with create_session_factory(empty_engine)() as db:
        assert {key[1] for key in _entries(db)} == {"Victim@example.com", "fine@example.com"}
    with create_session_factory(empty_engine)() as db, db.begin():  # the new rule is still the one in force
        allow(db, new_user(db), "email", "AnotherVictim@example.com")


# --- 0005 on a database that holds R3B data --------------------------------------------
#
# Policy under test: one table is added, empty, and nothing else is touched: every row of
# every old table stays byte for byte, nobody is signed out, no migration-time data step
# exists. No limit on the number of users enters the schema.

TABLES_BEFORE_0005 = TABLES - {"identity_invitations"}


def _r3b3_database(engine) -> dict:
    """A database as 0004 left it, with data in it: _r3b2_database's two users (their
    email identities now revoked by 0004), a live Telegram session, a conversation."""
    found = _r3b2_database(engine)
    upgrade_database(engine, EMAIL_CASE)
    return found


def _snapshot_before_0005(engine) -> dict[str, list]:
    with engine.connect() as connection:
        return {
            table: sorted(connection.exec_driver_sql(f"SELECT * FROM {table}").all(), key=repr)
            for table in sorted(TABLES_BEFORE_0005)
        }


def test_the_invitation_migration_adds_one_empty_table_and_changes_nothing_else(empty_engine):
    found = _r3b3_database(empty_engine)
    before = _snapshot_before_0005(empty_engine)
    assert _version(empty_engine) == [EMAIL_CASE]

    upgrade_database(empty_engine)

    assert _version(empty_engine) == [HEAD]
    assert _tables(empty_engine) == TABLES | {"alembic_version"}
    assert _snapshot_before_0005(empty_engine) == before  # every row of every old table
    with empty_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM identity_invitations").scalar() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]
    with create_session_factory(empty_engine)() as db:
        assert authenticate(db, found["telegram"].token, now=LATER) is not None  # nobody signed out


def test_the_invitation_migration_puts_no_limit_on_the_number_of_users_in_the_schema(empty_engine):
    upgrade_database(empty_engine)

    with create_session_factory(empty_engine)() as db, db.begin():
        users = [new_user(db, display_name=f"User {number}") for number in range(5)]
        for user in users:
            new_conversation(db, user)
    with empty_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM users").scalar() == 5
        # a trigger is the only other way for a schema to say "two"
        assert connection.exec_driver_sql(
            "SELECT count(*) FROM sqlite_master WHERE type = 'trigger'"
        ).scalar() == 0


def test_downgrading_the_invitations_drops_only_that_table(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    pristine = create_db_engine(f"sqlite:///{tmp_path / 'pristine.db'}")
    try:
        found = _r3b3_database(migrated)
        upgrade_database(pristine, EMAIL_CASE)
        upgrade_database(migrated)
        with create_session_factory(migrated)() as db, db.begin():  # an invitation is outstanding
            raw_insert(
                db,
                "identity_invitations",
                id=uuid.uuid4().hex,
                user_id=found["elena"].hex,
                provider="telegram",
                token_hash=hash_secret("an invitation"),
                created_at=stored(CREATED),
                expires_at=stored(CREATED + timedelta(hours=2)),
            )
        before = _snapshot_before_0005(migrated)

        downgrade_database(migrated, EMAIL_CASE)

        assert _version(migrated) == [EMAIL_CASE]
        assert _tables(migrated) == TABLES_BEFORE_0005 | {"alembic_version"}
        assert _fingerprint(migrated) == _fingerprint(pristine)  # the 0004 schema, exactly
        assert _snapshot_before_0005(migrated) == before
        with create_session_factory(migrated)() as db:
            assert authenticate(db, found["telegram"].token, now=LATER) is not None

        upgrade_database(migrated)  # and forward again: an empty table, as it began
        with migrated.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM identity_invitations").scalar() == 0
    finally:
        migrated.dispose()
        pristine.dispose()


def _index_sql(engine, name: str) -> str:
    with engine.connect() as connection:
        return connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
        ).scalar()


def test_the_one_outstanding_rule_is_the_same_partial_index_in_the_migrated_and_the_modeled_schema(tmp_path):
    # Guard the guard: the fingerprint compares an index by name, columns and uniqueness,
    # and not by its WHERE, which is the whole rule here.
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    modeled = create_db_engine(f"sqlite:///{tmp_path / 'modeled.db'}")
    try:
        upgrade_database(migrated)
        Base.metadata.create_all(modeled)

        name = "uq_identity_invitations_one_outstanding"
        assert _index_sql(migrated, name) == _index_sql(modeled, name)
        assert _index_sql(migrated, name) == (
            f"CREATE UNIQUE INDEX {name} ON identity_invitations (user_id) "
            "WHERE consumed_at IS NULL AND revoked_at IS NULL"
        )
    finally:
        migrated.dispose()
        modeled.dispose()


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
    assert _version(empty_engine) == [HEAD]


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
    elif direction == "down_from_provenance":
        command.downgrade(config, f"{PROVENANCE}:{INITIAL}", sql=True)
    elif direction == "down_from_challenges":
        command.downgrade(config, f"{CHALLENGES}:{PROVENANCE}", sql=True)
    elif direction == "down_from_email_case":
        command.downgrade(config, f"{EMAIL_CASE}:{CHALLENGES}", sql=True)
    elif direction == "down_from_head":
        command.downgrade(config, f"{HEAD}:{EMAIL_CASE}", sql=True)
    else:
        command.downgrade(config, f"{HEAD}:base", sql=True)
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
    assert "instr(" not in sql and "writable_schema" not in sql and "sqlite_master" not in sql
    assert "%" not in sql  # a doubled percent sign would make the offline SQL unrunnable
    assert f"INSERT INTO alembic_version (version_num) VALUES ('{INITIAL}')" in sql
    assert f"UPDATE alembic_version SET version_num='{HEAD}'" in sql
    assert sql.count("CREATE INDEX") == 15 and sql.count("CREATE UNIQUE INDEX") == 2


def test_the_downgrade_renders_as_postgresql_sql():
    sql = _offline_sql("down")

    for table in TABLES:
        assert f"DROP TABLE {table};" in sql
    assert "DELETE FROM alembic_version" in sql


def test_the_provenance_revision_renders_as_plain_postgresql_alters():
    up, down = _offline_sql("up"), _offline_sql("down_from_provenance")
    revision = up.split(f"Running upgrade {INITIAL} -> {PROVENANCE}")[1].split("Running upgrade")[0]

    # the data step comes first, so that the CHECK is added to rows that satisfy it
    steps = [line for line in revision.split("\n") if line.strip()]
    assert steps[0].startswith("UPDATE sessions SET revoked_at=now() WHERE sessions.revoked_at IS NULL")
    assert steps[1:6] == [
        "CREATE UNIQUE INDEX uq_identity_links_user_id_id ON identity_links (user_id, id);",
        "ALTER TABLE sessions ADD COLUMN identity_link_id UUID;",
        "ALTER TABLE sessions ADD CONSTRAINT fk_sessions_identity_link FOREIGN KEY(user_id, "
        "identity_link_id) REFERENCES identity_links (user_id, id) ON DELETE CASCADE;",
        "CREATE INDEX ix_sessions_identity_link_id ON sessions (identity_link_id);",
        "ALTER TABLE sessions ADD CONSTRAINT ck_sessions_provenance_or_revoked CHECK "
        "(identity_link_id IS NOT NULL OR revoked_at IS NOT NULL);",
    ]
    assert "strftime" not in revision and "CREATE TABLE" not in revision  # SQLite's rebuild stays out
    assert "DROP TABLE" not in down and "DROP COLUMN identity_link_id" in down
    assert "DROP INDEX uq_identity_links_user_id_id" in down


def test_the_email_challenge_revision_renders_as_one_table_and_its_indexes():
    up, down = _offline_sql("up"), _offline_sql("down_from_challenges")
    revision = up.split(f"Running upgrade {PROVENANCE} -> {CHALLENGES}")[1].split("-- Running upgrade")[0]

    statements = [part.strip() for part in revision.split(";") if part.strip() not in ("", "COMMIT")]
    assert statements[0].startswith("CREATE TABLE email_login_challenges (")
    assert "FOREIGN KEY(user_id, provider, subject) REFERENCES allowlist_entries (user_id, provider, subject) ON DELETE CASCADE" in statements[0]
    assert "CHECK (provider = 'email')" in statements[0]
    assert statements[1].startswith("CREATE INDEX ix_email_login_challenges_identity ON email_login_challenges (user_id, provider, subject, created_at)")
    assert statements[2].startswith("CREATE INDEX ix_email_login_challenges_expires_at ON email_login_challenges (expires_at)")
    assert statements[3].startswith("UPDATE alembic_version SET version_num='" + CHALLENGES + "'")
    assert len(statements) == 4  # nothing else is created, altered, updated or dropped
    assert "ALTER TABLE" not in revision and "DROP" not in revision
    assert re.findall(r"^DROP [^;]*", down, re.MULTILINE) == ["DROP TABLE email_login_challenges"]


def test_the_email_case_revision_renders_as_three_updates_and_a_swap_of_one_check_per_table():
    up, down = _offline_sql("up"), _offline_sql("down_from_email_case")
    revision = up.split(f"Running upgrade {CHALLENGES} -> {EMAIL_CASE}")[1].split("-- Running upgrade")[0]

    statements = [re.sub(r"\s+", " ", part.strip()) for part in revision.split(";")]
    statements = [part for part in statements if part not in ("", "COMMIT")]
    assert statements[0].startswith(
        "UPDATE allowlist_entries SET revoked_at=now() WHERE allowlist_entries.provider = 'email' "
        "AND allowlist_entries.revoked_at IS NULL"
    )
    assert statements[1].startswith("UPDATE sessions SET revoked_at=now() WHERE sessions.revoked_at IS NULL AND ")
    assert "sessions.identity_link_id IN (SELECT identity_links.id FROM identity_links WHERE identity_links.provider = 'email')" in statements[1]
    assert statements[2].startswith("UPDATE email_login_challenges SET revoked_at=now() WHERE ")
    assert "consumed_at IS NULL AND email_login_challenges.revoked_at IS NULL" in statements[2]
    for table, drop, add in (("allowlist_entries", 3, 4), ("identity_links", 5, 6)):
        name = f"ck_{table}_email_subject_normalized"
        assert statements[drop] == f"ALTER TABLE {table} DROP CONSTRAINT {name}"
        assert statements[add].startswith(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK (provider <> 'email' OR (")
        assert "substr(subject, strpos(subject, '@')) = lower(substr(subject, strpos(subject, '@')))" in statements[add]
        assert "subject = lower(subject)" not in statements[add]
    assert statements[7].startswith("UPDATE alembic_version SET version_num='" + EMAIL_CASE + "'")
    assert len(statements) == 8  # nothing else is created, altered, updated or dropped
    assert "telegram" not in revision  # the data step names no Telegram row
    # the way back swaps the old rule in, and undoes no revocation
    assert down.count("DROP CONSTRAINT") == 2 and down.count("ADD CONSTRAINT") == 2
    assert down.count("CHECK (provider <> 'email' OR (subject = lower(subject) AND ") == 2
    assert "UPDATE allowlist_entries" not in down and "UPDATE sessions" not in down


def test_the_invitation_revision_renders_as_one_table_and_its_indexes():
    up, down = _offline_sql("up"), _offline_sql("down_from_head")
    revision = up.split(f"Running upgrade {EMAIL_CASE} -> {HEAD}")[1]

    statements = [re.sub(r"\s+", " ", part.strip()) for part in revision.split(";")]
    statements = [part for part in statements if part not in ("", "COMMIT")]
    assert statements[0].startswith("CREATE TABLE identity_invitations (")
    assert "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE" in statements[0]
    assert "CHECK (provider = 'telegram')" in statements[0]
    assert "CHECK (consumed_at IS NULL OR revoked_at IS NULL)" in statements[0]
    assert "CHECK (length(token_hash) = 64)" in statements[0]
    assert "CHECK (expires_at > created_at)" in statements[0]
    assert statements[1] == (
        "CREATE INDEX ix_identity_invitations_user_id_created_at ON identity_invitations "
        "(user_id, created_at)"
    )
    assert statements[2] == (
        "CREATE INDEX ix_identity_invitations_expires_at ON identity_invitations (expires_at)"
    )
    # PostgreSQL's own partial unique index, with the predicate of the SQLite one
    assert statements[3] == (
        "CREATE UNIQUE INDEX uq_identity_invitations_one_outstanding ON identity_invitations "
        "(user_id) WHERE consumed_at IS NULL AND revoked_at IS NULL"
    )
    assert statements[4].startswith("UPDATE alembic_version SET version_num='" + HEAD + "'")
    assert len(statements) == 5  # nothing else is created, altered, updated or dropped
    assert "ALTER TABLE" not in revision and "DROP" not in revision and "UPDATE users" not in revision
    assert re.findall(r"^DROP [^;]*", down, re.MULTILINE) == ["DROP TABLE identity_invitations"]


def test_rendering_sql_offline_creates_no_database_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    _offline_sql("up")

    assert list(tmp_path.iterdir()) == []


# The PostgreSQL schema cannot be created here, so the next best thing is to show
# that the migration and the models say the same thing in PostgreSQL's own SQL:
# every column, constraint, foreign key (with its ON DELETE) and
# index, line for line. The SQLite equivalent is the fingerprint comparison above.


def _tables_in(sql: str) -> dict[str, list[str]]:
    """table name -> its definition lines, normalized and sorted. A later
    ALTER TABLE ... ADD [COLUMN] adds its definition to the table's lines, so the
    result is the table as the whole history leaves it (and CREATE TABLE alone is
    the table as the models declare it)."""
    tables = {}
    for match in re.finditer(r"CREATE TABLE (\w+) \(\n(.*?)\n\)", sql, re.DOTALL):
        name, body = match.groups()
        tables[name] = [
            re.sub(r"\s+", " ", line.strip().rstrip(",")) for line in body.split("\n") if line.strip()
        ]
    for match in re.finditer(
        r"ALTER TABLE (\w+) (?:ADD (?:COLUMN )?(?P<added>[^;]*)|DROP CONSTRAINT (?P<dropped>\w+));", sql
    ):
        name = match.group(1)
        if match.group("added") is not None:
            tables[name].append(re.sub(r"\s+", " ", match.group("added").strip()))
        else:  # DROP CONSTRAINT: the definition goes (a later ADD may put another in its place)
            prefix = f"CONSTRAINT {match.group('dropped')} "
            kept = [line for line in tables[name] if not line.startswith(prefix)]
            assert len(kept) == len(tables[name]) - 1, (name, prefix)
            tables[name] = kept
    return {name: sorted(lines) for name, lines in tables.items()}


def _indexes_in(sql: str) -> list[str]:
    return sorted(
        re.sub(r"\s+", " ", m.group(0)).rstrip(";")
        for m in re.finditer(r"CREATE (?:UNIQUE )?INDEX [^\n]*", sql)
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
    assert len(_indexes_in(_models_as_postgresql())) == 17  # 15 plain, 2 unique


def test_the_postgresql_comparison_does_see_a_difference(monkeypatch):
    # Guard the guard: change one CHECK in what the migration renders and the
    # comparison must notice.
    sql = _offline_sql("up").replace("interval '8760 hours'", "interval '9999 hours'")

    assert _tables_in(sql)["messages"] != _tables_in(_models_as_postgresql())["messages"]


def test_the_postgresql_comparison_sees_the_provenance_revision_too():
    # ... including what 0002 adds with ALTER TABLE (folded into the tables).
    modeled = _tables_in(_models_as_postgresql())
    up = _offline_sql("up")
    for change in (
        lambda sql: sql.replace(
            "ON DELETE CASCADE;\n\nCREATE INDEX ix_sessions_identity_link_id",
            "ON DELETE SET NULL;\n\nCREATE INDEX ix_sessions_identity_link_id",
        ),
        lambda sql: sql.replace(
            "identity_link_id IS NOT NULL OR revoked_at IS NOT NULL", "identity_link_id IS NOT NULL"
        ),
    ):
        assert change(up) != up
        assert _tables_in(change(up))["sessions"] != modeled["sessions"]
    narrowed = up.replace("ON identity_links (user_id, id);", "ON identity_links (id);")
    assert narrowed != up
    assert _indexes_in(narrowed) != _indexes_in(_models_as_postgresql())


def test_the_postgresql_comparison_sees_the_email_case_revision_too():
    # ... including the CHECK that 0004 drops and adds back (folded into the tables).
    modeled = _tables_in(_models_as_postgresql())
    up = _offline_sql("up")
    for table in ("allowlist_entries", "identity_links"):
        assert _tables_in(up)[table] == modeled[table]  # the folded history is the model
        for change in (
            lambda sql: sql.replace("lower(substr(subject, strpos(subject, '@')))", "lower(subject)"),
            lambda sql: sql.replace("AND substr(subject, 1, 1) <> '@' ", ""),
        ):
            assert change(up) != up
            assert _tables_in(change(up))[table] != modeled[table]
    # the old rule left in place (the DROP not honoured) is a difference too
    undropped = re.sub(r"ALTER TABLE \w+ DROP CONSTRAINT ck_\w+_email_subject_normalized;", "", up)
    assert undropped != up
    assert _tables_in(undropped)["allowlist_entries"] != modeled["allowlist_entries"]


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
