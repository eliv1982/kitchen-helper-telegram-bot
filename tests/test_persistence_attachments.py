"""Photos kept with messages: what is stored, who may read it, how it joins a message, what the
assistant is shown, and when it is forgotten.

The functions under test are persistence.attachments, turns.begin_turn (the attaching) and
conversations.build_context / list_messages (the reading). Every test that takes the `factory`
fixture runs twice (tests/persistence_support.py): on a schema created from the models and on
one built by the migrations.
"""

import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from house_helper.persistence import allowlist
from house_helper.persistence import attachments as att
from house_helper.persistence import conversations as cv
from house_helper.persistence import turns
from house_helper.persistence.base import RETENTION
from house_helper.persistence.identifiers import MAX_ATTACHMENT_BYTES
from house_helper.persistence.models import (
    Conversation,
    Message,
    MessageAttachment,
    TurnRequest,
    User,
)
from tests.persistence_support import (  # noqa: F401  (fixtures)
    engine,
    factory,
    new_user,
    rejected,
    session,
    utc,
)
from tests.photo_support import NOT_PICTURES, jpeg, png, webp

NOW = utc(2026, 10, 28, 12)
LEASE = timedelta(seconds=90)


def run(factory, work):
    with factory() as db, db.begin():
        return work(db)


def enroll(db) -> User:
    user = new_user(db)
    allowlist.allow(db, user, "telegram", f"sub-{uuid4().hex[:12]}")
    return user


@pytest.fixture
def user_id(factory):
    return run(factory, lambda db: enroll(db).id)


@pytest.fixture
def other_user_id(factory):
    return run(factory, lambda db: enroll(db).id)


def store(factory, user, data, *, now=NOW):
    return run(factory, lambda db: att.store(db, user, data, now=now))


def conversation_of(factory, user, at=NOW):
    return run(factory, lambda db: cv.create_conversation(db, user, now=at)).id


def begin(factory, user, conversation, key, text, *, attachment=None, now=NOW):
    return run(
        factory,
        lambda db: turns.begin_turn(
            db, user, conversation, key, text,
            clock=lambda: now, lease=LEASE, max_context_messages=20, attachment_id=attachment,
        ),
    )


def finish(factory, claim, reply="Ответ", now=NOW):
    return run(factory, lambda db: turns.complete_turn(db, claim, reply, truncated=False, clock=lambda: now))


def rows(factory, model=MessageAttachment):
    with factory() as db:
        return list(db.scalars(sa.select(model)))


# --- what counts as a photo ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "data, kind",
    [(jpeg(), "image/jpeg"), (png(), "image/png"), (webp(), "image/webp"), (bytearray(jpeg()), "image/jpeg")],
)
def test_the_kind_is_decided_by_the_bytes(data, kind):
    assert att.sniff(data) == kind
    assert att.check_photo(data) == kind


@pytest.mark.parametrize("name", sorted(NOT_PICTURES))
def test_anything_else_is_not_a_photo_whatever_it_is_called(name):
    assert att.sniff(NOT_PICTURES[name]) is None
    with pytest.raises(att.NotAPhoto):
        att.check_photo(NOT_PICTURES[name])


@pytest.mark.parametrize("data", [None, "", "text", b"", b"\xff\xd8\xff", 12, [1, 2], object()])
def test_what_is_not_even_bytes_or_too_short_is_not_a_photo(data):
    assert att.sniff(data) is None
    with pytest.raises(att.NotAPhoto):
        att.check_photo(data)


def test_the_size_limit_is_exact():
    assert att.check_photo(jpeg(MAX_ATTACHMENT_BYTES)) == "image/jpeg"
    with pytest.raises(att.NotAPhoto):
        att.check_photo(jpeg(MAX_ATTACHMENT_BYTES + 1))


# --- storing ------------------------------------------------------------------------------------------


def test_a_stored_photo_keeps_its_bytes_exactly_and_is_not_attached_to_anything(factory, user_id):
    data = jpeg(5000, "a")

    ref = store(factory, user_id, data)

    [row] = rows(factory)
    assert (row.id, row.user_id, row.message_id) == (ref.id, user_id, None)
    assert (row.content_type, row.size_bytes, bytes(row.data)) == ("image/jpeg", 5000, data)
    assert row.sha256 == hashlib.sha256(data).hexdigest()
    assert (row.created_at, row.expires_at) == (NOW, NOW + att.UPLOAD_LIFETIME)
    assert ref == att.AttachmentRef(row.id, "image/jpeg", 5000)


def test_nothing_that_is_not_a_photo_is_stored(factory, user_id):
    for data in (NOT_PICTURES["html"], b"", jpeg(MAX_ATTACHMENT_BYTES + 1)):
        with pytest.raises(att.NotAPhoto):
            store(factory, user_id, data)

    assert rows(factory) == []


def test_a_user_may_hold_only_so_many_photos_that_no_message_has_taken(factory, user_id, other_user_id):
    for number in range(att.MAX_UNATTACHED):
        store(factory, user_id, jpeg(300, str(number)))

    with pytest.raises(att.TooManyUploads):
        store(factory, user_id, jpeg(300, "one too many"))
    store(factory, other_user_id, jpeg(300, "somebody else"))  # the cap is per person
    assert len(rows(factory)) == att.MAX_UNATTACHED + 1


def test_an_upload_that_was_never_sent_is_gone_with_the_next_upload_after_its_hour(factory, user_id, other_user_id):
    stale = store(factory, user_id, jpeg(300, "old"))
    fresh = store(factory, other_user_id, jpeg(300, "fresh"), now=NOW + att.UPLOAD_LIFETIME - timedelta(seconds=1))

    store(factory, other_user_id, jpeg(300, "later"), now=NOW + att.UPLOAD_LIFETIME)

    ids = {row.id for row in rows(factory)}
    assert stale.id not in ids and fresh.id in ids and len(ids) == 2


# --- attaching to a message -----------------------------------------------------------------------------------


def test_a_photo_joins_the_message_that_is_made_in_the_same_transaction_and_takes_its_deadline(factory, user_id):
    conversation = conversation_of(factory, user_id)
    ref = store(factory, user_id, jpeg(900, "x"))

    claim = begin(factory, user_id, conversation, "tg-1", att.PHOTO_PLACEHOLDER, attachment=ref.id)

    [row] = rows(factory)
    [message] = [m for m in rows(factory, Message)]
    assert row.message_id == message.id == claim.user_message_id
    assert row.expires_at == message.expires_at == NOW + RETENTION
    assert [part["type"] for part in claim.context[-1]["content"]] == ["text", "image"]


def test_a_photo_that_cannot_be_attached_stops_the_turn_and_leaves_nothing_behind(factory, user_id, other_user_id):
    conversation = conversation_of(factory, user_id)
    theirs = store(factory, other_user_id, jpeg(900, "theirs"))
    mine = store(factory, user_id, jpeg(900, "mine"))
    expired = store(factory, user_id, jpeg(900, "stale"), now=NOW - att.UPLOAD_LIFETIME - timedelta(seconds=1))
    taken = store(factory, user_id, jpeg(900, "taken"))
    finish(factory, begin(factory, user_id, conversation, "tg-1", "первое", attachment=taken.id))

    for number, wanted in enumerate((theirs.id, expired.id, taken.id, uuid4()), start=2):
        with pytest.raises(att.AttachmentUnusable):
            begin(factory, user_id, conversation, f"tg-{number}", "второе", attachment=wanted)

    # only the first turn (its question and its answer) exists: the refused ones stored nothing
    assert len(rows(factory, Message)) == 2 and len(rows(factory, TurnRequest)) == 1
    held = {row.id: row.message_id for row in rows(factory)}
    assert held[theirs.id] is None and held[mine.id] is None  # nobody's photo was taken by the failures


def test_a_photo_goes_to_one_message_only(factory, user_id):
    conversation = conversation_of(factory, user_id)
    ref = store(factory, user_id, jpeg(900))
    finish(factory, begin(factory, user_id, conversation, "tg-1", "раз", attachment=ref.id))

    with pytest.raises(att.AttachmentUnusable):
        begin(factory, user_id, conversation, "tg-2", "два", attachment=ref.id)


def test_a_repeat_of_a_key_is_the_same_request_and_a_key_with_another_photo_is_a_conflict(factory, user_id):
    conversation = conversation_of(factory, user_id)
    first, second = store(factory, user_id, jpeg(900, "1")), store(factory, user_id, jpeg(900, "2"))
    claim = begin(factory, user_id, conversation, "tg-1", "раз", attachment=first.id)
    finish(factory, claim)

    replay_with = begin(factory, user_id, conversation, "tg-1", "раз", attachment=first.id)
    replay_without = begin(factory, user_id, conversation, "tg-1", "раз")

    assert isinstance(replay_with, turns.CompletedTurn) and isinstance(replay_without, turns.CompletedTurn)
    assert replay_with.user_message.attachment.id == first.id == replay_without.user_message.attachment.id
    for other in (second.id, uuid4()):
        with pytest.raises(turns.IdempotencyKeyConflict):
            begin(factory, user_id, conversation, "tg-1", "раз", attachment=other)
    assert {row.id: row.message_id for row in rows(factory)}[second.id] is None


def test_a_key_that_had_no_photo_does_not_accept_one_later(factory, user_id):
    conversation = conversation_of(factory, user_id)
    ref = store(factory, user_id, jpeg(900))
    finish(factory, begin(factory, user_id, conversation, "tg-1", "без фото"))

    with pytest.raises(turns.IdempotencyKeyConflict):
        begin(factory, user_id, conversation, "tg-1", "без фото", attachment=ref.id)


# --- reading it back ----------------------------------------------------------------------------------------------


def test_a_photo_is_given_back_to_its_owner_as_the_bytes_it_was_stored_with(factory, user_id, other_user_id):
    data = png(1500, "owner")
    ref = store(factory, user_id, data)

    mine = run(factory, lambda db: att.get_for_user(db, user_id, ref.id, now=NOW))
    theirs = run(factory, lambda db: att.get_for_user(db, other_user_id, ref.id, now=NOW))
    nobody = run(factory, lambda db: att.get_for_user(db, user_id, uuid4(), now=NOW))

    assert mine == att.AttachmentFile(ref.id, "image/png", data)
    assert theirs is None and nobody is None


def test_a_photo_is_not_given_back_after_its_deadline(factory, user_id):
    conversation = conversation_of(factory, user_id)
    ref = store(factory, user_id, jpeg(900))
    begin(factory, user_id, conversation, "tg-1", "раз", attachment=ref.id)
    deadline = NOW + RETENTION

    before = run(factory, lambda db: att.get_for_user(db, user_id, ref.id, now=deadline - timedelta(microseconds=1)))
    at = run(factory, lambda db: att.get_for_user(db, user_id, ref.id, now=deadline))

    assert before is not None and at is None


def test_the_history_names_the_photo_of_a_message_and_does_not_carry_its_bytes(factory, user_id):
    conversation = conversation_of(factory, user_id)
    ref = store(factory, user_id, jpeg(900, "h"))
    finish(factory, begin(factory, user_id, conversation, "tg-1", "Что это?", attachment=ref.id))
    finish(factory, begin(factory, user_id, conversation, "tg-2", "просто текст"), "ок")

    page = run(factory, lambda db: cv.list_messages(db, user_id, conversation, now=NOW, limit=10))

    carried = [(m.role, m.text, m.attachment) for m in page.messages]
    assert carried[0] == ("user", "Что это?", ref)
    assert [m.attachment for m in page.messages[1:]] == [None, None, None]
    assert "data" not in repr(page)


def test_another_persons_history_does_not_show_the_photo(factory, user_id, other_user_id):
    conversation = conversation_of(factory, user_id)
    ref = store(factory, user_id, jpeg(900))
    begin(factory, user_id, conversation, "tg-1", "раз", attachment=ref.id)

    with pytest.raises(cv.ConversationNotFound):
        run(factory, lambda db: cv.list_messages(db, other_user_id, conversation, now=NOW, limit=10))


# --- what the assistant is shown -------------------------------------------------------------------------------------


def test_the_assistant_sees_the_newest_photos_and_only_a_word_about_older_ones(factory, user_id):
    conversation = conversation_of(factory, user_id)
    photos = []
    for number in range(4):
        ref = store(factory, user_id, jpeg(700, str(number)))
        photos.append(ref)
        finish(
            factory,
            begin(factory, user_id, conversation, f"tg-{number}", f"вопрос {number}", attachment=ref.id),
            f"ответ {number}",
            now=NOW + timedelta(seconds=number),
        )
    ref = store(factory, user_id, jpeg(700, "last"))

    claim = begin(factory, user_id, conversation, "tg-last", "а это?", attachment=ref.id, now=NOW + timedelta(minutes=1))

    contents = [message["content"] for message in claim.context]
    with_pictures = [c for c in contents if isinstance(c, list)]
    assert len(with_pictures) == cv.MAX_CONTEXT_IMAGES == 2
    assert contents[-1][1]["data"] == jpeg(700, "last") and contents[-1][1]["content_type"] == "image/jpeg"
    assert contents[-1][0] == {"type": "text", "text": "а это?"}
    assert with_pictures[0][1]["data"] == jpeg(700, "3")
    older = [c for c in contents if isinstance(c, str) and cv.PHOTO_NOT_SHOWN in c]
    assert len(older) == 3 and older[0].startswith("вопрос 0")
    assert claim.context[0]["role"] == "user"


def test_a_photo_with_no_caption_is_described_to_the_assistant_in_words(factory, user_id):
    conversation = conversation_of(factory, user_id)
    ref = store(factory, user_id, png(700))

    claim = begin(factory, user_id, conversation, "tg-1", att.PHOTO_PLACEHOLDER, attachment=ref.id)

    assert claim.context[-1]["content"][0] == {"type": "text", "text": cv.PHOTO_WITHOUT_CAPTION}
    page = run(factory, lambda db: cv.list_messages(db, user_id, conversation, now=NOW, limit=10))
    assert page.messages[0].text == att.PHOTO_PLACEHOLDER  # the history keeps what was stored


def test_a_photo_that_has_expired_is_only_text_to_the_assistant(factory, user_id):
    conversation = conversation_of(factory, user_id)
    ref = store(factory, user_id, jpeg(700))
    finish(factory, begin(factory, user_id, conversation, "tg-1", "старое", attachment=ref.id), "ответ")
    later = NOW + RETENTION - timedelta(seconds=1)  # (the message has not expired; its photo is about to)
    run(
        factory,
        lambda db: db.execute(
            sa.update(MessageAttachment).values(expires_at=NOW + timedelta(days=1))
        ),
    )

    claim = begin(factory, user_id, conversation, "tg-2", "новое", now=later)

    assert all(isinstance(message["content"], str) for message in claim.context)


def test_a_context_with_no_photos_is_exactly_what_it_was_before_photos_existed(factory, user_id):
    conversation = conversation_of(factory, user_id)
    finish(factory, begin(factory, user_id, conversation, "k-1", "раз"), "ответ раз")

    claim = begin(factory, user_id, conversation, "k-2", "два", now=NOW + timedelta(seconds=5))

    assert claim.context == [
        {"role": "user", "content": "раз"},
        {"role": "assistant", "content": "ответ раз"},
        {"role": "user", "content": "два"},
    ]


# --- what the database refuses on its own ----------------------------------------------------------------------------------


def test_the_database_refuses_a_row_that_breaks_its_own_rules(session):
    user = enroll(session)
    good = dict(user_id=user.id, content_type="image/jpeg", size_bytes=4, sha256="a" * 64,
                data=b"\xff\xd8\xff\xe0", created_at=NOW, expires_at=NOW + timedelta(hours=1))

    for change, constraint in [
        (dict(content_type="image/gif"), "content_type_valid"),
        (dict(content_type="text/html"), "content_type_valid"),
        (dict(size_bytes=0, data=b""), "size_bytes_valid"),
        (dict(size_bytes=MAX_ATTACHMENT_BYTES + 1, data=b"x" * (MAX_ATTACHMENT_BYTES + 1)), "size_bytes_valid"),
        (dict(size_bytes=5), "data_matches_size"),
        (dict(sha256="abc"), "sha256_length"),
        (dict(expires_at=NOW), "expires_after_created"),
    ]:
        with rejected(session, constraint):
            session.add(MessageAttachment(**{**good, **change}))


def test_the_database_allows_one_photo_per_message_and_many_unattached(session):
    user = enroll(session)
    conversation = Conversation(user_id=user.id)
    session.add(conversation)
    session.flush()
    message = Message(conversation_id=conversation.id, role="user", content="x")
    session.add(message)
    session.flush()

    def photo(message_id=None, **extra):
        return MessageAttachment(
            user_id=user.id, content_type="image/png", size_bytes=3, sha256="b" * 64, data=b"abc",
            created_at=NOW, expires_at=NOW + timedelta(hours=1), message_id=message_id, **extra,
        )

    session.add_all([photo(), photo(), photo(message.id)])
    session.flush()
    with rejected(session, "message_attachments.message_id"):
        session.add(photo(message.id))


def test_a_stored_photo_cannot_be_rewritten_through_the_orm(session):
    user = enroll(session)
    row = MessageAttachment(
        user_id=user.id, content_type="image/png", size_bytes=3, sha256="b" * 64, data=b"abc",
        created_at=NOW, expires_at=NOW + timedelta(hours=1),
    )
    session.add(row)
    session.flush()

    row.data = b"xyz"
    with pytest.raises(ValueError, match="cannot change once stored"):
        session.flush()
    session.rollback()


def test_deleting_a_message_or_a_user_deletes_its_photos(factory, user_id, other_user_id):
    conversation = conversation_of(factory, user_id)
    other_conversation = conversation_of(factory, other_user_id)
    mine = store(factory, user_id, jpeg(700, "m"))
    theirs = store(factory, other_user_id, jpeg(700, "t"))
    begin(factory, user_id, conversation, "tg-1", "раз", attachment=mine.id)
    begin(factory, other_user_id, other_conversation, "tg-1", "раз", attachment=theirs.id)

    run(factory, lambda db: db.execute(sa.delete(TurnRequest).where(TurnRequest.conversation_id == conversation)))
    run(factory, lambda db: db.execute(sa.delete(Message).where(Message.conversation_id == conversation)))
    assert {row.id for row in rows(factory)} == {theirs.id}

    run(factory, lambda db: db.execute(sa.delete(User).where(User.id == other_user_id)))
    assert rows(factory) == []
