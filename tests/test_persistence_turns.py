"""TurnRequest: idempotency keys, durable status, recovery metadata, stored result.

The turn-processing state machine is a later increment (R5); these tests pin what
the schema itself guarantees for it to build on. Moving a request between statuses
through ORM attributes, as several tests do, shows what the CHECKs allow and that a
row survives each step. It does not show that a transition is safe when two workers
compete; that needs atomic compare-and-swap updates and tests with competing
connections, listed in TurnRequest's docstring. What a request may link, and that it
always has its input, is in test_persistence_references.py.
"""

import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.persistence.models import Conversation, FailureKind, TurnRequest, TurnStatus
from tests.persistence_support import (  # noqa: F401  (fixtures)
    digest,
    engine,
    factory,
    insert_turn,
    new_conversation,
    new_message,
    new_turn_request,
    new_user,
    rejected,
    session,
    utc,
)

NOW = utc(2026, 10, 16, 12)


@pytest.fixture
def conversation(session):
    return new_conversation(session, new_user(session))


# --- idempotency ------------------------------------------------------------------


def test_the_same_key_cannot_open_two_turns_in_one_conversation(session, conversation):
    new_turn_request(session, conversation, key="retry-me")

    with rejected(session, "turn_requests.conversation_id, turn_requests.idempotency_key"):
        new_turn_request(session, conversation, key="retry-me")


def test_two_conversations_of_one_user_may_use_the_same_key(session):
    user = new_user(session)

    new_turn_request(session, new_conversation(session, user), key="k")
    new_turn_request(session, new_conversation(session, user), key="k")

    assert session.scalar(sa.select(sa.func.count()).select_from(TurnRequest)) == 2


def test_two_users_may_use_the_same_key(session):
    new_turn_request(session, new_conversation(session, new_user(session)), key="k")
    new_turn_request(session, new_conversation(session, new_user(session)), key="k")

    assert session.scalar(sa.select(sa.func.count()).select_from(TurnRequest)) == 2


def test_a_duplicate_can_be_caught_and_the_original_looked_up(session, conversation):
    # The shape of an idempotent retry: try to insert, lose to the existing row,
    # carry on in the same transaction and read what the first attempt stored.
    original = new_turn_request(session, conversation, key="retry-me", request_fingerprint=digest("a"))
    original_id = original.id

    with pytest.raises(sa.exc.IntegrityError):
        with session.begin_nested():
            session.add(
                TurnRequest(
                    conversation_id=conversation.id,
                    idempotency_key="retry-me",
                    request_fingerprint=digest("b"),
                )
            )

    existing = session.scalar(
        sa.select(TurnRequest).where(
            TurnRequest.conversation_id == conversation.id,
            TurnRequest.idempotency_key == "retry-me",
        )
    )
    assert existing.id == original_id
    assert existing.request_fingerprint == digest("a")  # a different body is detectable


def test_an_empty_idempotency_key_is_rejected_by_the_application(session, conversation):
    with pytest.raises(ValueError, match="1 to 128 characters"):
        new_turn_request(session, conversation, key="")


def test_an_empty_idempotency_key_is_rejected_by_the_database(session, conversation):
    asked = new_message(session, conversation)

    with rejected(session, "ck_turn_requests_idempotency_key_length"):
        insert_turn(session, "sql", conversation, asked.id, idempotency_key="")


@pytest.mark.parametrize("fingerprint", ["", "abc", "a" * 63, "a" * 65])
def test_the_request_fingerprint_is_a_sha256_hex_digest_length(session, conversation, fingerprint):
    with rejected(session, "ck_turn_requests_fingerprint_length"):
        new_turn_request(session, conversation, request_fingerprint=fingerprint)


@pytest.mark.parametrize("how", ["orm", "sql"])
def test_a_turn_request_needs_a_conversation_that_exists(session, conversation, how):
    asked = new_message(session, conversation)

    with rejected(session, "FOREIGN KEY"):
        insert_turn(session, how, conversation, asked.id, conversation_id=uuid.uuid4())


# --- a new request ----------------------------------------------------------------


def test_a_new_turn_request_starts_pending_with_nothing_recorded(session, conversation):
    turn = new_turn_request(session, conversation)

    assert turn.status == TurnStatus.PENDING.value
    assert turn.attempt_count == 0
    assert turn.processing_started_at is None
    assert turn.lease_expires_at is None
    assert turn.finished_at is None
    assert turn.error_code is None
    assert turn.result_payload is None
    assert turn.user_message_id is not None  # it is born with the message it was made from
    assert turn.assistant_message_id is None  # the answer does not exist yet
    assert turn.created_at is not None and turn.updated_at is not None


def test_the_database_itself_supplies_the_pending_status_and_zero_attempts(session, conversation):
    # A row inserted by SQL that names neither column, as a migration or a
    # support script might: the server defaults fill them in. (Its timestamps are
    # written in the canonical form, microseconds included: the retention CHECK
    # accepts no other, see test_persistence_retention.py.)
    asked = new_message(session, conversation)
    session.execute(
        sa.text(
            "INSERT INTO turn_requests (id, conversation_id, idempotency_key,"
            " request_fingerprint, created_at, updated_at, expires_at, user_message_id)"
            " VALUES ('00000000000000000000000000000001', :conversation, 'k', :fp,"
            " '2026-10-16 12:00:00.000000', '2026-10-16 12:00:00.000000',"
            " '2027-10-16 12:00:00.000000', :asked)"
        ),
        {"conversation": conversation.id.hex, "fp": digest("k"), "asked": asked.id},
    )

    row = session.execute(sa.text("SELECT status, attempt_count FROM turn_requests")).one()

    assert tuple(row) == ("pending", 0)


# --- status -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", ["done", "PENDING", "", "canceled", "running", "failed_generation", "reconciling"]
)
def test_an_unknown_status_is_rejected(session, conversation, status):
    with rejected(session, "ck_turn_requests_status_valid"):
        new_turn_request(session, conversation, status=status)


def test_a_negative_attempt_count_is_rejected(session, conversation):
    with rejected(session, "ck_turn_requests_attempt_count_non_negative"):
        new_turn_request(session, conversation, attempt_count=-1)


def test_a_processing_turn_must_say_when_it_started_and_when_its_lease_runs_out(session, conversation):
    with rejected(session, "ck_turn_requests_processing_has_lease"):
        new_turn_request(session, conversation, status="processing")


def test_a_processing_turn_without_a_lease_is_rejected_even_if_it_has_a_start(session, conversation):
    with rejected(session, "ck_turn_requests_processing_has_lease"):
        new_turn_request(session, conversation, status="processing", processing_started_at=NOW)


def test_a_completed_turn_must_hold_its_result_and_finish_time(session, conversation):
    with rejected(session, "ck_turn_requests_completed_has_result"):
        new_turn_request(session, conversation, status="completed", finished_at=NOW)


def test_a_completed_turn_must_have_a_finish_time(session, conversation):
    with rejected(session, "ck_turn_requests_completed_has_result"):
        new_turn_request(session, conversation, status="completed", result_payload={"ok": 1})


def test_a_none_payload_is_stored_as_no_payload_not_as_json_null(session, conversation):
    # With the default JSON handling None would be stored as the JSON value
    # null, which is "present", and would satisfy the completed-has-result rule.
    with rejected(session, "ck_turn_requests_completed_has_result"):
        new_turn_request(
            session, conversation, status="completed", finished_at=NOW, result_payload=None
        )


def test_a_failed_turn_must_say_why_and_when(session, conversation):
    with rejected(session, "ck_turn_requests_failed_has_error"):
        new_turn_request(
            session, conversation, status="failed", failure_kind="generation", finished_at=NOW
        )
    conversation = new_conversation(session, new_user(session))
    with rejected(session, "ck_turn_requests_failed_has_error"):
        new_turn_request(
            session, conversation, status="failed", failure_kind="generation", error_code="timeout"
        )


# --- the lifecycle: six statuses, one failure_kind ---------------------------------

REPLY = {"outcome": "reply", "text": "Варите 40 минут."}
LEASED = {
    "processing_started_at": NOW,
    "lease_expires_at": NOW + timedelta(minutes=2),
    "attempt_count": 1,
}

# One row for every scenario the turn engine must be able to describe.
SCENARIOS = {
    "accepted": {},
    "running": {"status": "processing", **LEASED},
    "reply being committed": {"status": "committing", "result_payload": REPLY, **LEASED},
    "reply committed": {
        "status": "completed", "result_payload": REPLY, "finished_at": NOW, "attempt_count": 1,
    },
    "generation failed": {
        "status": "failed", "failure_kind": "generation", "error_code": "backend_timeout",
        "finished_at": NOW, "attempt_count": 1,
    },
    "persistence failed": {
        "status": "failed", "failure_kind": "persistence", "error_code": "commit_failed",
        "result_payload": REPLY, "finished_at": NOW, "attempt_count": 1,
    },
    "cancelled": {"status": "cancelled", "finished_at": NOW},
}
TERMINAL = {"completed", "failed", "cancelled"}


def test_every_lifecycle_scenario_is_stored_and_told_apart_by_status_and_failure_kind(
    session, conversation
):
    for name, fields in SCENARIOS.items():
        new_turn_request(session, conversation, key=name.replace(" ", "-"), **fields)

    stored = session.scalars(sa.select(TurnRequest)).all()
    signatures = {(turn.status, turn.failure_kind) for turn in stored}

    assert len(stored) == len(SCENARIOS) == 7
    assert len(signatures) == 7  # no two scenarios share a (status, failure_kind)
    assert {status for status, _ in signatures} == {status.value for status in TurnStatus}
    assert {kind for _, kind in signatures} == {None, "generation", "persistence"}
    # "is it over" is one plain column, and it agrees with the status.
    assert all((turn.finished_at is not None) == (turn.status in TERMINAL) for turn in stored)


def test_the_failure_kinds_are_the_ones_the_model_names():
    assert {kind.value for kind in FailureKind} == {"generation", "persistence"}


@pytest.mark.parametrize("kind", ["", "Generation", "timeout", "cancelled"])
def test_an_unknown_failure_kind_is_rejected(session, conversation, kind):
    with rejected(session, "ck_turn_requests_failure_kind_valid"):
        new_turn_request(
            session, conversation, status="failed", failure_kind=kind,
            error_code="x", finished_at=NOW,
        )


def test_a_failed_turn_must_say_which_kind_of_failure(session, conversation):
    with rejected(session, "ck_turn_requests_failure_kind_iff_failed"):
        new_turn_request(session, conversation, status="failed", error_code="x", finished_at=NOW)


@pytest.mark.parametrize(
    "kind, payload", [("generation", None), ("persistence", REPLY)]
)
def test_a_turn_that_has_not_failed_has_no_failure_kind(session, conversation, kind, payload):
    # The payload is the one that kind would carry, so that only the status is wrong.
    with rejected(session, "ck_turn_requests_failure_kind_iff_failed"):
        new_turn_request(session, conversation, failure_kind=kind, result_payload=payload)


def test_a_failed_generation_has_no_reply(session, conversation):
    with rejected(session, "ck_turn_requests_generation_failure_has_no_reply"):
        new_turn_request(
            session, conversation, status="failed", failure_kind="generation",
            error_code="x", finished_at=NOW, result_payload=REPLY,
        )


def test_a_failed_persistence_keeps_the_reply_it_could_not_commit(session, conversation):
    with rejected(session, "ck_turn_requests_persistence_failure_keeps_reply"):
        new_turn_request(
            session, conversation, status="failed", failure_kind="persistence",
            error_code="x", finished_at=NOW,
        )


def test_a_committing_turn_holds_the_reply_it_is_committing(session, conversation):
    with rejected(session, "ck_turn_requests_committing_has_reply_and_lease"):
        new_turn_request(session, conversation, status="committing", **LEASED)


@pytest.mark.parametrize("missing", ["processing_started_at", "lease_expires_at"])
def test_a_committing_turn_holds_a_lease_so_a_dead_committer_is_found(
    session, conversation, missing
):
    fields = {**LEASED, "result_payload": REPLY, missing: None}

    with rejected(session, "ck_turn_requests_committing_has_reply_and_lease"):
        new_turn_request(session, conversation, status="committing", **fields)


def test_a_cancelled_turn_says_when(session, conversation):
    with rejected(session, "ck_turn_requests_cancelled_has_finish"):
        new_turn_request(session, conversation, status="cancelled")


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"status": "processing", **LEASED},
        {"status": "committing", "result_payload": REPLY, **LEASED},
    ],
    ids=["pending", "processing", "committing"],
)
def test_a_turn_that_is_still_in_flight_has_no_finish_time(session, conversation, fields):
    with rejected(session, "ck_turn_requests_finished_only_when_terminal"):
        new_turn_request(session, conversation, finished_at=NOW, **fields)


# --- durable transitions ----------------------------------------------------------


def test_a_turn_can_move_through_its_statuses_and_each_step_is_durable(factory):
    with factory() as s:
        conversation = new_conversation(s, new_user(s))
        turn = new_turn_request(s, conversation, key="durable")
        turn_id = turn.id
        s.commit()

    with factory() as s:  # claimed by a worker
        turn = s.get(TurnRequest, turn_id)
        turn.status = TurnStatus.PROCESSING.value
        turn.attempt_count += 1
        turn.processing_started_at = NOW
        turn.lease_expires_at = NOW + timedelta(minutes=2)
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        assert turn.status == "processing"
        assert (turn.attempt_count, turn.lease_expires_at) == (1, NOW + timedelta(minutes=2))
        turn.status = TurnStatus.COMPLETED.value
        turn.finished_at = NOW + timedelta(seconds=5)
        turn.result_payload = {"outcome": "reply", "text": "Варите 40 минут."}
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        assert turn.status == "completed"
        assert turn.finished_at == NOW + timedelta(seconds=5)
        assert turn.result_payload == {"outcome": "reply", "text": "Варите 40 минут."}


def test_a_failed_turn_keeps_its_error_code(factory):
    with factory() as s:
        turn = new_turn_request(s, new_conversation(s, new_user(s)))
        turn.status = TurnStatus.FAILED.value
        turn.failure_kind = FailureKind.GENERATION.value
        turn.error_code = "backend_timeout"
        turn.finished_at = NOW
        turn_id = turn.id
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        assert (turn.status, turn.failure_kind, turn.error_code) == (
            "failed", "generation", "backend_timeout",
        )


def test_updated_at_moves_when_the_row_changes(factory):
    with factory() as s:
        turn = new_turn_request(s, new_conversation(s, new_user(s)), updated_at=NOW)
        turn_id = turn.id
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        turn.attempt_count = 1
        s.commit()

    with factory() as s:
        assert s.get(TurnRequest, turn_id).updated_at > NOW


def test_the_result_payload_round_trips_nested_json_and_unicode(factory):
    payload = {
        "outcome": "reply",
        "text": "Борщ: свёкла, капуста, 🍲",
        "messages": [{"role": "user", "id": 1}, {"role": "assistant", "id": 2}],
        "usage": None,
    }
    with factory() as s:
        turn = new_turn_request(s, new_conversation(s, new_user(s)))
        turn.status, turn.finished_at, turn.result_payload = "completed", NOW, payload
        turn_id = turn.id
        s.commit()

    with factory() as s:
        assert s.get(TurnRequest, turn_id).result_payload == payload


# --- recovery metadata ------------------------------------------------------------


def test_turns_abandoned_by_a_dead_worker_can_be_found_by_their_expired_lease(session):
    conversation = new_conversation(session, new_user(session))
    started = NOW - timedelta(minutes=10)
    abandoned = new_turn_request(
        session, conversation, key="abandoned", status="processing",
        processing_started_at=started, lease_expires_at=NOW - timedelta(minutes=8), attempt_count=1,
    )
    new_turn_request(
        session, conversation, key="alive", status="processing",
        processing_started_at=NOW, lease_expires_at=NOW + timedelta(minutes=2), attempt_count=1,
    )
    new_turn_request(session, conversation, key="waiting")

    stuck = session.scalars(
        sa.select(TurnRequest).where(
            TurnRequest.status == TurnStatus.PROCESSING.value,
            TurnRequest.lease_expires_at < NOW,
        )
    ).all()

    assert [turn.id for turn in stuck] == [abandoned.id]


def test_a_recovered_turn_records_another_attempt_and_a_new_lease(factory):
    with factory() as s:
        turn = new_turn_request(
            s, new_conversation(s, new_user(s)), status="processing",
            processing_started_at=NOW - timedelta(minutes=10),
            lease_expires_at=NOW - timedelta(minutes=8), attempt_count=1,
        )
        turn_id = turn.id
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        turn.attempt_count += 1
        turn.processing_started_at = NOW
        turn.lease_expires_at = NOW + timedelta(minutes=2)
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        assert turn.attempt_count == 2
        assert turn.lease_expires_at == NOW + timedelta(minutes=2)


def test_a_turn_remembers_how_far_it_got_through_its_message_ids(session, conversation):
    asked = new_message(session, conversation, role="user")
    turn = new_turn_request(session, conversation, user_message_id=asked.id)
    session.refresh(turn)

    assert turn.user_message.content == "hello"
    assert turn.assistant_message is None  # the answer was never stored


# --- crash recovery and reconciliation --------------------------------------------

LATER = NOW + timedelta(hours=1)


def test_turns_abandoned_in_either_leased_stage_are_found_by_their_expired_lease(session):
    conversation = new_conversation(session, new_user(session))
    dead = {
        "processing_started_at": NOW - timedelta(minutes=10),
        "lease_expires_at": NOW - timedelta(minutes=8),
        "attempt_count": 1,
    }
    generating = new_turn_request(session, conversation, key="generating", status="processing", **dead)
    committing = new_turn_request(
        session, conversation, key="committing", status="committing", result_payload=REPLY, **dead
    )
    new_turn_request(session, conversation, key="alive", **SCENARIOS["reply being committed"])
    new_turn_request(session, conversation, key="waiting")
    new_turn_request(session, conversation, key="done", **SCENARIOS["reply committed"])

    stuck = session.scalars(
        sa.select(TurnRequest).where(
            TurnRequest.status.in_([TurnStatus.PROCESSING.value, TurnStatus.COMMITTING.value]),
            TurnRequest.lease_expires_at < NOW,
        )
    ).all()

    assert {turn.id for turn in stuck} == {generating.id, committing.id}


def test_a_reply_stored_before_a_crash_is_committed_afterwards_without_generating_again(factory):
    with factory() as s:  # accepted, with the user's message stored beside it
        conversation = new_conversation(s, new_user(s))
        asked = new_message(s, conversation, role="user", content="сколько варить яйца")
        turn = new_turn_request(s, conversation, user_message_id=asked.id)
        conversation_id, turn_id = conversation.id, turn.id
        s.commit()

    with factory() as s:  # a worker claims it ...
        turn = s.get(TurnRequest, turn_id)
        turn.status, turn.attempt_count = "processing", 1
        turn.processing_started_at, turn.lease_expires_at = NOW, NOW + timedelta(minutes=2)
        s.commit()

    with factory() as s:  # ... generates the reply, stores it, and dies before committing it
        turn = s.get(TurnRequest, turn_id)
        turn.status, turn.result_payload = "committing", REPLY
        s.commit()

    with factory() as s:  # the reconciler finds it once the lease has run out
        stuck = s.scalars(
            sa.select(TurnRequest).where(
                TurnRequest.status == TurnStatus.COMMITTING.value,
                TurnRequest.lease_expires_at < LATER,
            )
        ).all()
        assert [turn.id for turn in stuck] == [turn_id]
        turn = stuck[0]
        assert turn.assistant_message_id is None  # the commit never happened
        answer = new_message(
            s, s.get(Conversation, conversation_id), role="assistant", content=turn.result_payload["text"]
        )
        turn.assistant_message_id = answer.id
        turn.status, turn.finished_at = "completed", LATER
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        assert (turn.status, turn.attempt_count) == ("completed", 1)  # nothing was generated twice
        assert turn.result_payload == REPLY
        assert turn.user_message.content == "сколько варить яйца"
        assert turn.assistant_message.content == REPLY["text"]


def test_a_commit_that_landed_but_was_not_recorded_only_needs_the_status_moved(session, conversation):
    asked = new_message(session, conversation, role="user")
    answered = new_message(session, conversation, role="assistant", content=REPLY["text"])
    turn = new_turn_request(
        session, conversation, status="committing", result_payload=REPLY,
        user_message_id=asked.id, assistant_message_id=answered.id, **LEASED,
    )

    turn.status, turn.finished_at = "completed", NOW
    session.flush()

    assert (turn.status, turn.assistant_message_id) == ("completed", answered.id)


def test_a_persistence_failure_keeps_the_reply_and_a_retry_resumes_at_the_commit(factory):
    with factory() as s:
        turn = new_turn_request(
            s, new_conversation(s, new_user(s)), status="committing",
            result_payload=REPLY, **LEASED,
        )
        turn_id = turn.id
        s.commit()

    with factory() as s:  # the commit gave up
        turn = s.get(TurnRequest, turn_id)
        turn.status, turn.failure_kind = "failed", "persistence"
        turn.error_code, turn.finished_at = "commit_failed", NOW
        s.commit()

    with factory() as s:  # later, retried: back to committing, the reply untouched
        turn = s.get(TurnRequest, turn_id)
        assert (turn.status, turn.failure_kind, turn.result_payload) == ("failed", "persistence", REPLY)
        turn.status, turn.failure_kind, turn.finished_at = "committing", None, None
        turn.attempt_count += 1
        turn.processing_started_at, turn.lease_expires_at = LATER, LATER + timedelta(minutes=2)
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        assert (turn.status, turn.failure_kind, turn.finished_at) == ("committing", None, None)
        assert (turn.attempt_count, turn.result_payload) == (2, REPLY)


def test_a_failed_generation_is_retried_from_scratch(factory):
    with factory() as s:
        turn = new_turn_request(s, new_conversation(s, new_user(s)), **SCENARIOS["generation failed"])
        turn_id = turn.id
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        assert turn.result_payload is None  # there is nothing to resume from
        turn.status, turn.failure_kind, turn.error_code, turn.finished_at = "processing", None, None, None
        turn.attempt_count += 1
        turn.processing_started_at, turn.lease_expires_at = LATER, LATER + timedelta(minutes=2)
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        assert (turn.status, turn.failure_kind, turn.attempt_count) == ("processing", None, 2)


@pytest.mark.parametrize("before", ["accepted", "running"])
def test_a_request_can_be_cancelled_before_or_while_it_runs(factory, before):
    with factory() as s:
        turn = new_turn_request(s, new_conversation(s, new_user(s)), **SCENARIOS[before])
        turn_id = turn.id
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        turn.status, turn.finished_at = "cancelled", NOW
        s.commit()

    with factory() as s:
        turn = s.get(TurnRequest, turn_id)
        assert (turn.status, turn.finished_at) == ("cancelled", NOW)
        assert (turn.failure_kind, turn.result_payload) == (None, None)
        # a cancelled turn is not something recovery picks up, whatever its lease says
        leased = s.scalars(
            sa.select(TurnRequest).where(
                TurnRequest.status.in_([TurnStatus.PROCESSING.value, TurnStatus.COMMITTING.value])
            )
        ).all()
        assert leased == []


def test_a_retry_after_completion_finds_the_stored_reply_to_replay(session, conversation):
    original = new_turn_request(
        session, conversation, key="k", request_fingerprint=digest("body"),
        **SCENARIOS["reply committed"],
    )

    found = session.scalar(
        sa.select(TurnRequest).where(
            TurnRequest.conversation_id == conversation.id, TurnRequest.idempotency_key == "k"
        )
    )

    assert found.id == original.id
    assert found.request_fingerprint == digest("body")  # same body: a replay, not a conflict
    assert (found.status, found.result_payload) == ("completed", REPLY)
