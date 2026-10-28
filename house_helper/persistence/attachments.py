"""Photos kept with messages: storing one, attaching it to a message, reading it back.

What a photo is here. Bytes in the database (models.MessageAttachment says why), identified by
a random UUID, owned by one user, of one of three kinds that the BYTES decide (sniff): what the
sender called the file or what a header claimed is never believed. Nothing is decoded or
resized: the bytes are stored as they came, shown to the assistant as they came and handed back
to their owner as they came, so there is no image library to attack and no pixel count to
bomb. (The assistant's provider and the person's browser decode them, as they would a photo
from anywhere.)

Two stages, so that an upload and a message can be one thing or two:

  store() keeps a photo for a user, not yet attached to anything, for UPLOAD_LIFETIME. Each user
  may hold MAX_UNATTACHED of these at once (the cap is on what a flood of uploads can leave in
  the database), and every store() first deletes the unattached photos whose hour is over,
  whoever's they are: that is the clean-up of an upload that was never sent, and `purge-expired`
  does the same for any that survive a quiet period (admin.retention).

  attach() gives the photo to the message of a turn, in the transaction that creates the
  message (turns.begin_turn), and moves its deadline to the message's: from then on the picture
  is part of the conversation and lives and dies with it. It succeeds only for a photo of the
  same user, that is not attached yet and has not expired; anything else is AttachmentUnusable
  and the turn is not started (the exception rolls everything back).

Reading. get_for_user() returns the bytes of a photo that is the user's and has not expired,
and nothing otherwise: a photo that does not exist, belongs to somebody else or has expired is
the same None. refs_for_messages() answers, for the messages of a page of history or of a
context, which have a photo (id, kind, size; no bytes).

Writers write first, as everywhere in this layer (see persistence.database): store() starts
with a DELETE, attach() is one conditional UPDATE. Reads write nothing. Nothing here commits;
the caller owns the transaction. Results are plain values, not ORM objects.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.identifiers import ATTACHMENT_CONTENT_TYPES, MAX_ATTACHMENT_BYTES
from house_helper.persistence.models import MessageAttachment

# What an upload that is never sent to a message lives for.
UPLOAD_LIFETIME = timedelta(hours=1)

# How many photos a user may have stored and not yet attached.
MAX_UNATTACHED = 5

# The text of a message that is only a photo (the sender wrote nothing with it). It is stored as
# the message's text, so the history says something and the assistant is told what it is.
PHOTO_PLACEHOLDER = "[Фото]"


class AttachmentUnusable(Exception):
    """The photo cannot be attached: unknown, somebody else's, already attached or expired
    (not told apart)."""


class TooManyUploads(Exception):
    """The user already holds MAX_UNATTACHED photos that no message has taken."""


class NotAPhoto(ValueError):
    """The bytes are empty, too large, or not a JPEG, a PNG or a WebP."""


@dataclass(frozen=True)
class AttachmentRef:
    """A photo, as the history shows it: no bytes."""

    id: UUID
    content_type: str
    size_bytes: int


@dataclass(frozen=True)
class AttachmentFile:
    """A photo, with its bytes: what the owner is given back and what the assistant is shown."""

    id: UUID
    content_type: str
    data: bytes


def sniff(data: object) -> Optional[str]:
    """The kind of picture these bytes begin as, or None. Checks the signature the format
    starts with, and nothing about the rest: it says what the bytes claim to be in the only
    place a format states it, not that they are a sound picture."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 12:
        return None
    head = bytes(data[:12])
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def check_photo(data: object) -> str:
    """The kind of `data`, or NotAPhoto."""
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_ATTACHMENT_BYTES:
        raise NotAPhoto("size")
    kind = sniff(data)
    if kind is None or kind not in ATTACHMENT_CONTENT_TYPES:
        raise NotAPhoto("kind")
    return kind


def store(session: OrmSession, user_id: UUID, data: bytes, *, now: datetime) -> AttachmentRef:
    """Keep `data` as an unattached photo of the user. NotAPhoto for bytes that are not an
    acceptable photo, TooManyUploads at the cap (neither stores anything)."""
    kind = check_photo(data)
    session.execute(
        sa.delete(MessageAttachment)
        .where(MessageAttachment.message_id.is_(None), MessageAttachment.expires_at <= now)
        .execution_options(synchronize_session=False)
    )
    waiting = session.scalar(
        sa.select(sa.func.count())
        .select_from(MessageAttachment)
        .where(MessageAttachment.user_id == user_id, MessageAttachment.message_id.is_(None))
    )
    if waiting >= MAX_UNATTACHED:
        raise TooManyUploads
    row = MessageAttachment(
        user_id=user_id,
        content_type=kind,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=bytes(data),
        created_at=now,
        expires_at=now + UPLOAD_LIFETIME,
    )
    session.add(row)
    session.flush()
    return AttachmentRef(row.id, row.content_type, row.size_bytes)


def attach(
    session: OrmSession,
    user_id: UUID,
    attachment_id: UUID,
    message_id: int,
    *,
    expires_at: datetime,
    now: datetime,
) -> None:
    """Give the user's unattached, unexpired photo to the message and move its deadline to
    `expires_at` (the message's). AttachmentUnusable otherwise. The caller has created the
    message in a conversation of this user (turns.begin_turn does, under the lock)."""
    attached = session.execute(
        sa.update(MessageAttachment)
        .where(
            MessageAttachment.id == attachment_id,
            MessageAttachment.user_id == user_id,
            MessageAttachment.message_id.is_(None),
            MessageAttachment.expires_at > now,
        )
        .values(message_id=message_id, expires_at=expires_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if attached != 1:
        raise AttachmentUnusable


def discard(session: OrmSession, user_id: UUID, attachment_id: UUID) -> bool:
    """Delete the user's photo if no message has taken it (a turn that was refused leaves it
    unattached; there is no reason to keep it for the hour). A photo that is attached is not
    touched: it belongs to its message. True if a photo was deleted."""
    deleted = session.execute(
        sa.delete(MessageAttachment)
        .where(
            MessageAttachment.id == attachment_id,
            MessageAttachment.user_id == user_id,
            MessageAttachment.message_id.is_(None),
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    return deleted == 1


def get_for_user(
    session: OrmSession, user_id: UUID, attachment_id: UUID, *, now: datetime
) -> Optional[AttachmentFile]:
    row = session.execute(
        sa.select(MessageAttachment.id, MessageAttachment.content_type, MessageAttachment.data).where(
            MessageAttachment.id == attachment_id,
            MessageAttachment.user_id == user_id,
            MessageAttachment.expires_at > now,
        )
    ).first()
    if row is None:
        return None
    return AttachmentFile(row.id, row.content_type, bytes(row.data))


def refs_for_messages(
    session: OrmSession, message_ids: Iterable[int], *, now: datetime
) -> dict[int, AttachmentRef]:
    """message id -> the photo it carries, for those of `message_ids` that carry one that has
    not expired. The messages are the caller's (found through the user's conversation)."""
    ids = list(message_ids)
    if not ids:
        return {}
    rows = session.execute(
        sa.select(
            MessageAttachment.message_id,
            MessageAttachment.id,
            MessageAttachment.content_type,
            MessageAttachment.size_bytes,
        ).where(MessageAttachment.message_id.in_(ids), MessageAttachment.expires_at > now)
    ).all()
    return {row.message_id: AttachmentRef(row.id, row.content_type, row.size_bytes) for row in rows}


def files_for_messages(
    session: OrmSession, message_ids: Iterable[int], *, now: datetime
) -> dict[int, AttachmentFile]:
    """message id -> the photo with its bytes (see refs_for_messages)."""
    ids = list(message_ids)
    if not ids:
        return {}
    rows = session.execute(
        sa.select(
            MessageAttachment.message_id,
            MessageAttachment.id,
            MessageAttachment.content_type,
            MessageAttachment.data,
        ).where(MessageAttachment.message_id.in_(ids), MessageAttachment.expires_at > now)
    ).all()
    return {row.message_id: AttachmentFile(row.id, row.content_type, bytes(row.data)) for row in rows}
