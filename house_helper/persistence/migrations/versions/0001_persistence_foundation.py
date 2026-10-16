"""persistence foundation

The first schema: users, the allowlist, identity links, login sessions and OIDC
login state, conversations with their messages, and the turn requests that make
a turn idempotent and recoverable. The allowlist is keyed by (provider, subject)
so that Telegram and email entries share one table, and each entry belongs to
the pre-provisioned user it signs in; an identity link can only exist for an
entry of the same user; message roles include the system_note that marks a
context reset; turn_requests has the whole lifecycle (committing, cancelled,
failure_kind) up front, so growing into the turn engine needs no table rebuild,
and it links the user message it was made from, and the assistant message it
produced, through composite foreign keys that pin conversation and role.

Written out in full rather than imported from the models: a migration describes
the schema as it was when the migration was written, whatever the models become.
Constraint and index names are spelled out (op.f) for the same reason.

Two CHECK rules differ by database (printable ASCII, and the 365-day retention
ceiling); their SQL for each dialect is spelled out below and compared with the
models' rendering by tests/test_persistence_migrations.py.

Revision ID: 0001_persistence_foundation
Revises:
Create Date: 2026-10-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_persistence_foundation"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RETENTION_DAYS = 365


def _timestamp() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _message_id() -> sa.types.TypeEngine:
    # BIGINT on PostgreSQL; SQLite only auto-increments a column typed INTEGER.
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _is_sqlite() -> bool:
    # Also right in offline (--sql) mode, where there is no connection.
    return op.get_context().dialect.name == "sqlite"


def _printable_ascii(column: str) -> str:
    """Only the characters "!" to "~", at least one (no whitespace, no control
    character, nothing outside ASCII). SQLite: GLOB, plus the BLOB length to
    catch a NUL, which GLOB and length() stop at, plus typeof() to refuse a
    BLOB, which GLOB and length() read as text ('null' stays allowed for a
    nullable column). PostgreSQL: a regular expression (it cannot store a NUL or
    a BLOB in text)."""
    if _is_sqlite():
        return (
            f"(typeof({column}) IN ('text', 'null') "
            f"AND {column} NOT GLOB '*[^!-~]*' "
            f"AND length({column}) = length(CAST({column} AS BLOB)) "
            f"AND length({column}) >= 1)"
        )
    return f"{column} ~ '^[!-~]+$'"


# The canonical SQLite timestamp: "YYYY-MM-DD HH:MM:SS.ffffff", UTC, 26 characters.
_UTC_TEXT_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]"


def _canonical_utc_text(column: str) -> str:
    """SQLite: exactly the canonical text, and a real moment. typeof() because
    GLOB and length() read a BLOB as text; the BLOB length because both stop at a
    NUL; GLOB for the shape; and datetime(..., '+0 days') must return the first
    19 characters unchanged, because a bare datetime() reads "2026-02-30" as 2
    March and "24:00:00" as it stands."""
    return (
        f"typeof({column}) = 'text' "
        f"AND length(CAST({column} AS BLOB)) = 26 "
        f"AND {column} GLOB '{_UTC_TEXT_GLOB}' "
        f"AND datetime(substr({column}, 1, 19), '+0 days') = substr({column}, 1, 19)"
    )


def _within_retention(created: str, expires: str) -> str:
    """expires is no later than 365 days after created. SQLite keeps UTC text,
    which must be the canonical spelling (a timestamp with an offset or a "Z"
    compares wrongly as text, so it is refused): add the days to the first 19
    characters and append the fraction back, so the bound is exact to the
    microsecond; COALESCE because a CHECK passes on NULL and datetime() is NULL
    for non-dates. PostgreSQL: hours, not days, so a daylight-saving change
    cannot move it, on timestamptz, which has no spelling to get wrong."""
    if _is_sqlite():
        return (
            f"COALESCE({_canonical_utc_text(created)} "
            f"AND {_canonical_utc_text(expires)} "
            f"AND {expires} <= (datetime(substr({created}, 1, 19), '+{RETENTION_DAYS} days') "
            f"|| substr({created}, 20)), 0)"
        )
    return f"{expires} <= {created} + interval '{RETENTION_DAYS * 24} hours'"


def _identity_checks(table: str) -> list[sa.CheckConstraint]:
    """The rules for a (provider, subject) pair; the same on allowlist_entries
    and identity_links."""
    return [
        sa.CheckConstraint(
            "provider IN ('telegram', 'email')", name=op.f(f"ck_{table}_provider_valid")
        ),
        sa.CheckConstraint(
            "length(subject) BETWEEN 1 AND 255", name=op.f(f"ck_{table}_subject_length")
        ),
        sa.CheckConstraint(
            _printable_ascii("subject"), name=op.f(f"ck_{table}_subject_printable_ascii")
        ),
        sa.CheckConstraint(
            "provider <> 'email' OR (subject = lower(subject) "
            "AND length(subject) - length(replace(subject, '@', '')) = 1 "
            "AND substr(subject, 1, 1) <> '@' "
            "AND substr(subject, length(subject), 1) <> '@')",
            name=op.f(f"ck_{table}_email_subject_normalized"),
        ),
        sa.CheckConstraint(
            "provider <> 'telegram' OR replace(subject, '@', '') = subject",
            name=op.f(f"ck_{table}_telegram_subject_not_email"),
        ),
    ]


def _max_length(table: str, column: str, limit: int) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length({column}) <= {limit}", name=op.f(f"ck_{table}_{column}_max_length")
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("updated_at", _timestamp(), nullable=False),
        sa.Column("disabled_at", _timestamp(), nullable=True),
        _max_length("users", "display_name", 200),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )

    op.create_table(
        "allowlist_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("revoked_at", _timestamp(), nullable=True),
        *_identity_checks("allowlist_entries"),
        _max_length("allowlist_entries", "note", 500),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_allowlist_entries_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_allowlist_entries")),
        sa.UniqueConstraint(
            "provider", "subject", name=op.f("uq_allowlist_entries_provider_subject")
        ),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "subject",
            name=op.f("uq_allowlist_entries_user_id_provider_subject"),
        ),
    )
    op.create_index(op.f("ix_allowlist_entries_user_id"), "allowlist_entries", ["user_id"])

    op.create_table(
        "identity_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("last_login_at", _timestamp(), nullable=True),
        *_identity_checks("identity_links"),
        sa.CheckConstraint(
            _printable_ascii("email"), name=op.f("ck_identity_links_email_printable_ascii")
        ),
        _max_length("identity_links", "email", 320),
        sa.ForeignKeyConstraint(
            ["user_id", "provider", "subject"],
            [
                "allowlist_entries.user_id",
                "allowlist_entries.provider",
                "allowlist_entries.subject",
            ],
            name=op.f("fk_identity_links_allowlist_entry"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_identity_links_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_links")),
        sa.UniqueConstraint(
            "provider", "subject", name=op.f("uq_identity_links_provider_subject")
        ),
    )
    op.create_index(op.f("ix_identity_links_user_id"), "identity_links", ["user_id"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("last_seen_at", _timestamp(), nullable=False),
        sa.Column("expires_at", _timestamp(), nullable=False),
        sa.Column("revoked_at", _timestamp(), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_sessions_expires_after_created")
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64", name=op.f("ck_sessions_token_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"])
    op.create_index(op.f("ix_sessions_expires_at"), "sessions", ["expires_at"])

    op.create_table(
        "oidc_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("expires_at", _timestamp(), nullable=False),
        sa.Column("consumed_at", _timestamp(), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_oidc_states_expires_after_created")
        ),
        sa.CheckConstraint(
            "length(state_hash) = 64", name=op.f("ck_oidc_states_state_hash_length")
        ),
        _max_length("oidc_states", "provider", 64),
        _max_length("oidc_states", "nonce", 128),
        _max_length("oidc_states", "code_verifier", 128),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oidc_states")),
        sa.UniqueConstraint("state_hash", name=op.f("uq_oidc_states_state_hash")),
    )
    op.create_index(op.f("ix_oidc_states_expires_at"), "oidc_states", ["expires_at"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("updated_at", _timestamp(), nullable=False),
        _max_length("conversations", "title", 200),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        op.f("ix_conversations_user_id_updated_at"),
        "conversations",
        ["user_id", "updated_at"],
    )

    op.create_table(
        "messages",
        sa.Column("id", _message_id(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("expires_at", _timestamp(), nullable=False),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_messages_expires_after_created")
        ),
        sa.CheckConstraint(
            _within_retention("created_at", "expires_at"),
            name=op.f("ck_messages_expires_within_retention"),
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system_note')", name=op.f("ck_messages_role_valid")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "conversation_id", "id", "role", name=op.f("uq_messages_conversation_id_id_role")
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        op.f("ix_messages_conversation_id_id"), "messages", ["conversation_id", "id"]
    )
    op.create_index(op.f("ix_messages_expires_at"), "messages", ["expires_at"])

    op.create_table(
        "turn_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", _timestamp(), nullable=False),
        sa.Column("updated_at", _timestamp(), nullable=False),
        sa.Column("processing_started_at", _timestamp(), nullable=True),
        sa.Column("lease_expires_at", _timestamp(), nullable=True),
        sa.Column("finished_at", _timestamp(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("failure_kind", sa.String(length=16), nullable=True),
        sa.Column(
            "result_payload",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("user_message_id", _message_id(), nullable=False),
        sa.Column("user_message_role", sa.String(length=16), server_default="user", nullable=False),
        sa.Column("assistant_message_id", _message_id(), nullable=True),
        sa.Column(
            "assistant_message_role",
            sa.String(length=16),
            server_default="assistant",
            nullable=False,
        ),
        sa.Column("expires_at", _timestamp(), nullable=False),
        sa.CheckConstraint(
            "user_message_role = 'user'", name=op.f("ck_turn_requests_user_message_role_is_user")
        ),
        sa.CheckConstraint(
            "assistant_message_role = 'assistant'",
            name=op.f("ck_turn_requests_assistant_message_role_is_assistant"),
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name=op.f("ck_turn_requests_idempotency_key_length"),
        ),
        sa.CheckConstraint(
            _printable_ascii("idempotency_key"),
            name=op.f("ck_turn_requests_idempotency_key_printable_ascii"),
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64", name=op.f("ck_turn_requests_fingerprint_length")
        ),
        _max_length("turn_requests", "error_code", 64),
        sa.CheckConstraint(
            "status <> 'completed' OR (result_payload IS NOT NULL AND finished_at IS NOT NULL)",
            name=op.f("ck_turn_requests_completed_has_result"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR (error_code IS NOT NULL AND finished_at IS NOT NULL)",
            name=op.f("ck_turn_requests_failed_has_error"),
        ),
        sa.CheckConstraint(
            "status <> 'processing' OR "
            "(processing_started_at IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_turn_requests_processing_has_lease"),
        ),
        sa.CheckConstraint(
            "status <> 'committing' OR (result_payload IS NOT NULL AND "
            "processing_started_at IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_turn_requests_committing_has_reply_and_lease"),
        ),
        sa.CheckConstraint(
            "failure_kind IN ('generation', 'persistence')",
            name=op.f("ck_turn_requests_failure_kind_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND failure_kind IS NOT NULL) OR "
            "(status <> 'failed' AND failure_kind IS NULL)",
            name=op.f("ck_turn_requests_failure_kind_iff_failed"),
        ),
        sa.CheckConstraint(
            "failure_kind <> 'generation' OR result_payload IS NULL",
            name=op.f("ck_turn_requests_generation_failure_has_no_reply"),
        ),
        sa.CheckConstraint(
            "failure_kind <> 'persistence' OR result_payload IS NOT NULL",
            name=op.f("ck_turn_requests_persistence_failure_keeps_reply"),
        ),
        sa.CheckConstraint(
            "status <> 'cancelled' OR finished_at IS NOT NULL",
            name=op.f("ck_turn_requests_cancelled_has_finish"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR status IN ('completed', 'failed', 'cancelled')",
            name=op.f("ck_turn_requests_finished_only_when_terminal"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'committing', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_turn_requests_status_valid"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_turn_requests_attempt_count_non_negative")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_turn_requests_expires_after_created")
        ),
        sa.CheckConstraint(
            _within_retention("created_at", "expires_at"),
            name=op.f("ck_turn_requests_expires_within_retention"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "assistant_message_id", "assistant_message_role"],
            ["messages.conversation_id", "messages.id", "messages.role"],
            name=op.f("fk_turn_requests_assistant_message"),
            ondelete="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "user_message_id", "user_message_role"],
            ["messages.conversation_id", "messages.id", "messages.role"],
            name=op.f("fk_turn_requests_user_message"),
            ondelete="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_turn_requests_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_turn_requests")),
        sa.UniqueConstraint(
            "assistant_message_id", name=op.f("uq_turn_requests_assistant_message_id")
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "idempotency_key",
            name=op.f("uq_turn_requests_conversation_id_idempotency_key"),
        ),
        sa.UniqueConstraint("user_message_id", name=op.f("uq_turn_requests_user_message_id")),
    )
    op.create_index(
        op.f("ix_turn_requests_status_lease_expires_at"),
        "turn_requests",
        ["status", "lease_expires_at"],
    )
    op.create_index(op.f("ix_turn_requests_expires_at"), "turn_requests", ["expires_at"])


def downgrade() -> None:
    # A table's indexes go with it. Children before parents.
    op.drop_table("turn_requests")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("oidc_states")
    op.drop_table("sessions")
    op.drop_table("identity_links")
    op.drop_table("allowlist_entries")
    op.drop_table("users")
