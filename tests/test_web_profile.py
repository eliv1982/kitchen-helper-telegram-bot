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

DEFAULT_MEASURES = {
    "glass_ml": {"override": None, "effective": 250, "default": 250, "min": 50, "max": 1000},
    "teaspoon_ml": {"override": None, "effective": 5, "default": 5, "min": 1, "max": 20},
    "tablespoon_ml": {"override": None, "effective": 15, "default": 15, "min": 5, "max": 50},
}


def measure(name, default, minimum, maximum, override=None):
    return {
        "override": override,
        "effective": default if override is None else override,
        "default": default,
        "min": minimum,
        "max": maximum,
    }


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
        "measures": DEFAULT_MEASURES,
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


# --- household measures (KH-F01-B1) --------------------------------------------------


@pytest.mark.asyncio
async def test_the_measures_start_at_the_system_defaults_and_no_account_is_seeded(tmp_path):
    async with chat_app(tmp_path) as chat:
        mine = await profile(chat.client)
        other = await chat.second_user()
        theirs = await profile(other)
        with chat.base.factory() as db:
            user = db.get(User, chat.user_id)
            row = (user.measure_glass_ml, user.measure_teaspoon_ml, user.measure_tablespoon_ml)

    assert mine["measures"] == DEFAULT_MEASURES == theirs["measures"]
    assert row == (None, None, None)


@pytest.mark.asyncio
async def test_each_measure_changes_on_its_own_and_null_restores_the_default(tmp_path):
    async with chat_app(tmp_path) as chat:
        glass = await change(chat.client, measures={"glass_ml": 240})
        teaspoon = await change(chat.client, measures={"teaspoon_ml": 4})
        tablespoon = await change(chat.client, measures={"tablespoon_ml": 20})
        restored = await change(chat.client, measures={"tablespoon_ml": None})

    assert glass.json()["measures"] == {
        **DEFAULT_MEASURES,
        "glass_ml": measure("glass_ml", 250, 50, 1000, 240),
    }
    assert teaspoon.json()["measures"]["teaspoon_ml"] == measure("teaspoon_ml", 5, 1, 20, 4)
    assert teaspoon.json()["measures"]["glass_ml"]["override"] == 240  # untouched
    assert tablespoon.json()["measures"]["tablespoon_ml"]["effective"] == 20
    assert restored.json()["measures"]["tablespoon_ml"] == DEFAULT_MEASURES["tablespoon_ml"]
    assert restored.json()["measures"]["glass_ml"]["override"] == 240
    assert restored.json()["measures"]["teaspoon_ml"]["override"] == 4


@pytest.mark.asyncio
async def test_a_value_equal_to_the_default_may_remain_an_explicit_override(tmp_path):
    async with chat_app(tmp_path) as chat:
        explicit = await change(chat.client, measures={"glass_ml": 250})
        after_reload = await profile(chat.client)

    assert explicit.json()["measures"]["glass_ml"] == {
        "override": 250,
        "effective": 250,
        "default": 250,
        "min": 50,
        "max": 1000,
    }
    assert after_reload["measures"]["glass_ml"]["override"] == 250


@pytest.mark.asyncio
async def test_the_range_boundaries_are_accepted_and_the_measures_survive_another_session(tmp_path):
    async with chat_app(tmp_path) as chat:
        low = await change(
            chat.client,
            measures={"glass_ml": 50, "teaspoon_ml": 1, "tablespoon_ml": 5},
        )
        high = await change(
            chat.client,
            measures={"glass_ml": 1000, "teaspoon_ml": 20, "tablespoon_ml": 50},
        )
        # Another browser signs in as the same person: the measures are the account's.
        other = chat.base.new_client()
        assert (await sign_in(other, chat.base.telegram)).status_code == 303
        elsewhere = await profile(other)

    assert low.status_code == 200 and high.status_code == 200
    assert low.json()["measures"]["glass_ml"]["effective"] == 50
    assert low.json()["measures"]["teaspoon_ml"]["effective"] == 1
    assert low.json()["measures"]["tablespoon_ml"]["effective"] == 5
    assert elsewhere["measures"] == {
        **DEFAULT_MEASURES,
        "glass_ml": measure("glass_ml", 250, 50, 1000, 1000),
        "teaspoon_ml": measure("teaspoon_ml", 5, 1, 20, 20),
        "tablespoon_ml": measure("tablespoon_ml", 15, 5, 50, 50),
    }


@pytest.mark.asyncio
async def test_no_cross_field_rule_requires_the_tablespoon_to_exceed_the_teaspoon(tmp_path):
    async with chat_app(tmp_path) as chat:
        response = await change(
            chat.client, measures={"teaspoon_ml": 20, "tablespoon_ml": 5}
        )
        after = await profile(chat.client)

    assert response.status_code == 200
    assert after["measures"]["teaspoon_ml"]["effective"] == 20
    assert after["measures"]["tablespoon_ml"]["effective"] == 5


@pytest.mark.asyncio
async def test_the_measures_are_persisted_in_the_database_not_only_in_the_answer(tmp_path):
    async with chat_app(tmp_path) as chat:
        assert (await change(chat.client, measures={"glass_ml": 240})).status_code == 200
        with chat.base.factory() as db:
            user = db.get(User, chat.user_id)
            row = (user.measure_glass_ml, user.measure_teaspoon_ml, user.measure_tablespoon_ml)

    assert row == (240, None, None)


@pytest.mark.asyncio
async def test_two_people_have_their_own_measures_and_nothing_crosses_between_them(tmp_path):
    async with chat_app(tmp_path) as chat:
        other = await chat.second_user()
        assert (await change(chat.client, measures={"glass_ml": 240})).status_code == 200
        assert (await change(other, measures={"glass_ml": 300})).status_code == 200

        mine = await profile(chat.client)
        theirs = await profile(other)

    assert mine["measures"]["glass_ml"]["override"] == 240
    assert theirs["measures"]["glass_ml"]["override"] == 300


@pytest.mark.asyncio
async def test_an_unusable_measure_writes_nothing_and_names_its_own_error(tmp_path):
    async with chat_app(tmp_path) as chat:
        before = await profile(chat.client)
        # The valid field in the same body is not written either: the whole change is refused.
        response = await change(
            chat.client,
            display_name="Иван",
            measures={"glass_ml": 240, "teaspoon_ml": 0},
        )
        after = await profile(chat.client)

    assert response.status_code == 400
    assert error_of(response)["detail"] == "invalid_measure"
    assert "50" in error_of(response)["message"] and "1000" in error_of(response)["message"]
    assert after == before


@pytest.mark.asyncio
async def test_the_measure_refusal_asks_no_provider(tmp_path):
    from tests.chat_support import ScriptedBackend

    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        assert (await change(chat.client, measures={"glass_ml": 49})).status_code == 400
        assert (await change(chat.client, measures={"glass_ml": 240})).status_code == 200

    assert backend.calls == 0


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
    # The household measures: a value must be a whole millilitre inside its own range, or
    # null to restore the default. Nothing is clamped or rounded into range.
    ({"measures": {"glass_ml": 49}}, "invalid_measure"),
    ({"measures": {"glass_ml": 1001}}, "invalid_measure"),
    ({"measures": {"glass_ml": 0}}, "invalid_measure"),
    ({"measures": {"glass_ml": -1}}, "invalid_measure"),
    ({"measures": {"glass_ml": 240.5}}, "invalid_measure"),
    ({"measures": {"glass_ml": 240.0}}, "invalid_measure"),
    ({"measures": {"glass_ml": "240"}}, "invalid_measure"),
    ({"measures": {"glass_ml": True}}, "invalid_measure"),
    ({"measures": {"glass_ml": False}}, "invalid_measure"),
    ({"measures": {"glass_ml": [240]}}, "invalid_measure"),
    ({"measures": {"glass_ml": {"ml": 240}}}, "invalid_measure"),
    ({"measures": {"teaspoon_ml": 0}}, "invalid_measure"),
    ({"measures": {"teaspoon_ml": 21}}, "invalid_measure"),
    ({"measures": {"tablespoon_ml": 4}}, "invalid_measure"),
    ({"measures": {"tablespoon_ml": 51}}, "invalid_measure"),
    ({"measures": {"glass_ml": 200, "cup_ml": 200}}, "invalid_request"),
    ({"measures": []}, "invalid_request"),
    ({"measures": "240"}, "invalid_request"),
    ({"measures": None}, "invalid_request"),
    ({"measures": {}}, "invalid_request"),  # an empty object alone is no change at all
    ({"display_name": "Иван", "measures": {"glass_ml": 240, "unknown": 1}}, "invalid_request"),
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
