"""What an invitation's secret never touches, and what happens when Telegram login is off.

The invitation code is a capability: whoever holds it, within its two hours, may link THEIR OWN
Telegram account to the user it names. So it exists in exactly three places: the link the
operator hands over, the URL fragment of that link in the person's browser (which the browser
never sends and the page's script removes at once), and the body of one POST to this origin. It is
in no URL, query string, redirect, cookie, request to Telegram, response, error, log line or
table (the database holds the digest of the secret, and the invitation's id, which is not a
secret but is not shown either). These tests run whole sign-ins, honest and not, and search
everything that was said for the secret, the code, the id and the person's identity.

When Telegram login is not configured, the page and the start answer 503 and nothing is
stored (the ordinary routes answer as they always did).
"""

import json

import pytest
import sqlalchemy as sa

from house_helper.persistence.invitations import issue_invitation
from house_helper.persistence.models import User
from house_helper.web import invite_pages
from house_helper.web.app import create_app
from tests.auth_support import ORIGIN, app_log_text, auth_app, database_text, watch_logs
from tests.email_support import email_app
from tests.invite_support import MOTHER_SUB, OWNER_SUB, THIRD_SUB, enroll, follow, shape, start
from tests.test_web_auth_failures import with_claims
from tests.web_support import serving, settings_for

pytestmark = pytest.mark.asyncio


def table_text(h, table: str) -> str:
    with h.factory() as db:
        return "\n".join(repr(tuple(row)) for row in db.execute(sa.text(f"SELECT * FROM {table}")).all())


def evidence(h, responses, caplog):
    """Everything that was said in the course of the sign-ins, as one string per place."""
    telegram = "\n".join(f"{method} {url}" for method, url in h.telegram.requests)
    telegram += "\n" + json.dumps(h.telegram.token_requests)
    answers = []
    for response in responses:
        answers.append(str(response.request.url))
        answers.append(response.text)
        answers.append(json.dumps(dict(response.headers.items())))
        answers.extend(response.headers.get_list("set-cookie"))
        answers.append(response.headers.get("location", ""))
    cookies = " ".join(f"{cookie.name}={cookie.value}" for cookie in h.client.cookies.jar)
    return {
        "what Telegram saw": telegram,
        "what the browser was told": "\n".join(answers),
        "the cookies": cookies,
        "the log": app_log_text(caplog),
        "the oidc states": table_text(h, "oidc_states"),
        "the sessions": table_text(h, "sessions"),
        "the identities": table_text(h, "identity_links") + table_text(h, "allowlist_entries"),
    }


async def an_afternoon_of_invitations(h, caplog):
    """An honest link, a refused identity, a bad token, a used invitation and a wrong code, each by
    a browser of its own: (the responses, the invitations that were involved)."""
    mother, owner, brother = enroll(h, "Мама"), enroll(h, "Елена"), enroll(h, "Брат")
    seen = []

    async def record(coro):
        response = await coro
        seen.append(response)
        return response

    owners = h.new_client()
    started = await record(start(h.client, mother.code))
    seen.append(await h.client.get(h.telegram.authorize(started.json()["authorization_url"], MOTHER_SUB)))
    seen.append(await h.client.get("/auth/invite/done"))
    seen.append(await h.client.get("/auth/me"))
    seen.append(await record(start(owners, owner.code)))  # a good start...
    seen.append(await owners.get(h.telegram.authorize(seen[-1].json()["authorization_url"], MOTHER_SUB)))  # ...a taken identity
    other = h.new_client()
    seen.append(await record(start(other, mother.code)))  # a used invitation
    seen.append(await record(start(other, f"{brother.invitation_id}.{'Z' * 43}")))  # a wrong secret
    seen.append(await record(start(other, brother.code + ".surplus")))  # malformed, with the real secret inside
    seen.append(await record(start(other, " " + brother.code)))  # padded: malformed too
    h.telegram.claims_hook = with_claims(nonce="not this login's")
    third = h.new_client()
    started = await record(start(third, brother.code))
    seen.append(await third.get(h.telegram.authorize(started.json()["authorization_url"], THIRD_SUB)))  # a bad token
    h.telegram.claims_hook = None
    seen.append(await h.client.get("/auth/invite"))
    return seen, (mother, owner, brother)


async def test_no_secret_code_or_id_is_in_any_url_cookie_answer_log_or_request_to_telegram(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path, seed=False) as h:
        seen, invitations = await an_afternoon_of_invitations(h, caplog)

        places = evidence(h, seen, caplog)
        for enrolled in invitations:
            for place, text in places.items():
                assert enrolled.token not in text, (enrolled.display_name, place)
                assert enrolled.code not in text, (enrolled.display_name, place)
                if place == "the oidc states":
                    # (This table is where the login keeps the invitation's id, as a column, so the id is
                    # legitimately there; it is not a secret. SQLite writes a UUID as 32 hexadecimal digits,
                    # which is how the rows below print, so a search for the hyphenated spelling in table
                    # text would prove nothing: it is not made.)
                    continue
                # everywhere else the id is in neither spelling: the hyphenated one that a URL, an answer or
                # a log would carry, and the hexadecimal one that a table row would
                assert str(enrolled.invitation_id) not in text, (enrolled.display_name, place)
                assert enrolled.invitation_id.hex not in text, (enrolled.display_name, place)
        # the control: the hexadecimal spelling is how the one table that holds the id shows it
        mother = invitations[0]
        assert mother.invitation_id.hex in places["the oidc states"]
        assert str(mother.invitation_id) not in places["the oidc states"]


async def test_the_database_holds_the_digest_of_the_secret_and_never_the_secret(tmp_path):
    from house_helper.persistence.authn import hash_secret

    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        await follow(h.client, h.telegram, mother, MOTHER_SUB)

        everything = database_text(h) + table_text(h, "identity_invitations")

        assert mother.token not in everything and mother.code not in everything
        assert hash_secret(mother.token) in table_text(h, "identity_invitations")  # the digest is what it keeps


async def test_the_start_request_carries_the_code_in_its_body_and_nowhere_in_its_address(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        response = await start(h.client, mother.code)

        request = response.request
        assert request.url.query == b"" and request.url.fragment == "" and request.url.path == "/auth/invite/start"
        assert mother.code not in str(request.url) and mother.code not in json.dumps(dict(request.headers))
        assert json.loads(request.content) == {"code": mother.code}  # the body, over TLS, to this origin only
        assert "cookie" not in request.headers or mother.token not in request.headers["cookie"]


async def test_the_authorization_address_names_nothing_of_the_invitation(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        started = await start(h.client, mother.code)

        url = started.json()["authorization_url"]
        for secret in (mother.token, mother.code, str(mother.invitation_id), str(mother.user_id)):
            assert secret not in url
        assert f"state={h.client.cookies.get('__Host-hh_login')}" in url  # the state is the cookie's, nothing more


async def test_the_state_cookie_and_the_state_are_unrelated_to_the_invitation(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        first = await start(h.client, mother.code)
        second = await start(h.new_client(), mother.code)

        states = {
            part.split("=")[1]
            for response in (first, second)
            for part in response.json()["authorization_url"].split("&")
            if part.startswith("state=")
        }
        assert len(states) == 2 and all(len(state) == 43 for state in states)  # random, one per start
        assert all(mother.token[:10] not in state for state in states)


async def test_what_the_person_is_told_never_names_a_user_an_identity_or_a_reason(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        owner, mother = enroll(h, "Елена-владелец"), enroll(h, "Мама-гостья")
        seen = [
            await follow(h.client, h.telegram, owner, OWNER_SUB),
            await follow(h.new_client(), h.telegram, mother, OWNER_SUB),  # his account: refused
            await start(h.new_client(), owner.code),  # used
            await start(h.client, mother.code),  # his browser, her link
            await start(h.new_client(), "nonsense"),
            await start(h.new_client(), mother.code, origin="https://evil.example"),
        ]

        for response in seen:
            text = response.text + json.dumps(dict(response.headers.items()))
            for private in (OWNER_SUB, MOTHER_SUB, str(owner.user_id), str(mother.user_id),
                            "Елена-владелец", "Мама-гостья", str(owner.invitation_id), str(mother.invitation_id)):
                assert private not in text, (response.status_code, private)
            assert "Traceback" not in text and "sqlalchemy" not in text.lower() and "sqlite" not in text.lower()


async def test_the_log_says_what_happened_and_names_no_secret_identity_or_invitation(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        await follow(h.client, h.telegram, mother, MOTHER_SUB)
        await start(h.new_client(), mother.code)  # now used

        logged = app_log_text(caplog)

        assert "A Telegram invitation sign-in was started" in logged
        assert "linked Telegram by invitation and signed in" in logged
        assert "A Telegram invitation was not valid" in logged
        for private in (mother.token, mother.code, str(mother.invitation_id), MOTHER_SUB):
            assert private not in logged


async def test_a_failure_log_names_the_class_of_a_database_error_and_not_its_parameters(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        with h.factory() as db, db.begin():
            db.execute(sa.text(
                "CREATE TRIGGER refuse BEFORE INSERT ON oidc_states BEGIN SELECT RAISE(ABORT, 'disk full'); END"
            ))

        response = await start(h.client, mother.code)

        assert response.status_code == 503
        logged = app_log_text(caplog)
        assert "IntegrityError" in logged  # the class, as for every database error here
        for private in (mother.token, mother.code, str(mother.invitation_id)):
            assert private not in logged + response.text
        assert caplog.records and all(
            record.name != "sqlalchemy.engine" for record in caplog.records
        )  # the engine's own logger, which prints bound parameters, is not on


async def test_the_secret_is_not_even_in_an_exception_message_of_a_failed_claim(tmp_path, caplog, monkeypatch):
    from house_helper.persistence import invitations as invitations_module

    watch_logs(caplog)
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        started = await start(h.client, mother.code)

        def boom(*args, **kwargs):
            raise RuntimeError("the claim blew up")

        monkeypatch.setattr(invitations_module, "claim_invitation", boom)
        response = await h.client.get(h.telegram.authorize(started.json()["authorization_url"], MOTHER_SUB))

        assert response.status_code == 500
        for private in (mother.token, mother.code, str(mother.invitation_id), MOTHER_SUB):
            assert private not in app_log_text(caplog) + response.text


# --- Telegram login is not configured ---------------------------------------------------------------------


async def test_without_any_login_configured_the_invitation_routes_answer_503_and_touch_nothing(tmp_path):
    app = create_app(settings_for(tmp_path / "off.db"))

    async with serving(app) as client:
        page = await client.get("/auth/invite")
        started = await client.post("/auth/invite/start", json={"code": "x"}, headers={"origin": "http://web.test"})
        done = await client.get("/auth/invite/done")
        ordinary = await client.get("/auth/telegram/login")

    assert page.status_code == 503 and page.text == invite_pages.not_configured_page().html
    assert started.status_code == 503 and started.json()["detail"] == "login_unavailable"
    assert "Telegram" in started.json()["message"] and "set-cookie" not in started.headers
    assert done.status_code == 303 and done.headers["location"] == "/"
    assert ordinary.status_code == 503 and ordinary.json() == {"detail": "login_unavailable"}  # as ever
    assert list(tmp_path.iterdir()) == []  # not even the database was touched


async def test_with_only_email_login_the_invitation_routes_are_off_and_the_start_stores_nothing(tmp_path):
    async with email_app(tmp_path, seed=False) as h:
        with h.factory() as db, db.begin():
            user = User(display_name="Мама")
            db.add(user)
            db.flush()
            issued = issue_invitation(db, user.id, now=h.clock())

        page = await h.client.get("/auth/invite")
        started = await h.client.post(
            "/auth/invite/start", json={"code": f"{issued.invitation_id}.{issued.token}"}, headers={"origin": ORIGIN}
        )

        assert page.status_code == 503 and page.text == invite_pages.not_configured_page().html
        assert started.status_code == 503 and started.json()["detail"] == "login_unavailable"
        with h.factory() as db:
            assert db.scalar(sa.text("SELECT count(*) FROM oidc_states")) == 0
            assert db.scalar(sa.text("SELECT count(*) FROM identity_invitations WHERE consumed_at IS NOT NULL")) == 0


async def test_the_not_configured_answers_do_not_depend_on_the_code_sent(tmp_path):
    app = create_app(settings_for(tmp_path / "off.db"))

    async with serving(app) as client:
        answers = [
            await client.post("/auth/invite/start", json={"code": code}, headers={"origin": "http://web.test"})
            for code in ("x", "", "0" * 36 + "." + "A" * 43)
        ]

    assert len({shape(answer) for answer in answers}) == 1
