"""What `purge-expired` does with the Telegram tables: pairing codes go at their own deadline, a
link never goes (it is a person's choice, removed by unlinking or with the user), and a photo goes
with its message.

The rules for the rest of the history are tests/test_admin_retention.py; the photo storage is
tests/test_persistence_attachments.py.
"""

from datetime import timedelta

from house_helper.persistence.authn import hash_secret
from house_helper.persistence.models import MessageAttachment, TelegramLink, TelegramLinkCode
from tests.admin_support import db_path, db_url, migrated_template  # noqa: F401  (fixtures)
from tests.photo_support import jpeg
from tests.retention_support import MICROSECOND, NOW, ago, count, read, seeded
from tests.test_admin_retention import present, purge


def code(user, name, *, created, expires, consumed_at=None):
    return TelegramLinkCode(
        user_id=user.id,
        code_hash=hash_secret(name),
        created_at=created,
        expires_at=expires,
        consumed_at=consumed_at,
    )


def photo(user, *, message_id, created, expires, tag="x"):
    data = jpeg(300, tag)
    return MessageAttachment(
        user_id=user.id,
        message_id=message_id,
        content_type="image/jpeg",
        size_bytes=len(data),
        sha256=hash_secret(tag),
        data=data,
        created_at=created,
        expires_at=expires,
    )


def test_a_pairing_code_goes_at_its_own_deadline_used_or_not_and_a_link_stays(db_url):
    def fill(s):
        user = s.user()
        link = TelegramLink(user_id=user.id, telegram_user_id=555000111, display_name="@mama", created_at=ago(days=400))
        s.session.add(link)
        gone = [
            code(user, "expired", created=ago(minutes=30), expires=ago(minutes=20)),
            code(user, "used and expired", created=ago(minutes=30), expires=ago(minutes=20), consumed_at=ago(minutes=25)),
            code(user, "at the deadline", created=ago(minutes=10), expires=NOW),
        ]
        kept = [
            code(user, "live", created=ago(minutes=1), expires=NOW + timedelta(minutes=9)),
            code(user, "used but live", created=ago(minutes=1), expires=NOW + timedelta(minutes=9), consumed_at=ago(seconds=30)),
            code(user, "a moment left", created=ago(minutes=10), expires=NOW + MICROSECOND),
        ]
        s.session.add_all(gone + kept)
        s.session.flush()
        return [row.id.hex for row in gone], [row.id.hex for row in kept]

    gone, kept = seeded(db_url, fill)

    dry = purge(db_url, dry_run=True)
    report = purge(db_url)

    assert dry.deleted["telegram_link_codes"] == report.deleted["telegram_link_codes"] == 3
    left = present(db_url, "telegram_link_codes")
    assert not set(gone) & left and set(kept) <= left and len(left) == 3
    assert count(db_url, "telegram_links") == 1  # a link has no deadline


def test_a_photo_goes_with_its_message_and_not_before_and_an_upload_that_was_never_sent_goes_after_its_hour(db_url):
    def fill(s):
        user = s.user()
        conversation = s.conversation(user, created=ago(days=500), updated=ago(days=1))
        old = s.message(conversation, "user", "старое фото", created=ago(days=366))
        recent = s.message(conversation, "user", "недавнее фото", created=ago(days=2))
        rows = {
            "with an expired message": photo(user, message_id=old.id, created=ago(days=366), expires=old.expires_at, tag="old"),
            "with a recent message": photo(user, message_id=recent.id, created=ago(days=2), expires=recent.expires_at, tag="recent"),
            "never sent, hour over": photo(user, message_id=None, created=ago(hours=3), expires=ago(hours=2), tag="stale"),
            "never sent, hour left": photo(user, message_id=None, created=ago(minutes=5), expires=NOW + timedelta(minutes=55), tag="fresh"),
        }
        s.session.add_all(rows.values())
        s.session.flush()
        return {name: row.id.hex for name, row in rows.items()}

    ids = seeded(db_url, fill)

    report = purge(db_url)

    left = present(db_url, "message_attachments")
    assert left == {ids["with a recent message"], ids["never sent, hour left"]}
    assert report.deleted["message_attachments"] == 2
    assert report.deleted["messages"] == 1  # the old message went too, and its photo had gone first
    assert len(read(db_url, "SELECT id FROM messages")) == 1


def test_the_second_run_finds_nothing_more_to_do(db_url):
    seeded(db_url, lambda s: s.session.add(code(s.user(), "old", created=ago(hours=1), expires=ago(minutes=50))))

    first = purge(db_url)
    second = purge(db_url)

    assert first.deleted["telegram_link_codes"] == 1 and second.total == 0
