"""What goes wrong in a Telegram invitation sign-in, and what is left behind (nothing).

Every failure after the person has begun with an invitation is a page in Russian (the ordinary
login's answers are JSON, and are pinned in tests/test_web_auth_failures.py), and every one of
them leaves the invitation valid for the right person, nothing on the allowlist, no identity
link and no session. The login that was begun is spent, whatever happens: the person starts
again from the link.

What can go wrong: the invitation stops being usable between the start and the end (withdrawn,
reissued, expired, used by another browser, the user linked another way); the identity cannot be
linked (another user's, a revoked one, one that cannot be stored); the login is not this
browser's or has been tampered with; Telegram's answer is not good (the same list of damaged
tokens and outages as the ordinary login's); the browser is signed in as somebody else; the
database fails halfway; and two browsers race.
"""

import asyncio
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from house_helper.persistence import authn
from house_helper.persistence import invitations as invitations_module
from house_helper.persistence.allowlist import allow, revoke
from house_helper.persistence.authn import IssuedSession
from house_helper.persistence.invitations import issue_invitation, revoke_invitations
from house_helper.persistence.models import AllowlistEntry, IdentityInvitation, User
from house_helper.web import invite_pages
from tests.auth_support import (
    ORIGIN,
    begin_sign_in,
    auth_app,
    cookie_attributes,
    counts,
    finish_sign_in,
    make_rsa,
    seed_user,
    sign_in,
)
from tests.invite_support import (
    MOTHER_SUB,
    OWNER_SUB,
    THIRD_SUB,
    come_back,
    enroll,
    entries_of,
    follow,
    invitation_row,
    is_unused,
    sessions_of,
    start,
    states,
    tables,
)
from tests.test_web_auth_failures import BAD_TOKENS, PROVIDER_OUTAGES, with_claims

pytestmark = pytest.mark.asyncio

LOGIN_COOKIE = "__Host-hh_login"
SESSION_COOKIE = "__Host-hh_session"


def assert_page(response, page, *, spent_login=True):
    """The answer is that page, with its status, private and without a session."""
    assert response.status_code == page.status_code, response.text
    assert response.text == page.html
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    cookies = response.headers.get_list("set-cookie")
    assert not any(c.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" not in c for c in cookies)
    if spent_login:
        assert cookie_attributes(response, LOGIN_COOKIE)["max-age"] == "0"  # this attempt's cookie is spent


def assert_nothing_linked(h, enrolled, *, sessions=0):
    assert entries_of(h, enrolled.user_id) == []
    assert tables(h)["identity_links"] == 0 and tables(h)["sessions"] == sessions
    assert is_unused(h, enrolled)  # still valid, for the right person
    assert (h.client.cookies.get(SESSION_COOKIE) is None) or sessions


def state_of(client) -> str:
    return client.cookies.get(LOGIN_COOKIE)


async def begun(h, enrolled, client=None, subject=MOTHER_SUB):
    """A browser that has started with the invitation and been approved at Telegram:
    (the client, the address Telegram sends it back to)."""
    client = client or h.client
    started = await start(client, enrolled.code)
    assert started.status_code == 200, started.text
    return client, h.telegram.authorize(started.json()["authorization_url"], subject)


# --- the invitation stops being usable between the start and the end -----------------------------------


def revoked(h, mother):
    with h.factory() as db, db.begin():
        assert revoke_invitations(db, mother.user_id, now=h.clock()) == 1


def reissued(h, mother):
    with h.factory() as db, db.begin():
        issue_invitation(db, mother.user_id, now=h.clock())


def linked_another_way(h, mother):
    with h.factory() as db, db.begin():
        allow(db, db.get(User, mother.user_id), "telegram", THIRD_SUB)


def disabled(h, mother):
    with h.factory() as db, db.begin():
        db.get(User, mother.user_id).disabled_at = h.clock()


BETWEEN = {
    "withdrawn": revoked,
    "superseded by a new one": reissued,
    "user linked another way": linked_another_way,
    "user disabled": disabled,
}


@pytest.mark.parametrize("what", BETWEEN)
async def test_an_invitation_that_stopped_being_usable_after_the_start_is_refused_at_the_end(tmp_path, what):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)
        BETWEEN[what](h, mother)

        response = await client.get(callback)

        assert_page(response, invite_pages.invalid_invitation_page())
        assert tables(h)["sessions"] == 0 and tables(h)["identity_links"] == 0
        assert client.cookies.get(SESSION_COOKIE) is None
        assert (await client.get("/auth/me")).status_code == 401
        assert not any(row[0] == THIRD_SUB for row in entries_of(h, mother.user_id)) or what == "user linked another way"
        assert h.telegram.protocol_errors == []


async def test_an_invitation_that_expired_after_the_start_is_refused_at_the_end(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h, now=h.clock() - timedelta(hours=1, minutes=58))  # two minutes left
        client, callback = await begun(h, mother)
        h.clock.advance(minutes=3)  # the login (ten minutes) is alive, the invitation is not

        response = await client.get(callback)

        assert_page(response, invite_pages.invalid_invitation_page())
        assert_nothing_linked(h, mother)


async def test_the_invitation_that_another_browser_used_first_is_refused_to_the_second(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        first, first_callback = await begun(h, mother, h.client, MOTHER_SUB)
        second, second_callback = await begun(h, mother, h.new_client(), THIRD_SUB)  # both began in time

        assert (await first.get(first_callback)).status_code == 303
        response = await second.get(second_callback)

        assert_page(response, invite_pages.invalid_invitation_page())
        assert entries_of(h, mother.user_id) == [("telegram", MOTHER_SUB, False)]  # the first one's, only
        assert tables(h)["sessions"] == 1 and second.cookies.get(SESSION_COOKIE) is None


async def test_a_new_start_after_a_refusal_still_works_while_the_invitation_is_valid(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)
        h.telegram.claims_hook = with_claims(nonce="another login's")
        assert_page(await client.get(callback), invite_pages.not_completed_page())

        h.telegram.claims_hook = None
        response = await follow(client, h.telegram, mother, MOTHER_SUB)  # the person opens the link again

        assert response.status_code == 303 and entries_of(h, mother.user_id) == [("telegram", MOTHER_SUB, False)]


# --- identities that cannot be linked -----------------------------------------------------------------


async def test_a_revoked_identity_is_not_brought_back_by_an_invitation(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        owner, mother = enroll(h, "Елена"), enroll(h, "Мама")
        await follow(h.client, h.telegram, owner, OWNER_SUB)
        old_token = h.client.cookies.get(SESSION_COOKIE)
        with h.factory() as db, db.begin():
            revoke(db, "telegram", OWNER_SUB, now=h.clock())
        mothers_browser = h.new_client()

        response = await follow(mothers_browser, h.telegram, mother, OWNER_SUB)

        assert_page(response, invite_pages.identity_page())
        assert entries_of(h, owner.user_id) == [("telegram", OWNER_SUB, True)]  # still revoked, still his
        assert entries_of(h, mother.user_id) == []
        with h.factory() as db:
            assert authn.authenticate(db, old_token, now=h.clock()) is None  # his old session is still dead
        assert tables(h)["sessions"] == 1 and is_unused(h, mother)
        assert mothers_browser.cookies.get(SESSION_COOKIE) is None


async def test_an_identity_that_is_already_linked_to_the_same_user_cannot_be_linked_again(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)
        with h.factory() as db, db.begin():  # the same account, linked by some other route in between
            allow(db, db.get(User, mother.user_id), "telegram", MOTHER_SUB)

        response = await client.get(callback)

        assert_page(response, invite_pages.invalid_invitation_page())  # the user is linked: nothing to invite
        assert entries_of(h, mother.user_id) == [("telegram", MOTHER_SUB, False)] and tables(h)["sessions"] == 0


@pytest.mark.parametrize("subject", ["has@sign", "x" * 300, "ünï", "with space", "tab\there"])
async def test_a_sub_that_cannot_be_stored_as_an_identity_is_refused_and_the_invitation_stays_valid(tmp_path, subject):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother, subject=subject)

        response = await client.get(callback)

        assert_page(response, invite_pages.identity_page())
        assert_nothing_linked(h, mother)


# --- the login is not this browser's, or has been tampered with -----------------------------------------


async def test_a_callback_opened_in_another_browser_is_refused_and_does_not_spend_the_login(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)
        attacker = h.new_client()

        refused = await attacker.get(callback)

        assert_page(refused, invite_pages.stale_page(), spent_login=True)
        assert attacker.cookies.get(SESSION_COOKIE) is None and tables(h)["sessions"] == 0
        assert h.telegram.token_calls == 0  # Telegram was never asked: the code was not exchanged
        assert [s.consumed_at for s in states(h)] == [None]  # the person's own login is not spent
        assert (await client.get(callback)).status_code == 303  # and it still works, in the right browser


async def test_the_cookie_of_one_login_does_not_open_the_callback_of_another(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mine, theirs = enroll(h, "Мама"), enroll(h, "Елена")
        my_client, my_callback = await begun(h, mine, h.client, MOTHER_SUB)
        their_client, their_callback = await begun(h, theirs, h.new_client(), OWNER_SUB)
        my_client.cookies.set(LOGIN_COOKIE, state_of(their_client), domain="kitchen.example.test", path="/")

        response = await my_client.get(my_callback)  # my state, their cookie

        assert_page(response, invite_pages.stale_page())
        assert tables(h)["sessions"] == 0
        assert is_unused(h, mine) and is_unused(h, theirs)


async def test_a_state_that_was_changed_by_a_character_is_not_this_login(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)
        parts = urlsplit(callback)
        query = {name: values[0] for name, values in parse_qs(parts.query).items()}
        changed = query["state"][:-1] + ("A" if query["state"][-1] != "A" else "B")

        response = await client.get(parts.path, params={**query, "state": changed})

        assert response.status_code == 400 and response.json() == {"detail": "invalid_login_request"}
        assert tables(h)["sessions"] == 0 and h.telegram.token_calls == 0
        assert cookie_attributes(response, LOGIN_COOKIE)["max-age"] == "0"  # a failed callback spends the cookie
        assert [s.consumed_at for s in states(h)] == [None] and is_unused(h, mother)
        # the person opens the link again; the invitation was not touched
        assert (await follow(client, h.telegram, mother, MOTHER_SUB)).status_code == 303


async def test_a_state_and_a_cookie_that_agree_but_were_never_issued_do_nothing(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        forged = "forged-state-" + "A" * 30
        h.client.cookies.set(LOGIN_COOKIE, forged, domain="kitchen.example.test", path="/")

        response = await h.client.get("/auth/telegram/callback", params={"state": forged, "code": "c"})

        assert response.status_code == 400 and response.json() == {"detail": "invalid_login_request"}
        assert tables(h)["sessions"] == 0 and is_unused(h, mother) and h.telegram.requests == []


@pytest.mark.parametrize(
    "params",
    [{}, {"code": "x"}, {"state": ""}, {"state": "has space", "code": "c"}, {"state": "x" * 200, "code": "c"}],
    ids=["nothing", "no state", "empty state", "bad characters", "too long"],
)
async def test_a_callback_without_a_usable_state_is_the_ordinary_refusal(tmp_path, params):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        await begun(h, mother)

        response = await h.client.get("/auth/telegram/callback", params=params)

        assert response.status_code == 400 and response.json() == {"detail": "invalid_login_request"}
        assert tables(h)["sessions"] == 0 and h.telegram.token_calls == 0


async def test_a_database_that_cannot_say_whether_it_was_an_invitation_gives_the_ordinary_refusal(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)
        attacker = h.new_client()
        with h.factory() as db, db.begin():  # the table that holds the logins cannot be read
            db.execute(sa.text("ALTER TABLE oidc_states RENAME TO oidc_states_gone"))

        response = await attacker.get(callback)  # not this browser's login: refused before anything is looked up...

        assert response.status_code == 400 and response.json() == {"detail": "invalid_login_request"}  # ...in words
        assert h.telegram.token_calls == 0 and tables_without_states(h)["sessions"] == 0


def tables_without_states(h):
    with h.factory() as db:
        return {
            name: db.scalar(sa.text(f"SELECT count(*) FROM {name}"))
            for name in ("allowlist_entries", "identity_links", "sessions", "identity_invitations")
        }


async def test_a_second_use_of_the_same_callback_is_refused_and_grants_nothing_more(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)
        state = state_of(client)
        assert (await client.get(callback)).status_code == 303
        client.cookies.set(LOGIN_COOKIE, state, domain="kitchen.example.test", path="/")  # as if it were kept

        replay = await client.get(callback)

        assert_page(replay, invite_pages.stale_page())
        assert tables(h)["sessions"] == 1 and len(entries_of(h, mother.user_id)) == 1
        assert invitation_row(h, mother).consumed_at is not None


async def test_a_login_older_than_its_lifetime_is_refused_with_the_invitation_still_valid(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)
        h.clock.advance(minutes=11)  # the person took a long time at Telegram

        response = await client.get(callback)

        assert_page(response, invite_pages.stale_page())
        assert_nothing_linked(h, mother)
        assert (await follow(client, h.telegram, mother, MOTHER_SUB)).status_code == 303


async def test_an_invitation_id_in_the_callback_is_ignored_and_the_one_the_state_names_is_claimed(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother, owner = enroll(h, "Мама"), enroll(h, "Елена")
        client, callback = await begun(h, mother)
        for name in ("invitation", "invitation_id", "invite", "i", "code2"):
            callback += f"&{name}={owner.invitation_id}"
        callback += f"&token={owner.token}&user_id={owner.user_id}"

        response = await client.get(callback)

        assert response.status_code == 303
        assert entries_of(h, mother.user_id) == [("telegram", MOTHER_SUB, False)]
        assert entries_of(h, owner.user_id) == [] and is_unused(h, owner)
        assert (await client.get("/auth/me")).json()["user_id"] == str(mother.user_id)


async def test_an_ordinary_login_cannot_be_turned_into_an_invitation_login_by_the_callback(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        started = await begin_sign_in(h.client)  # an ordinary login: its state names no invitation
        callback = h.telegram.authorize(started.headers["location"], MOTHER_SUB)
        callback += f"&invitation_id={mother.invitation_id}&token={mother.token}"

        response = await h.client.get(callback)

        assert response.status_code == 403 and response.json() == {"detail": "not_allowed"}  # as ever
        assert_nothing_linked(h, mother)


# --- what Telegram says -----------------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore:The RSA key is 1024 bits")
@pytest.mark.parametrize("name, damage, reason", BAD_TOKENS, ids=[case[0] for case in BAD_TOKENS])
async def test_a_bad_id_token_links_nothing_and_leaves_the_invitation_valid(tmp_path, name, damage, reason):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        damage(h.telegram)

        client, callback = await begun(h, mother)
        response = await client.get(callback)

        assert_page(response, invite_pages.not_completed_page())
        assert_nothing_linked(h, mother)
        assert h.telegram.token_calls == 1 and h.telegram.jwks_calls == 1
        assert [s.consumed_at is not None for s in states(h)] == [True]  # the login is spent


@pytest.mark.parametrize("name, hook, behaviour", PROVIDER_OUTAGES, ids=[case[0] for case in PROVIDER_OUTAGES])
async def test_a_provider_that_fails_links_nothing_and_says_so_in_russian(tmp_path, name, hook, behaviour):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        setattr(h.telegram, hook, behaviour)

        client, callback = await begun(h, mother)
        response = await client.get(callback)

        assert_page(response, invite_pages.provider_unavailable_page())
        assert_nothing_linked(h, mother)
        assert "evil.test" not in [url for _, url in h.telegram.requests]  # no redirect was followed


@pytest.mark.parametrize("code", [400, 401, 403])
async def test_a_provider_that_refuses_the_code_links_nothing(tmp_path, code):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        h.telegram.token_failure = lambda request: httpx.Response(code, json={"error": "invalid_client"})

        client, callback = await begun(h, mother)
        response = await client.get(callback)

        assert_page(response, invite_pages.not_completed_page())
        assert "invalid_client" not in response.text
        assert_nothing_linked(h, mother)


async def test_a_person_who_cancels_at_telegram_comes_back_with_an_error_and_nothing_is_linked(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        started = await start(h.client, mother.code)
        state = state_of(h.client)

        response = await h.client.get(
            "/auth/telegram/callback", params={"error": "access_denied", "state": state}
        )

        assert_page(response, invite_pages.not_completed_page())
        assert_nothing_linked(h, mother) and started.status_code == 200
        assert h.telegram.requests == []  # nothing was asked of Telegram


async def test_a_callback_with_no_code_is_the_stale_page_and_nothing_is_asked_of_telegram(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        await start(h.client, mother.code)

        response = await h.client.get("/auth/telegram/callback", params={"state": state_of(h.client)})

        assert_page(response, invite_pages.stale_page())
        assert h.telegram.requests == [] and is_unused(h, mother)


async def test_a_token_for_another_audience_does_not_link_anybody(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        h.telegram.claims_hook = with_claims(aud="999")

        client, callback = await begun(h, mother)
        response = await client.get(callback)

        assert_page(response, invite_pages.not_completed_page())
        assert_nothing_linked(h, mother)


# --- a browser that is signed in as somebody else ---------------------------------------------------------


async def test_a_browser_signed_in_as_another_user_cannot_start_and_stays_signed_in(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        owner, mother = enroll(h, "Елена"), enroll(h, "Мама")
        await follow(h.client, h.telegram, owner, OWNER_SUB)  # this browser is the owner's now
        token = h.client.cookies.get(SESSION_COOKIE)

        response = await start(h.client, mother.code)  # her link, opened in his browser

        assert response.status_code == 409 and response.json()["detail"] == "signed_in_as_another_user"
        assert "учетную запись" in response.json()["message"]
        assert "set-cookie" not in response.headers
        assert len(states(h)) == 1  # only the owner's own, from before
        assert is_unused(h, mother)
        assert h.client.cookies.get(SESSION_COOKIE) == token
        assert (await h.client.get("/auth/me")).json()["user_id"] == str(owner.user_id)


async def test_a_browser_that_became_somebody_else_in_between_is_refused_at_the_end(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        owner, mother = enroll(h, "Елена"), enroll(h, "Мама")
        client, callback = await begun(h, mother)  # begun by nobody...
        await follow(client, h.telegram, owner, OWNER_SUB)  # ...then another tab signs in as the owner
        token = client.cookies.get(SESSION_COOKIE)
        client.cookies.set(LOGIN_COOKIE, parse_qs(urlsplit(callback).query)["state"][0], domain="kitchen.example.test", path="/")

        response = await client.get(callback)

        assert_page(response, invite_pages.other_user_page())
        assert entries_of(h, mother.user_id) == [] and is_unused(h, mother)
        assert client.cookies.get(SESSION_COOKIE) == token  # his session is not replaced by hers
        assert (await client.get("/auth/me")).json()["user_id"] == str(owner.user_id)
        assert sessions_of(h, mother.user_id) == []


async def test_a_dead_session_of_somebody_else_does_not_stand_in_the_way(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        owner, mother = enroll(h, "Елена"), enroll(h, "Мама")
        await follow(h.client, h.telegram, owner, OWNER_SUB)
        h.clock.advance(days=15)  # the owner's cookie is still in the jar; the session is over
        assert (await h.client.get("/auth/me")).status_code == 401
        mother = enroll(h, "Мама 2")

        response = await follow(h.client, h.telegram, mother, MOTHER_SUB)

        assert response.status_code == 303
        assert (await h.client.get("/auth/me")).json()["user_id"] == str(mother.user_id)


async def test_the_ordinary_login_refuses_a_browser_signed_in_as_somebody_else_as_the_invitation_does(tmp_path):
    # Until the A3 remediation the callback of an ORDINARY Telegram login replaced the session cookie of
    # whoever the browser was signed in as. It now refuses it like the invitation sign-in (above) and
    # the email link do; the whole matrix is in tests/test_web_auth_cross_user.py.
    async with auth_app(tmp_path) as h:
        seed_user(h.factory, subject=OWNER_SUB, display_name="Борис")
        await sign_in(h.client, h.telegram)  # the seeded user's session
        before = h.client.cookies.get(SESSION_COOKIE)

        response = await sign_in(h.client, h.telegram, OWNER_SUB)

        assert_page(response, invite_pages.login_other_user_page())
        assert h.client.cookies.get(SESSION_COOKIE) == before
        assert (await h.client.get("/auth/me")).json()["user_id"] == str(h.user_id)


# --- the database fails halfway ----------------------------------------------------------------------------


def trigger(h, table, name="refuse"):
    with h.factory() as db, db.begin():
        db.execute(
            sa.text(
                f"CREATE TRIGGER {name} BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'disk full'); END"
            )
        )


def drop_trigger(h, name="refuse"):
    with h.factory() as db, db.begin():
        db.execute(sa.text(f"DROP TRIGGER {name}"))


@pytest.mark.parametrize(
    ("table", "page"),
    [
        # the claim, the entry, the link and the session come in this order; whichever refuses,
        # the answer is the one the code gives that refusal (a constraint at the entry is an
        # identity that cannot be linked, at the link a login that is denied, at the session a
        # database that cannot answer) and what the earlier steps wrote is gone
        ("allowlist_entries", invite_pages.identity_page),
        ("identity_links", invite_pages.not_completed_page),
        ("sessions", invite_pages.unavailable_page),
    ],
)
async def test_a_database_failure_at_any_step_of_the_end_undoes_every_step_before_it(tmp_path, table, page):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)
        trigger(h, table)

        response = await client.get(callback)

        assert_page(response, page())
        assert_nothing_linked(h, mother)
        assert invitation_row(h, mother).consumed_at is None  # the claim was undone with the rest
        drop_trigger(h)
        # and the person simply opens the link again
        assert (await follow(client, h.telegram, mother, MOTHER_SUB)).status_code == 303
        assert entries_of(h, mother.user_id) == [("telegram", MOTHER_SUB, False)]


async def test_a_database_failure_when_the_login_is_started_stores_nothing_and_says_so(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        trigger(h, "oidc_states")

        response = await start(h.client, mother.code)

        assert response.status_code == 503 and response.json()["detail"] == "temporarily_unavailable"
        assert "set-cookie" not in response.headers and is_unused(h, mother)
        assert tables(h)["oidc_states"] == 0


async def test_an_unexpected_error_while_the_session_is_issued_undoes_the_claim(tmp_path, monkeypatch, caplog):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)
        real = authn.complete_login

        def boom(session, provider, subject, **options):
            real(session, provider, subject, **options)  # written, and then it fails
            raise RuntimeError("secret-marker-in-the-message")

        monkeypatch.setattr(invitations_module.authn, "complete_login", boom)

        response = await client.get(callback)

        assert_page(response, invite_pages.error_page())
        assert_nothing_linked(h, mother)
        assert "secret-marker-in-the-message" not in response.text + caplog.text  # the class only
        assert "RuntimeError" in caplog.text


async def test_a_session_that_is_somebody_elses_is_refused_and_nothing_is_kept(tmp_path, monkeypatch):
    async with auth_app(tmp_path, seed=False) as h:
        owner, mother = enroll(h, "Елена"), enroll(h, "Мама")
        client, callback = await begun(h, mother)

        def somebody_elses(session, provider, subject, **options):
            return IssuedSession(token="t" * 43, user_id=owner.user_id, session_id=uuid4(), expires_at=h.clock())

        monkeypatch.setattr(invitations_module.authn, "complete_login", somebody_elses)

        response = await client.get(callback)

        assert_page(response, invite_pages.not_completed_page())
        assert_nothing_linked(h, mother)
        assert client.cookies.get(SESSION_COOKIE) is None


# --- two browsers at once ----------------------------------------------------------------------------------


async def test_two_browsers_finishing_one_invitation_at_once_link_exactly_one_identity(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        first, first_callback = await begun(h, mother, h.client, MOTHER_SUB)
        second, second_callback = await begun(h, mother, h.new_client(), THIRD_SUB)

        responses = await asyncio.gather(first.get(first_callback), second.get(second_callback))

        assert sorted(r.status_code for r in responses) == [303, 400]
        loser = next(r for r in responses if r.status_code == 400)
        assert_page(loser, invite_pages.invalid_invitation_page())
        assert len(entries_of(h, mother.user_id)) == 1
        assert tables(h)["identity_links"] == 1 and tables(h)["sessions"] == 1
        assert invitation_row(h, mother).consumed_at is not None


async def test_one_telegram_account_finishing_two_invitations_at_once_belongs_to_one_user(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        owner, mother = enroll(h, "Елена"), enroll(h, "Мама")
        owners, owner_callback = await begun(h, owner, h.client, OWNER_SUB)
        mothers, mother_callback = await begun(h, mother, h.new_client(), OWNER_SUB)  # the same account

        responses = await asyncio.gather(owners.get(owner_callback), mothers.get(mother_callback))

        assert sorted(r.status_code for r in responses) == [303, 409]
        winner = owner if responses[0].status_code == 303 else mother
        loser = mother if winner is owner else owner
        assert entries_of(h, winner.user_id) == [("telegram", OWNER_SUB, False)]
        assert entries_of(h, loser.user_id) == [] and is_unused(h, loser)  # still valid for the right account
        assert tables(h)["allowlist_entries"] == 1 and tables(h)["sessions"] == 1


async def test_one_callback_sent_twice_at_once_makes_one_session(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)

        responses = await asyncio.gather(client.get(callback), client.get(callback))

        assert sorted(r.status_code for r in responses) == [303, 400]
        assert tables(h)["sessions"] == 1 and tables(h)["allowlist_entries"] == 1
        assert h.telegram.token_calls == 1  # the code was exchanged once


async def test_a_revocation_and_a_callback_at_once_never_leave_a_live_session_behind(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        client, callback = await begun(h, mother)

        def revoke_it():
            with h.factory() as db, db.begin():
                revoke_invitations(db, mother.user_id, now=h.clock())

        responses = await asyncio.gather(client.get(callback), asyncio.to_thread(revoke_it))

        callback_response = responses[0]
        if callback_response.status_code == 303:  # the claim came first: the invitation is used, not revoked
            assert tables(h)["sessions"] == 1
        else:  # the revocation came first: nothing is linked
            assert_page(callback_response, invite_pages.invalid_invitation_page())
            assert tables(h)["sessions"] == 0 and entries_of(h, mother.user_id) == []
