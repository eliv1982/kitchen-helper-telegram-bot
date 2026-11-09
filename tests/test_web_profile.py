"""The profile API: GET and PATCH /api/profile.

The requests go through httpx's ASGI transport into the real application on a real
migrated SQLite file; nothing leaves the process and no provider is called. The two
users of the ownership tests are two provisioned people, each signed in from their own
browser (their own cookie jar).
"""

import json

import pytest

from house_helper.persistence.allowlist import revoke
from house_helper.persistence.models import User
from tests.auth_support import ORIGIN, SUBJECT, logout_as, sign_in
from tests.chat_support import chat_app, error_of
from tests.email_support import ADDRESS, email_app
from tests.persistence_support import new_identity_link

PATCH_HEADERS = {"Origin": ORIGIN}


async def profile(client) -> dict:
    response = await client.get("/api/profile")
    assert response.status_code == 200, response.text
    return response.json()


async def change(client, **fields):
    return await client.patch(
        "/api/profile",
        json=fields,
        headers={"Content-Type": "application/json", **PATCH_HEADERS},
    )


# --- who may ask ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_profile_needs_a_session_and_changes_nothing_without_one(tmp_path):
    async with chat_app(tmp_path, sign_in_first=False) as chat:
        listing = await chat.client.get("/api/profile")
        changed = await change(chat.client, display_name="Кто-то")

    assert listing.status_code == 401
    assert error_of(listing)["detail"] == "not_authenticated"
    assert changed.status_code == 401
    assert error_of(changed)["detail"] == "not_authenticated"


@pytest.mark.asyncio
async def test_a_change_from_another_site_is_refused_and_a_read_is_not(tmp_path):
    async with chat_app(tmp_path) as chat:
        before = await profile(chat.client)
        foreign = await chat.client.patch(
            "/api/profile",
            json={"display_name": "Чужой"},
            headers={"Content-Type": "application/json", "Origin": "https://evil.example.test"},
        )
        null_origin = await chat.client.patch(
            "/api/profile",
            json={"display_name": "Чужой"},
            headers={"Content-Type": "application/json", "Origin": "null"},
        )
        after = await profile(chat.client)

    assert foreign.status_code == 403
    assert error_of(foreign)["detail"] == "forbidden_origin"
    assert null_origin.status_code == 403
    assert after == before


# --- reading -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_profile_is_the_users_own_name_size_and_sign_in_methods(tmp_path):
    async with chat_app(tmp_path) as chat:
        body = await profile(chat.client)

    assert body == {
        "display_name": "Елена",
        "text_size": "normal",
        "login_methods": [{"provider": "telegram", "address": None, "status": "active"}],
    }
    # The Telegram subject is not part of the answer: it is an internal identifier, and
    # the only thing the API names about that identity is its provider.
    assert SUBJECT not in json.dumps(body)
    assert "subject" not in json.dumps(body).lower()


@pytest.mark.asyncio
async def test_the_profile_asks_no_provider_and_no_assistant(tmp_path):
    from tests.chat_support import ScriptedBackend

    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        await profile(chat.client)
        saved = await change(chat.client, display_name="Иван")
        await profile(chat.client)

    assert saved.status_code == 200
    assert backend.calls == 0


# --- writing -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_change_is_server_side_and_survives_another_session_and_another_browser(tmp_path):
    async with chat_app(tmp_path) as chat:
        saved = await change(chat.client, display_name="  Иван  ", text_size="large")
        again = await profile(chat.client)

        # Another browser signs in as the same person: the profile is the account's, not
        # the page's, and not the session's.
        other = chat.base.new_client()
        assert (await sign_in(other, chat.base.telegram)).status_code == 303
        elsewhere = await profile(other)

    assert saved.status_code == 200
    assert saved.json()["display_name"] == "Иван"  # trimmed, as it was stored
    assert saved.json()["text_size"] == "large"
    assert again == saved.json() == elsewhere
    assert "Иван" not in SUBJECT


@pytest.mark.asyncio
async def test_one_field_can_change_without_the_other(tmp_path):
    async with chat_app(tmp_path) as chat:
        first = await change(chat.client, text_size="large")
        second = await change(chat.client, display_name="Иван")
        third = await change(chat.client, text_size="normal")

    assert (first.json()["display_name"], first.json()["text_size"]) == ("Елена", "large")
    assert (second.json()["display_name"], second.json()["text_size"]) == ("Иван", "large")
    assert (third.json()["display_name"], third.json()["text_size"]) == ("Иван", "normal")


@pytest.mark.asyncio
async def test_the_limit_is_two_hundred_code_points_and_duplicates_are_allowed(tmp_path):
    async with chat_app(tmp_path) as chat:
        longest = await change(chat.client, display_name="😀" * 200)
        too_long = await change(chat.client, display_name="😀" * 201)
        duplicate = await change(chat.client, display_name="Другой человек")
        other = await chat.second_user()
        theirs = await change(other, display_name="Другой человек")

    assert longest.status_code == 200
    assert longest.json()["display_name"] == "😀" * 200
    assert too_long.status_code == 400
    assert error_of(too_long)["detail"] == "invalid_display_name"
    assert duplicate.status_code == 200 and theirs.status_code == 200  # names are not keys


@pytest.mark.asyncio
async def test_the_change_is_persisted_in_the_database_not_only_in_the_answer(tmp_path):
    async with chat_app(tmp_path) as chat:
        assert (await change(chat.client, display_name="Иван", text_size="large")).status_code == 200
        with chat.base.factory() as db:
            user = db.get(User, chat.user_id)
            assert (user.display_name, user.text_size) == ("Иван", "large")


# --- two people ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_people_have_two_profiles_and_nothing_crosses_between_them(tmp_path):
    async with chat_app(tmp_path) as chat:
        other = await chat.second_user()
        assert (await change(chat.client, display_name="Иван", text_size="large")).status_code == 200
        assert (await change(other, display_name="Пётр", text_size="normal")).status_code == 200

        mine = await profile(chat.client)
        theirs = await profile(other)

    assert (mine["display_name"], mine["text_size"]) == ("Иван", "large")
    assert (theirs["display_name"], theirs["text_size"]) == ("Пётр", "normal")
    assert mine["login_methods"] == [{"provider": "telegram", "address": None, "status": "active"}]
    assert theirs["login_methods"] == [{"provider": "telegram", "address": None, "status": "active"}]
    assert SUBJECT not in json.dumps([mine, theirs])


# --- revoked identities --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_revoked_identity_is_reported_as_revoked_while_another_way_in_stays(tmp_path):
    async with email_app(tmp_path) as h:
        signed_in = await h.sign_in(h.client)
        assert signed_in.status_code == 303
        with h.factory() as db, db.begin():
            user = db.get(User, h.user_id)
            new_identity_link(db, user, "telegram", SUBJECT)
        with h.factory() as db, db.begin():
            revoke(db, "telegram", SUBJECT, now=h.clock())
        body = await profile(h.client)

    assert body["login_methods"] == [
        {"provider": "email", "address": ADDRESS, "status": "active"},
        {"provider": "telegram", "address": None, "status": "revoked"},
    ]
    assert ADDRESS in json.dumps(body)
    assert SUBJECT not in json.dumps(body)


# --- requests that cannot be stored --------------------------------------------------


INVALID_BODIES = [
    ({}, "invalid_request"),
    ({"user_id": "00000000-0000-4000-8000-000000000000"}, "invalid_request"),
    ({"owner": "me"}, "invalid_request"),
    ({"display_name": 5}, "invalid_request"),
    ({"display_name": None}, "invalid_request"),
    ({"display_name": ["Иван"]}, "invalid_request"),
    ({"display_name": {"name": "Иван"}}, "invalid_request"),
    ({"display_name": ""}, "invalid_display_name"),
    ({"display_name": "   "}, "invalid_display_name"),
    ({"display_name": "а" * 201}, "invalid_display_name"),
    ({"display_name": "а\nб"}, "invalid_display_name"),
    ({"display_name": "а​б"}, "invalid_display_name"),
    ({"display_name": "а‮б"}, "invalid_display_name"),
    ({"display_name": "\ud800"}, "invalid_display_name"),
    ({"text_size": "huge"}, "invalid_text_size"),
    ({"text_size": "Normal"}, "invalid_text_size"),
    ({"text_size": 1}, "invalid_text_size"),
    ({"text_size": None}, "invalid_text_size"),
    ({"text_size": True}, "invalid_text_size"),
    ({"text_size": ["normal"]}, "invalid_text_size"),
    ({"text_size": {"size": "normal"}}, "invalid_text_size"),
    ({"display_name": "Иван", "text_size": "normal", "extra": 1}, "invalid_request"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "code"), INVALID_BODIES)
async def test_an_unusable_change_is_refused_and_nothing_is_written(tmp_path, body, code):
    async with chat_app(tmp_path) as chat:
        before = await profile(chat.client)
        response = await chat.client.patch(
            "/api/profile",
            content=json.dumps(body),  # ASCII escapes: a lone surrogate is sendable and refused
            headers={"Content-Type": "application/json", **PATCH_HEADERS},
        )
        after = await profile(chat.client)

    assert response.status_code == 400, response.text
    assert error_of(response)["detail"] == code
    assert after == before


@pytest.mark.asyncio
async def test_a_body_that_is_not_a_json_object_is_invalid_request(tmp_path):
    answers = [
        ("[]", 400, "invalid_request"),
        ('"Иван"', 400, "invalid_request"),
        ("null", 400, "invalid_request"),
        ('{"display_name": "a", "display_name": "b"}', 400, "invalid_request"),  # a repeated key
        ('{"display_name": }', 400, "invalid_request"),
    ]
    async with chat_app(tmp_path) as chat:
        responses = [
            await chat.client.patch(
                "/api/profile",
                content=content,
                headers={"Content-Type": "application/json", **PATCH_HEADERS},
            )
            for content, _, _ in answers
        ]

    for response, (_, status, code) in zip(responses, answers):
        assert response.status_code == status
        assert error_of(response)["detail"] == code


@pytest.mark.asyncio
async def test_a_change_must_be_json_and_is_bounded(tmp_path):
    async with chat_app(tmp_path) as chat:
        form = await chat.client.patch(
            "/api/profile",
            content="display_name=Иван",
            headers={"Content-Type": "application/x-www-form-urlencoded", **PATCH_HEADERS},
        )
        oversized = await chat.client.patch(
            "/api/profile",
            content=json.dumps({"display_name": "a" * 100_000}),
            headers={"Content-Type": "application/json", **PATCH_HEADERS},
        )

    assert form.status_code == 415 and error_of(form)["detail"] == "unsupported_media_type"
    assert oversized.status_code == 413 and error_of(oversized)["detail"] == "request_too_large"


@pytest.mark.asyncio
async def test_the_refusal_is_in_russian_and_says_what_the_name_needs(tmp_path):
    async with chat_app(tmp_path) as chat:
        response = await change(chat.client, display_name="")

    body = error_of(response)
    assert body["detail"] == "invalid_display_name"
    assert "Имя" in body["message"] and "200" in body["message"]
    assert body["retryable"] is False


# --- the session ending --------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_the_session_ends_the_profile_is_not_served(tmp_path):
    async with chat_app(tmp_path) as chat:
        assert (await profile(chat.client))["display_name"] == "Елена"
        out = await chat.client.post("/auth/logout", headers=logout_as(chat.user_id, Origin=ORIGIN))
        after = await chat.client.get("/api/profile")
        changed = await change(chat.client, display_name="Иван")

    assert out.status_code == 204
    assert after.status_code == 401 and changed.status_code == 401
