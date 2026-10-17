"""Who owns what, and the rules on users, identities, logins and conversations.

Every test here runs against both a schema created from the models and one built
by the migrations (see tests/persistence_support.py).
"""

import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.persistence.models import (
    AllowlistEntry,
    Conversation,
    IdentityLink,
    Message,
    Session,
    TurnRequest,
    User,
)
from house_helper.persistence.allowlist import allow
from tests.persistence_support import (  # noqa: F401  (fixtures)
    CREATED,
    count,
    engine,
    factory,
    link_row,
    new_conversation,
    new_identity_link,
    new_login_session,
    new_message,
    new_oidc_state,
    new_turn_request,
    new_user,
    raw_insert,
    rejected,
    session,
    stored,
    utc,
)

# --- users ------------------------------------------------------------------------


def test_a_user_id_is_generated_by_the_server_and_is_a_random_uuid(session):
    first, second = new_user(session), new_user(session)

    assert isinstance(first.id, uuid.UUID)
    assert first.id.version == 4
    assert first.id != second.id


def test_a_user_round_trips_through_the_database(factory):
    with factory() as writer:
        user = new_user(writer, display_name="Елена")
        user_id = user.id
        writer.commit()

    with factory() as reader:
        loaded = reader.get(User, user_id)

        assert loaded.display_name == "Елена"
        assert loaded.disabled_at is None
        assert loaded.created_at.tzinfo is not None


# --- identity links ---------------------------------------------------------------
# What a link may be (provisioned identities only, the shared provider/subject rules)
# is in test_persistence_identity.py.


def test_an_external_identity_belongs_to_one_user(session):
    first, second = new_user(session), new_user(session)
    new_identity_link(session, first, provider="telegram", subject="sub-1")
    allow(session, second, "telegram", "sub-2")

    with rejected(session, "identity_links.provider, identity_links.subject"):
        raw_insert(session, "identity_links", **link_row(second, subject="sub-1"))


def test_a_user_can_have_several_identity_links(session):
    user = new_user(session)
    new_identity_link(session, user, subject="a")
    new_identity_link(session, user, subject="b")
    session.refresh(user)

    assert sorted(link.subject for link in user.identity_links) == ["a", "b"]


def test_an_identity_link_needs_an_owner_that_exists(session):
    with rejected(session, "FOREIGN KEY"):
        session.add(IdentityLink(user_id=uuid.uuid4(), provider="telegram", subject="x"))


# --- login sessions ---------------------------------------------------------------


def test_a_login_session_resolves_to_its_user_by_token_hash(session):
    user = new_user(session)
    login = new_login_session(session, user, token="secret-token")
    session.expire_all()

    found = session.scalar(sa.select(Session).where(Session.token_hash == login.token_hash))

    assert found.user.id == user.id


def test_a_token_hash_identifies_exactly_one_session(session):
    user = new_user(session)
    new_login_session(session, user, token="same")

    with rejected(session, "sessions.token_hash"):
        new_login_session(session, user, token="same")


@pytest.mark.parametrize("token_hash", ["short", "a" * 63, "a" * 65, ""])
def test_a_stored_token_hash_is_a_sha256_hex_digest_length(session, token_hash):
    user = new_user(session)

    with rejected(session, "ck_sessions_token_hash_length"):
        new_login_session(session, user, token_hash=token_hash)


def test_a_session_cannot_expire_before_it_was_created(session):
    user = new_user(session)
    now = utc(2026, 10, 16, 12)

    with rejected(session, "ck_sessions_expires_after_created"):
        new_login_session(session, user, created_at=now, expires_at=now)


def test_a_session_records_activity_and_revocation(session):
    user = new_user(session)
    login = new_login_session(session, user)

    assert login.revoked_at is None
    assert login.last_seen_at is not None


def test_a_session_needs_an_owner_that_exists(session):
    with rejected(session, "FOREIGN KEY"):
        now = utc(2026, 10, 16)
        session.add(
            Session(
                user_id=uuid.uuid4(),
                token_hash="a" * 64,
                created_at=now,
                expires_at=now + timedelta(days=1),
                revoked_at=now,  # no provenance, so only a revoked row is legal at all
            )
        )


# --- login sessions: provenance -----------------------------------------------------


def _session_row(user, token_hash="a" * 64, **overrides) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "user_id": user.id.hex,
        "token_hash": token_hash,
        "created_at": stored(CREATED),
        "last_seen_at": stored(CREATED),
        "expires_at": stored(CREATED + timedelta(days=1)),
        **overrides,
    }


def test_a_session_records_the_identity_link_it_was_opened_through(session):
    user = new_user(session)
    first = new_identity_link(session, user, subject="first")
    second = new_identity_link(session, user, subject="second")

    login = new_login_session(session, user, token="t", identity_link_id=second.id)
    session.expire_all()

    assert session.get(Session, login.id).identity_link_id == second.id != first.id


@pytest.mark.parametrize("how", ["orm", "sql"])
def test_a_session_cannot_come_through_the_link_of_another_user(session, how):
    owner, other = new_user(session), new_user(session)
    foreign_link = new_identity_link(session, other, subject="theirs")

    with rejected(session, "FOREIGN KEY"):
        if how == "orm":
            new_login_session(session, owner, identity_link_id=foreign_link.id)
        else:
            raw_insert(
                session, "sessions", **_session_row(owner, identity_link_id=foreign_link.id.hex)
            )


@pytest.mark.parametrize("how", ["orm", "sql"])
def test_a_session_cannot_point_at_a_link_that_does_not_exist(session, how):
    user = new_user(session)

    with rejected(session, "FOREIGN KEY"):
        if how == "orm":
            new_login_session(session, user, identity_link_id=uuid.uuid4())
        else:
            raw_insert(session, "sessions", **_session_row(user, identity_link_id=uuid.uuid4().hex))


@pytest.mark.parametrize("how", ["orm", "sql"])
def test_a_session_without_provenance_can_only_exist_revoked(session, how):
    user = new_user(session)

    with rejected(session, "ck_sessions_provenance_or_revoked"):
        if how == "orm":
            new_login_session(session, user, identity_link_id=None)
        else:
            raw_insert(session, "sessions", **_session_row(user))

    # a revoked one is legal: that is what the sessions that predate the column are
    revoked = new_login_session(
        session, user, token="legacy", identity_link_id=None, revoked_at=utc(2026, 10, 17)
    )
    assert revoked.identity_link_id is None


def test_a_session_without_provenance_cannot_be_made_live_again(session):
    user = new_user(session)
    new_login_session(
        session, user, token="legacy", identity_link_id=None, revoked_at=utc(2026, 10, 17)
    )

    with rejected(session, "ck_sessions_provenance_or_revoked"):
        session.execute(sa.update(Session).values(revoked_at=None))


def test_the_orm_does_not_rewrite_the_provenance_of_a_stored_session(session):
    user = new_user(session)
    first = new_identity_link(session, user, subject="first")
    second = new_identity_link(session, user, subject="second")
    login = new_login_session(session, user, identity_link_id=first.id)

    login.identity_link_id = second.id

    with pytest.raises(ValueError, match="identity_link_id cannot change"):
        session.flush()
    session.rollback()


def test_a_session_without_provenance_cannot_be_given_one_afterwards(session):
    user = new_user(session)
    link = new_identity_link(session, user)
    legacy = new_login_session(
        session, user, identity_link_id=None, revoked_at=utc(2026, 10, 17)
    )

    legacy.identity_link_id = link.id

    with pytest.raises(ValueError, match="identity_link_id cannot change"):
        session.flush()
    session.rollback()


def test_deleting_a_link_deletes_the_sessions_it_opened_and_only_those(session):
    user = new_user(session)
    gone = new_identity_link(session, user, subject="gone")
    kept = new_identity_link(session, user, subject="kept")
    new_login_session(session, user, token="from-gone", identity_link_id=gone.id)
    survivor = new_login_session(session, user, token="from-kept", identity_link_id=kept.id)

    session.execute(sa.delete(IdentityLink).where(IdentityLink.id == gone.id))

    assert [row.id for row in session.scalars(sa.select(Session))] == [survivor.id]


def test_deleting_the_allowlist_entry_takes_the_link_and_its_sessions_with_it(session):
    user = new_user(session)
    link = new_identity_link(session, user, subject="gone")
    new_login_session(session, user, identity_link_id=link.id)

    session.execute(sa.delete(AllowlistEntry).where(AllowlistEntry.subject == "gone"))

    assert count(session, IdentityLink) == 0 and count(session, Session) == 0
    assert count(session, User) == 1


# --- OIDC login state -------------------------------------------------------------


def test_an_oidc_state_value_can_be_used_to_start_only_one_login(session):
    new_oidc_state(session, state="same")

    with rejected(session, "oidc_states.state_hash"):
        new_oidc_state(session, state="same")


def test_an_oidc_state_is_single_use_and_short_lived(session):
    row = new_oidc_state(session)

    assert row.consumed_at is None
    assert row.expires_at - row.created_at == timedelta(minutes=10)


@pytest.mark.parametrize("state_hash", ["short", "a" * 65])
def test_a_stored_oidc_state_is_a_sha256_hex_digest_length(session, state_hash):
    with rejected(session, "ck_oidc_states_state_hash_length"):
        new_oidc_state(session, state_hash=state_hash)


def test_an_oidc_state_cannot_expire_before_it_was_created(session):
    now = utc(2026, 10, 16)

    with rejected(session, "ck_oidc_states_expires_after_created"):
        new_oidc_state(session, created_at=now, expires_at=now - timedelta(seconds=1))


def test_an_oidc_state_needs_its_nonce_and_code_verifier(session):
    with rejected(session, "NOT NULL constraint failed: oidc_states.nonce"):
        new_oidc_state(session, nonce=None)


# --- conversations belong to a user -----------------------------------------------


def test_a_user_can_have_many_conversations(session):
    user = new_user(session)
    for title in ("Борщ", "Плов", None):
        new_conversation(session, user, title=title)
    session.refresh(user)

    assert len(user.conversations) == 3
    assert all(conversation.user is user for conversation in user.conversations)


def test_conversations_are_listed_per_owner_most_recent_first(session):
    mine, theirs = new_user(session), new_user(session)
    old = new_conversation(session, mine, updated_at=utc(2026, 1, 1))
    new = new_conversation(session, mine, updated_at=utc(2026, 6, 1))
    other = new_conversation(session, theirs, updated_at=utc(2026, 9, 1))

    listed = session.scalars(
        sa.select(Conversation)
        .where(Conversation.user_id == mine.id)
        .order_by(Conversation.updated_at.desc())
    ).all()

    assert [c.id for c in listed] == [new.id, old.id]
    assert other.id not in {c.id for c in listed}


def test_a_conversation_is_not_found_through_another_users_scope(session):
    owner, stranger = new_user(session), new_user(session)
    conversation = new_conversation(session, owner)

    as_owner = session.scalar(
        sa.select(Conversation).where(
            Conversation.id == conversation.id, Conversation.user_id == owner.id
        )
    )
    as_stranger = session.scalar(
        sa.select(Conversation).where(
            Conversation.id == conversation.id, Conversation.user_id == stranger.id
        )
    )

    assert as_owner is not None
    assert as_stranger is None


def test_a_conversation_needs_an_owner_that_exists(session):
    with rejected(session, "FOREIGN KEY"):
        session.add(Conversation(user_id=uuid.uuid4()))


def test_a_conversation_cannot_be_ownerless(session):
    with rejected(session, "NOT NULL constraint failed: conversations.user_id"):
        session.add(Conversation(user_id=None))


# --- messages belong to a conversation --------------------------------------------


def test_messages_come_back_in_the_order_they_were_written(session):
    conversation = new_conversation(session, new_user(session))
    same_instant = utc(2026, 10, 16, 12)
    for text in ("first", "second", "third"):
        new_message(session, conversation, content=text, created_at=same_instant)
    session.refresh(conversation)

    assert [m.content for m in conversation.messages] == ["first", "second", "third"]


def test_message_ids_are_never_reused_after_the_newest_is_deleted(session):
    conversation = new_conversation(session, new_user(session))
    newest = new_message(session, conversation)
    newest_id = newest.id
    session.delete(newest)
    session.flush()

    following = new_message(session, conversation)

    assert following.id > newest_id


@pytest.mark.parametrize("role", ["user", "assistant", "system_note"])
def test_the_roles_of_a_conversation_are_accepted(session, role):
    message = new_message(session, new_conversation(session, new_user(session)), role=role)

    assert message.role == role


@pytest.mark.parametrize("role", ["system", "tool", "", "User", "System_Note", "note"])
def test_any_other_role_is_rejected(session, role):
    # "system" stays out: a system note is the application's marker, not a prompt.
    conversation = new_conversation(session, new_user(session))

    with rejected(session, "ck_messages_role_valid"):
        new_message(session, conversation, role=role)


def test_a_context_reset_is_a_durable_note_that_bounds_what_comes_after(factory):
    # The shape of the reset: write a system_note, and the context is whatever
    # follows the latest one. The ids give the order, so this needs no timestamp.
    with factory() as writer:
        conversation = new_conversation(writer, new_user(writer))
        new_message(writer, conversation, role="user", content="before")
        new_message(writer, conversation, role="assistant", content="before-reply")
        reset = new_message(writer, conversation, role="system_note", content="context reset")
        new_message(writer, conversation, role="user", content="after")
        conversation_id, reset_id = conversation.id, reset.id
        writer.commit()

    with factory() as reader:
        latest_note = reader.scalar(
            sa.select(sa.func.max(Message.id)).where(
                Message.conversation_id == conversation_id, Message.role == "system_note"
            )
        )
        context = reader.scalars(
            sa.select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.id > latest_note,
                Message.role.in_(["user", "assistant"]),
            )
            .order_by(Message.id)
        ).all()

        assert latest_note == reset_id
        assert [m.content for m in context] == ["after"]
        assert reader.get(Message, reset_id).content == "context reset"  # the note itself is kept


def test_a_message_needs_content(session):
    conversation = new_conversation(session, new_user(session))

    with rejected(session, "NOT NULL constraint failed: messages.content"):
        new_message(session, conversation, content=None)


def test_a_message_keeps_its_text_exactly(factory):
    text = "Как приготовить борщ?\n\n  Нужно: свёкла, капуста; 🍲"
    with factory() as writer:
        conversation = new_conversation(writer, new_user(writer))
        message_id = new_message(writer, conversation, content=text).id
        writer.commit()

    with factory() as reader:
        assert reader.get(Message, message_id).content == text


def test_a_message_needs_a_conversation_that_exists(session):
    with rejected(session, "FOREIGN KEY"):
        session.add(Message(conversation_id=uuid.uuid4(), role="user", content="x"))


# --- deleting cascades along ownership --------------------------------------------


def _populate(session, user):
    """One of everything beneath `user`; returns the conversation."""
    new_identity_link(session, user, subject=f"sub-{user.id}")
    new_login_session(session, user, token=f"token-{user.id}")
    conversation = new_conversation(session, user)
    asked = new_message(session, conversation, role="user")
    answered = new_message(session, conversation, role="assistant")
    new_turn_request(
        session,
        conversation,
        key=f"key-{user.id}",
        user_message_id=asked.id,
        assistant_message_id=answered.id,
    )
    return conversation


# What _populate() creates beneath one user.
PER_USER = {IdentityLink: 1, Session: 1, Conversation: 1, Message: 2, TurnRequest: 1}


def _counts(session):
    return {model: count(session, model) for model in PER_USER}


def test_deleting_a_user_through_the_orm_deletes_everything_they_own(session):
    leaving, staying = new_user(session), new_user(session)
    _populate(session, leaving)
    _populate(session, staying)

    session.delete(leaving)
    session.flush()

    assert _counts(session) == PER_USER  # exactly the other user's rows remain
    assert count(session, User) == 1


def test_deleting_a_user_in_the_database_cascades_without_the_orm(session):
    # The DELETE statement alone, no loaded objects: only ON DELETE CASCADE and
    # the foreign-key pragma can remove the children.
    leaving, staying = new_user(session), new_user(session)
    _populate(session, leaving)
    _populate(session, staying)

    session.execute(sa.delete(User).where(User.id == leaving.id))

    assert _counts(session) == PER_USER  # exactly the other user's rows remain
    assert count(session, User) == 1


def test_deleting_a_conversation_deletes_its_messages_and_turn_requests(session):
    user = new_user(session)
    doomed = _populate(session, user)
    kept = new_conversation(session, user)
    new_message(session, kept)

    session.execute(sa.delete(Conversation).where(Conversation.id == doomed.id))

    assert count(session, Message) == 1
    assert count(session, TurnRequest) == 0
    assert count(session, Conversation) == 1


def test_a_message_that_a_turn_request_links_is_not_deleted_from_under_it(session):
    # The links are NO ACTION, not SET NULL: a request keeps its input, so a purge
    # that forgets it fails instead of orphaning the request.
    # (test_persistence_references.py covers the whole rule.)
    conversation = new_conversation(session, new_user(session))
    asked = new_message(session, conversation, role="user")
    new_turn_request(session, conversation, user_message_id=asked.id)

    with rejected(session, "FOREIGN KEY"):
        session.execute(sa.delete(Message).where(Message.id == asked.id))


def test_one_users_data_is_untouched_by_another_users_deletion(session):
    leaving, staying = new_user(session), new_user(session)
    _populate(session, leaving)
    kept = _populate(session, staying)

    session.execute(sa.delete(User).where(User.id == leaving.id))
    session.expire_all()

    assert [m.role for m in session.get(Conversation, kept.id).messages] == ["user", "assistant"]
