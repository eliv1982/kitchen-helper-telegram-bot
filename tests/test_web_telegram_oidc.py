"""Telegram as an OIDC provider: the authorization request, the back-channel
exchange, and the verification of the ID token.

Everything is deterministic: the provider is tests/auth_support.FakeTelegram behind
httpx.MockTransport, tokens are signed with keys the tests generate, and "now" is a
clock the tests hold. No test needs, or can reach, a live Telegram.

The ID token tests are a matrix over one good token: each case changes exactly one
thing and expects the one reason that thing deserves (the reasons are fixed words,
so an operator's log says why without quoting the token).
"""

import asyncio
import logging
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from house_helper.web import telegram_oidc
from house_helper.web.telegram_oidc import (
    AUTHORIZATION_ENDPOINT,
    ISSUER,
    JWKS_URI,
    MAX_ID_TOKEN_LENGTH,
    MAX_RESPONSE_BYTES,
    TOKEN_ENDPOINT,
    InvalidIdToken,
    ProviderRejected,
    ProviderUnavailable,
    TelegramOidc,
    code_challenge,
    new_authorization,
)
from tests.auth_support import (
    CLIENT_ID,
    CLIENT_SECRET,
    SUBJECT,
    FakeClock,
    FakeTelegram,
    auth_settings,
    claims_for,
    forge,
    hmac_forgery,
    jwks_of,
    key_for,
    make_rsa,
    make_secp256k1_jwk,
    sign,
)

NONCE = "the-nonce-of-this-login"
MISSING = object()
VERIFIER_ALPHABET = re.compile(r"[A-Za-z0-9\-._~]{43,128}")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def settings():
    return auth_settings()


def verifier_for(settings, clock, algorithm="RS256"):
    return TelegramOidc(auth_settings(algorithm=algorithm), None, clock)


def good_claims(clock, **changes):
    claims = claims_for(clock, nonce=NONCE)
    for name, value in changes.items():
        if value is MISSING:
            claims.pop(name, None)
        else:
            claims[name] = value
    return claims


def all_keys():
    return jwks_of(key_for("RS256"), key_for("ES256"), key_for("EdDSA"), extra=(make_secp256k1_jwk(),))


# --- the authorization request ---------------------------------------------------------------


def test_the_pkce_transform_matches_the_rfc_7636_test_vector():
    assert (
        code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_the_authorization_request_carries_exactly_what_the_flow_needs(settings):
    request = new_authorization(settings)
    parts = urlsplit(request.url)
    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)

    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZATION_ENDPOINT
    assert AUTHORIZATION_ENDPOINT == "https://oauth.telegram.org/auth"
    assert {name: values for name, values in query.items()} == {
        "response_type": ["code"],
        "client_id": [CLIENT_ID],
        "redirect_uri": ["https://kitchen.example.test/auth/telegram/callback"],
        "scope": ["openid"],  # nothing of the person's profile or phone is asked for
        "state": [request.state],
        "nonce": [request.nonce],
        "code_challenge": [code_challenge(request.code_verifier)],
        "code_challenge_method": ["S256"],
    }


def test_the_secrets_of_a_login_are_long_url_safe_and_never_repeat(settings):
    requests = [new_authorization(settings) for _ in range(200)]

    for field in ("state", "nonce", "code_verifier"):
        values = [getattr(request, field) for request in requests]
        assert len(set(values)) == 200
        assert all(re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in values)
    assert all(len(request.state) == 43 and len(request.nonce) == 43 for request in requests)
    assert all(VERIFIER_ALPHABET.fullmatch(request.code_verifier) for request in requests)
    assert all(len(request.code_verifier) == 64 for request in requests)
    # Three independent secrets, not one value reused.
    assert all(len({r.state, r.nonce, r.code_verifier}) == 3 for r in requests)


def test_the_state_the_nonce_and_the_verifier_come_from_the_systems_csprng(settings, monkeypatch):
    seen = []
    real = telegram_oidc.secrets.token_urlsafe
    monkeypatch.setattr(
        telegram_oidc.secrets, "token_urlsafe", lambda n: seen.append(n) or real(n)
    )

    new_authorization(settings)

    assert seen == [telegram_oidc.STATE_BYTES, telegram_oidc.NONCE_BYTES, telegram_oidc.VERIFIER_BYTES]
    assert min(seen) >= 32  # 256 bits of state and nonce


def test_the_redirect_uri_is_percent_encoded_in_the_query(settings):
    assert "redirect_uri=https%3A%2F%2Fkitchen.example.test%2Fauth%2Ftelegram%2Fcallback" in (
        new_authorization(settings).url
    )


def test_an_authorization_request_shows_none_of_its_secrets_in_its_repr(settings):
    request = new_authorization(settings)

    rendered = repr(request) + str(request)
    for secret in (request.state, request.nonce, request.code_verifier):
        assert secret not in rendered


def test_the_endpoints_are_telegrams_and_fixed():
    assert ISSUER == "https://oauth.telegram.org"
    assert TOKEN_ENDPOINT == "https://oauth.telegram.org/token"
    assert JWKS_URI == "https://oauth.telegram.org/.well-known/jwks.json"


# --- verifying an ID token: success ---------------------------------------------------------------


@pytest.mark.parametrize("algorithm", ["RS256", "ES256", "EdDSA"])
def test_a_good_token_yields_its_subject(clock, algorithm):
    oidc = verifier_for(None, clock, algorithm)
    token = sign(good_claims(clock), key_for(algorithm))

    assert oidc.verify_id_token(token, all_keys(), nonce=NONCE) == SUBJECT


def test_the_audience_may_be_the_one_element_list_and_extra_claims_are_ignored(clock):
    oidc = verifier_for(None, clock)
    claims = good_claims(
        clock,
        aud=[CLIENT_ID],
        azp=CLIENT_ID,
        name="Елена",
        preferred_username="elena",
        picture="https://t.me/i/userpic/x.jpg",
        phone_number="971577777777",
        id=987654321,
    )

    assert oidc.verify_id_token(sign(claims, key_for("RS256")), all_keys(), nonce=NONCE) == SUBJECT


def test_the_subject_is_returned_exactly_as_given_never_the_username(clock):
    oidc = verifier_for(None, clock)
    claims = good_claims(clock, sub="42", preferred_username="someone_else", name="Somebody Else")

    assert oidc.verify_id_token(sign(claims, key_for("RS256")), all_keys(), nonce=NONCE) == "42"


def test_clock_skew_within_a_minute_is_tolerated_for_issue_and_not_before_only(clock):
    oidc = verifier_for(None, clock)
    now = int(clock().timestamp())
    claims = good_claims(clock, iat=now + 60, nbf=now + 60)

    assert oidc.verify_id_token(sign(claims, key_for("RS256")), all_keys(), nonce=NONCE) == SUBJECT


def test_a_fractional_expiry_is_accepted(clock):
    oidc = verifier_for(None, clock)
    claims = good_claims(clock, exp=clock().timestamp() + 10.5)

    assert oidc.verify_id_token(sign(claims, key_for("RS256")), all_keys(), nonce=NONCE) == SUBJECT


def test_the_clock_given_decides_what_expired(clock):
    oidc = verifier_for(None, clock)
    token = sign(good_claims(clock), key_for("RS256"))
    clock.advance(minutes=59)
    assert oidc.verify_id_token(token, all_keys(), nonce=NONCE) == SUBJECT

    clock.advance(minutes=1)  # exactly exp: over
    with pytest.raises(InvalidIdToken) as caught:
        oidc.verify_id_token(token, all_keys(), nonce=NONCE)
    assert caught.value.reason == "expired"


# --- verifying an ID token: every way it can be wrong ---------------------------------------------------

CLAIM_CASES = [
    # issuer
    ("iss", "https://oauth.telegram.org/", "wrong_issuer"),
    ("iss", "http://oauth.telegram.org", "wrong_issuer"),
    ("iss", "https://oauth.telegram.org.evil.test", "wrong_issuer"),
    ("iss", "https://OAUTH.telegram.org", "wrong_issuer"),
    ("iss", ["https://oauth.telegram.org"], "wrong_issuer"),
    ("iss", None, "wrong_issuer"),
    ("iss", MISSING, "wrong_issuer"),
    # audience
    ("aud", "9999999999", "wrong_audience"),
    ("aud", CLIENT_ID + "0", "wrong_audience"),
    ("aud", int(CLIENT_ID), "wrong_audience"),
    ("aud", [CLIENT_ID, "9999999999"], "wrong_audience"),
    ("aud", ["9999999999"], "wrong_audience"),
    ("aud", [], "wrong_audience"),
    ("aud", None, "wrong_audience"),
    ("aud", MISSING, "wrong_audience"),
    ("azp", "9999999999", "wrong_authorized_party"),
    # expiry
    ("exp", MISSING, "missing_expiry"),
    ("exp", None, "missing_expiry"),
    ("exp", "9999999999", "missing_expiry"),
    ("exp", True, "missing_expiry"),
    ("exp", [1], "missing_expiry"),
    ("exp", float("nan"), "missing_expiry"),
    ("exp", float("inf"), "missing_expiry"),
    # issue time
    ("iat", MISSING, "missing_issue_time"),
    ("iat", "1700000000", "missing_issue_time"),
    ("iat", True, "missing_issue_time"),
    # not before
    ("nbf", "soon", "not_yet_valid"),
    # nonce
    ("nonce", "another-nonce", "wrong_nonce"),
    ("nonce", NONCE.upper(), "wrong_nonce"),
    ("nonce", NONCE + " ", "wrong_nonce"),
    ("nonce", "", "wrong_nonce"),
    ("nonce", None, "wrong_nonce"),
    ("nonce", 12345, "wrong_nonce"),
    ("nonce", [NONCE], "wrong_nonce"),
    ("nonce", MISSING, "wrong_nonce"),
    # subject
    ("sub", MISSING, "missing_subject"),
    ("sub", "", "missing_subject"),
    ("sub", None, "missing_subject"),
    ("sub", 1234123412341234123, "missing_subject"),
    ("sub", ["1234123412341234123"], "missing_subject"),
]


@pytest.mark.parametrize("claim, value, reason", CLAIM_CASES)
def test_a_token_with_one_wrong_claim_is_refused_for_that_claim(clock, claim, value, reason):
    oidc = verifier_for(None, clock)
    token = sign(good_claims(clock, **{claim: value}), key_for("RS256"))

    with pytest.raises(InvalidIdToken) as caught:
        oidc.verify_id_token(token, all_keys(), nonce=NONCE)

    assert caught.value.reason == reason


@pytest.mark.parametrize(
    "claim, offset, reason",
    [
        ("exp", 0, "expired"),
        ("exp", -1, "expired"),
        ("exp", -86400, "expired"),
        ("iat", 61, "issued_in_the_future"),
        ("iat", 3600, "issued_in_the_future"),
        ("nbf", 61, "not_yet_valid"),
    ],
)
def test_time_claims_are_checked_against_the_clock_with_no_slack_on_expiry(clock, claim, offset, reason):
    oidc = verifier_for(None, clock)
    now = int(clock().timestamp())
    token = sign(good_claims(clock, **{claim: now + offset}), key_for("RS256"))

    with pytest.raises(InvalidIdToken) as caught:
        oidc.verify_id_token(token, all_keys(), nonce=NONCE)

    assert caught.value.reason == reason


def test_the_expected_nonce_is_what_the_login_remembered_not_what_the_token_says(clock):
    oidc = verifier_for(None, clock)
    token = sign(good_claims(clock, nonce="attacker-chosen"), key_for("RS256"))

    with pytest.raises(InvalidIdToken) as caught:
        oidc.verify_id_token(token, all_keys(), nonce=NONCE)
    assert caught.value.reason == "wrong_nonce"
    assert oidc.verify_id_token(token, all_keys(), nonce="attacker-chosen") == SUBJECT


# --- signatures, algorithms, keys --------------------------------------------------------------------------


def reason_of(oidc, token, jwks=None):
    with pytest.raises(InvalidIdToken) as caught:
        oidc.verify_id_token(token, jwks if jwks is not None else all_keys(), nonce=NONCE)
    return caught.value.reason


def test_a_token_signed_by_another_key_with_the_right_kid_is_refused(clock):
    oidc = verifier_for(None, clock)
    attacker = make_rsa(kid=key_for("RS256").kid)

    assert reason_of(oidc, sign(good_claims(clock), attacker)) == "bad_signature"


def test_a_token_whose_payload_was_altered_is_refused(clock):
    oidc = verifier_for(None, clock)
    good = sign(good_claims(clock, sub="111"), key_for("RS256"))
    other = sign(good_claims(clock, sub="222"), key_for("RS256"))
    head, _, signature = good.rsplit(".", 2)[0], None, good.rsplit(".", 1)[1]
    altered = f"{good.split('.')[0]}.{other.split('.')[1]}.{signature}"

    assert reason_of(oidc, altered) == "bad_signature"


def test_a_token_with_a_damaged_signature_is_refused(clock):
    oidc = verifier_for(None, clock)
    token = sign(good_claims(clock), key_for("RS256"))
    head, body, signature = token.split(".")
    damaged = f"{head}.{body}.{signature[:-4]}AAAA"

    assert reason_of(oidc, damaged) == "bad_signature"


@pytest.mark.parametrize("alg", ["none", "None", "NONE", "nOnE", ""])
def test_an_unsigned_token_is_refused_before_any_key_is_looked_at(clock, alg):
    oidc = verifier_for(None, clock)
    token = forge({"alg": alg, "typ": "JWT", "kid": key_for("RS256").kid}, good_claims(clock))

    assert reason_of(oidc, token) == "algorithm_not_allowed"


@pytest.mark.parametrize("alg", ["HS256", "HS384", "HS512"])
def test_the_public_key_cannot_be_used_as_an_hmac_secret(clock, alg):
    oidc = verifier_for(None, clock)
    rsa_key = key_for("RS256")
    pem = rsa_key.private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    for secret in (pem, rsa_key.jwk["n"].encode()):
        token = hmac_forgery(secret, {"alg": alg, "typ": "JWT", "kid": rsa_key.kid}, good_claims(clock))
        assert reason_of(oidc, token) == "algorithm_not_allowed"


@pytest.mark.parametrize(
    "configured, signing",
    [("RS256", "ES256"), ("RS256", "EdDSA"), ("ES256", "RS256"), ("EdDSA", "ES256"), ("EdDSA", "RS256")],
)
def test_only_the_configured_algorithm_is_accepted(clock, configured, signing):
    oidc = verifier_for(None, clock, configured)
    token = sign(good_claims(clock), key_for(signing))  # a perfectly good token, for another algorithm

    assert reason_of(oidc, token) == "algorithm_not_allowed"


@pytest.mark.parametrize("alg", ["rs256", "RS256 ", "RS384", "PS256", "ES256K"])
def test_an_algorithm_spelled_or_chosen_differently_is_refused(clock, alg):
    oidc = verifier_for(None, clock)
    token = forge({"alg": alg, "kid": key_for("RS256").kid}, good_claims(clock), b"x")

    assert reason_of(oidc, token) == "algorithm_not_allowed"


def test_a_kid_the_provider_does_not_publish_is_refused(clock):
    oidc = verifier_for(None, clock)
    stranger = make_rsa(kid="oidc-99")

    assert reason_of(oidc, sign(good_claims(clock), stranger)) == "unknown_key"


@pytest.mark.parametrize("kid, reason", [
    (MISSING, "missing_key_id"),
    ("", "missing_key_id"),
    (None, "malformed"),  # PyJWT's own header check: a kid must be a string
    (1, "malformed"),
    (["oidc-1"], "malformed"),
])
def test_a_token_without_a_usable_kid_is_refused(clock, kid, reason):
    oidc = verifier_for(None, clock)
    header = {"alg": "RS256", "typ": "JWT"}
    if kid is not MISSING:
        header["kid"] = kid
    token = forge(header, good_claims(clock), b"x")

    assert reason_of(oidc, token) == reason


def test_a_kid_that_matches_two_keys_selects_neither(clock):
    oidc = verifier_for(None, clock)
    first = key_for("RS256")
    duplicate = make_rsa(kid=first.kid)

    assert reason_of(oidc, sign(good_claims(clock), first), jwks_of(first, duplicate)) == "unknown_key"


@pytest.mark.parametrize(
    "damage",
    [
        {"alg": "RS512"},
        {"alg": "ES256"},
        {"use": "enc"},
        {"key_ops": ["sign"]},
        {"key_ops": "verify"},
        {"kty": "EC"},
        {"kty": "oct"},
    ],
)
def test_a_published_key_that_is_not_a_verification_key_for_the_algorithm_is_refused(clock, damage):
    oidc = verifier_for(None, clock)
    key = key_for("RS256")
    jwks = {"keys": [{**key.jwk, **damage}]}

    assert reason_of(oidc, sign(good_claims(clock), key), jwks) == "unsuitable_key"


def test_a_published_key_with_no_declared_use_or_alg_is_still_usable(clock):
    oidc = verifier_for(None, clock)
    key = key_for("RS256")
    bare = {name: value for name, value in key.jwk.items() if name not in {"use", "alg"}}

    assert oidc.verify_id_token(sign(good_claims(clock), key), {"keys": [bare]}, nonce=NONCE) == SUBJECT


def test_a_published_key_with_the_right_ops_is_usable(clock):
    oidc = verifier_for(None, clock)
    key = key_for("RS256")

    jwks = {"keys": [{**key.jwk, "key_ops": ["verify"], "ext": True}]}  # as Telegram's RSA key is

    assert oidc.verify_id_token(sign(good_claims(clock), key), jwks, nonce=NONCE) == SUBJECT


def test_a_published_key_that_cannot_be_loaded_is_refused(clock):
    oidc = verifier_for(None, clock)
    key = key_for("RS256")
    broken = {**key.jwk, "n": "!!!not-base64!!!"}

    assert reason_of(oidc, sign(good_claims(clock), key), {"keys": [broken]}) == "unusable_key"


def test_an_elliptic_key_on_the_wrong_curve_is_refused(clock):
    oidc = verifier_for(None, clock, "ES256")
    key = key_for("ES256")
    on_another_curve = {**key.jwk, "crv": "P-384"}

    assert reason_of(oidc, sign(good_claims(clock), key), {"keys": [on_another_curve]}) == "unsuitable_key"


@pytest.mark.parametrize("keys", [[], "keys", None, {}, [None], [1, "x"], [[]]])
def test_a_key_set_without_the_key_refuses(clock, keys):
    oidc = verifier_for(None, clock)

    assert reason_of(oidc, sign(good_claims(clock), key_for("RS256")), {"keys": keys}) == "unknown_key"


@pytest.mark.filterwarnings("ignore:The RSA key is 1024 bits")  # signing with one, on purpose
def test_an_rsa_key_under_2048_bits_is_not_trusted(clock):
    oidc = verifier_for(None, clock)
    weak = make_rsa(kid="oidc-1", bits=1024)
    token = sign(good_claims(clock), weak)

    assert reason_of(oidc, token, jwks_of(weak)) == "invalid_token"


def test_the_unused_keys_of_the_set_do_not_matter(clock):
    # Telegram's set also holds a secp256k1 key this application never accepts.
    oidc = verifier_for(None, clock)
    token = sign(good_claims(clock), key_for("RS256"))

    assert oidc.verify_id_token(token, all_keys(), nonce=NONCE) == SUBJECT


@pytest.mark.parametrize(
    "header",
    [
        {"jku": "https://evil.test/jwks.json"},
        {"x5u": "https://evil.test/cert.pem"},
        {"x5c": ["MIIB"]},
        {"x5t": "abc"},
        {"x5t#S256": "abc"},
    ],
)
def test_headers_that_name_a_key_or_change_processing_are_refused(clock, header):
    oidc = verifier_for(None, clock)
    token = sign(good_claims(clock), key_for("RS256"), headers=header)

    assert reason_of(oidc, token) == "unsupported_header"


@pytest.mark.parametrize(
    "header",
    [
        {"crit": ["exp"]},
        {"crit": []},
        {"crit": "exp"},
        {"b64": False},
        {"crit": ["b64"], "b64": False},
    ],
)
def test_critical_extensions_and_unencoded_payloads_are_refused(clock, header):
    # Built by hand (the signing code would turn b64=false into a different token).
    # PyJWT's own header validation refuses all of these first; the application's
    # list of forbidden headers (checked next) is the second line, so either reason
    # is a refusal.
    oidc = verifier_for(None, clock)
    token = forge({"alg": "RS256", "kid": key_for("RS256").kid, **header}, good_claims(clock), b"x")

    assert reason_of(oidc, token) in {"malformed", "unsupported_header"}


def test_the_applications_own_crit_check_does_not_depend_on_pyjwt_refusing_first(clock, monkeypatch):
    oidc = verifier_for(None, clock)
    token = forge({"alg": "RS256", "kid": key_for("RS256").kid, "crit": ["exp"]}, good_claims(clock), b"x")
    # A PyJWT that let the header through (as an older or changed one might).
    monkeypatch.setattr(jwt.PyJWS, "_validate_headers", lambda *args, **kwargs: None)

    assert reason_of(oidc, token) == "unsupported_header"


def test_the_applications_own_b64_check_is_reached_by_a_detached_payload_token(clock):
    # header..signature: PyJWT's loader accepts the empty payload segment, and
    # checks b64 only when it verifies, which the application never gets to.
    oidc = verifier_for(None, clock)
    head = forge({"alg": "RS256", "kid": key_for("RS256").kid, "b64": False}, {}).split(".")[0]

    assert reason_of(oidc, f"{head}..eA") == "unsupported_header"


def test_a_key_embedded_in_the_token_is_never_used(clock):
    # The attacker signs with their own key and ships the public half in the header.
    oidc = verifier_for(None, clock)
    attacker = make_rsa(kid=key_for("RS256").kid)
    token = sign(good_claims(clock), attacker, headers={"jwk": attacker.jwk})

    assert reason_of(oidc, token) == "unsupported_header"


@pytest.mark.parametrize(
    "token",
    [
        "",
        ".",
        "..",
        "a",
        "a.b",
        "a.b.c",
        "a.b.c.d",
        "....",
        "not a token",
        "e30.e30.e30",  # {} . {} . ...: no alg
        "x" * (MAX_ID_TOKEN_LENGTH + 1),
        None,
        12345,
        b"bytes",
        ["a", "b", "c"],
    ],
)
def test_garbage_is_not_a_token(clock, token):
    oidc = verifier_for(None, clock)

    assert reason_of(oidc, token) in {"malformed", "algorithm_not_allowed"}


@pytest.mark.parametrize("payload", [b"not json", b"[1, 2]", b'"a string"', b"42", b"null", b""])
def test_a_signed_payload_that_is_not_a_json_object_is_refused(clock, payload):
    oidc = verifier_for(None, clock)
    key = key_for("RS256")
    token = jwt.PyJWS().encode(payload, key.private, algorithm="RS256", headers={"kid": key.kid})

    assert reason_of(oidc, token) == "invalid_claims"


def test_the_error_names_a_reason_and_never_the_token(clock):
    oidc = verifier_for(None, clock)
    token = sign(good_claims(clock, nonce="leak-marker-nonce"), key_for("RS256"))

    with pytest.raises(InvalidIdToken) as caught:
        oidc.verify_id_token(token, all_keys(), nonce=NONCE)

    for text in (str(caught.value), repr(caught.value), caught.value.reason):
        assert token not in text and "leak-marker-nonce" not in text
    assert caught.value.__cause__ is None and caught.value.__context__ is None


# --- the back channel -------------------------------------------------------------------------------------------


def fresh_login(telegram, settings, subject=SUBJECT):
    authorization = new_authorization(settings)
    callback = telegram.authorize(authorization.url, subject)
    code = parse_qs(urlsplit(callback).query)["code"][0]
    return authorization, code


def oidc_over(telegram, clock):
    http = httpx.AsyncClient(transport=telegram.transport(), follow_redirects=False)
    return TelegramOidc(telegram.settings, http, clock), http


@pytest.fixture
def telegram(clock, settings):
    return FakeTelegram(clock, settings)


@pytest.mark.asyncio
async def test_a_code_is_exchanged_and_the_token_verified(telegram, clock, settings):
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    async with http:
        subject = await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    assert subject == SUBJECT
    assert telegram.protocol_errors == []
    assert [(method, url) for method, url in telegram.requests] == [
        ("POST", TOKEN_ENDPOINT),
        ("GET", JWKS_URI),
    ]
    assert telegram.token_requests == [
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.redirect_uri,
            "client_id": CLIENT_ID,
            "code_verifier": authorization.code_verifier,
        }
    ]


@pytest.mark.asyncio
async def test_the_client_credentials_travel_in_the_basic_header_not_the_body(telegram, clock, settings):
    seen = {}
    real = telegram.handle

    def spy(request):
        if str(request.url) == TOKEN_ENDPOINT:
            seen["authorization"] = request.headers["authorization"]
            seen["content_type"] = request.headers["content-type"]
            seen["accept"] = request.headers["accept"]
            seen["body"] = request.content.decode()
        return real(request)

    telegram.handle = spy
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    async with http:
        await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    assert seen["authorization"].startswith("Basic ")
    assert CLIENT_SECRET not in seen["body"] and "client_secret" not in seen["body"]
    assert seen["content_type"] == "application/x-www-form-urlencoded"
    assert seen["accept"] == "application/json"


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["ES256", "EdDSA"])
async def test_the_other_botfather_algorithms_work_end_to_end(clock, algorithm):
    configured = auth_settings(algorithm=algorithm)
    telegram = FakeTelegram(clock, configured)
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, configured)

    async with http:
        assert await oidc.redeem(code, authorization.code_verifier, authorization.nonce) == SUBJECT


@pytest.mark.asyncio
async def test_a_code_works_once(telegram, clock, settings):
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    async with http:
        await oidc.redeem(code, authorization.code_verifier, authorization.nonce)
        with pytest.raises(ProviderRejected):
            await oidc.redeem(code, authorization.code_verifier, authorization.nonce)


@pytest.mark.asyncio
async def test_the_wrong_pkce_verifier_is_refused_by_the_provider(telegram, clock, settings):
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    async with http:
        with pytest.raises(ProviderRejected):
            await oidc.redeem(code, "v" * 64, authorization.nonce)

    assert telegram.protocol_errors == ["PKCE, redirect_uri or client_id mismatch"]
    assert telegram.jwks_calls == 0  # no key set is fetched for a login the provider refused


@pytest.mark.asyncio
async def test_wrong_client_credentials_are_refused_by_the_provider(telegram, clock, settings):
    wrong = auth_settings()
    object.__setattr__(wrong, "client_secret", "not-the-secret")
    oidc = TelegramOidc(wrong, httpx.AsyncClient(transport=telegram.transport()), clock)
    authorization, code = fresh_login(telegram, settings)

    with pytest.raises(ProviderRejected):
        await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    assert telegram.protocol_errors == ["client authentication"]


@pytest.mark.asyncio
async def test_a_token_for_another_login_is_refused_for_its_nonce(telegram, clock, settings):
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    async with http:
        with pytest.raises(InvalidIdToken) as caught:
            await oidc.redeem(code, authorization.code_verifier, "the-nonce-of-another-login")

    assert caught.value.reason == "wrong_nonce"


def respond(status, **kwargs):
    return lambda request: httpx.Response(status, **kwargs)


def fail_with(exception):
    def raiser(request):
        raise exception

    return raiser


BROKEN_TOKEN_ENDPOINTS = [
    ("server error", respond(500, text="boom")),
    ("unavailable", respond(503)),
    ("bad gateway", respond(502, text="<html>")),
    ("redirect", respond(302, headers={"location": "https://evil.test/token"})),
    ("not json", respond(200, text="<html>not json</html>")),
    ("empty body", respond(200, content=b"")),
    ("json list", respond(200, json=[])),
    ("json string", respond(200, json="id_token")),
    ("no id_token", respond(200, json={"access_token": "a", "token_type": "Bearer"})),
    ("id_token not a string", respond(200, json={"id_token": 123})),
    ("empty id_token", respond(200, json={"id_token": ""})),
    ("id_token too long", respond(200, json={"id_token": "x" * (MAX_ID_TOKEN_LENGTH + 1)})),
    ("body too large", respond(200, content=b" " * (MAX_RESPONSE_BYTES + 1))),
    ("no content", respond(204)),
    ("connect timeout", fail_with(httpx.ConnectTimeout("timed out"))),
    ("read timeout", fail_with(httpx.ReadTimeout("timed out"))),
    ("connection refused", fail_with(httpx.ConnectError("refused"))),
    ("protocol error", fail_with(httpx.RemoteProtocolError("garbage"))),
    ("TLS failure", fail_with(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"))),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name, behaviour", BROKEN_TOKEN_ENDPOINTS, ids=[c[0] for c in BROKEN_TOKEN_ENDPOINTS])
async def test_a_token_endpoint_that_fails_is_a_provider_outage(telegram, clock, settings, name, behaviour, caplog):
    telegram.token_failure = behaviour
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    with caplog.at_level(logging.DEBUG):
        async with http:
            with pytest.raises(ProviderUnavailable) as caught:
                await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    assert telegram.jwks_calls == 0
    assert [url for _, url in telegram.requests] == [TOKEN_ENDPOINT]  # a redirect is not followed
    for secret in (code, authorization.code_verifier, CLIENT_SECRET):
        assert secret not in str(caught.value) + repr(caught.value) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
async def test_a_token_endpoint_that_refuses_is_a_rejected_login(telegram, clock, settings, status, caplog):
    telegram.token_failure = respond(
        status, json={"error": "invalid_grant", "error_description": "description-marker"}
    )
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    with caplog.at_level(logging.DEBUG):
        async with http:
            with pytest.raises(ProviderRejected) as caught:
                await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    assert "description-marker" not in str(caught.value) + caplog.text
    assert f"HTTP {status}" in caplog.text  # the status is logged: it is what an operator needs


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["token_failure", "jwks_failure"])
async def test_a_provider_that_never_finishes_answering_is_given_up_on(
    telegram, clock, settings, monkeypatch, endpoint
):
    # httpx's timeouts are per read: an answer dripped slowly never trips them. The
    # deadline covers the whole exchange.
    monkeypatch.setattr(telegram_oidc, "REQUEST_DEADLINE_SECONDS", 0.2)

    async def stalls(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json={})

    setattr(telegram, endpoint, stalls)
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    async with http:
        started = asyncio.get_running_loop().time()
        with pytest.raises(ProviderUnavailable):
            await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    assert asyncio.get_running_loop().time() - started < 10


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["token", "key set"])
async def test_an_answer_that_would_be_fine_but_is_too_large_is_refused(telegram, clock, settings, endpoint):
    # Valid JSON, valid content, followed by whitespace past the limit: refused
    # because of its size alone (not because it fails to parse).
    wanted = TOKEN_ENDPOINT if endpoint == "token" else JWKS_URI
    real = telegram.handle
    padded = []

    def pad_the_wanted_answer(request):
        response = real(request)
        if str(request.url) != wanted:
            return response
        padded.append(wanted)
        body = response.content + b" " * (MAX_RESPONSE_BYTES + 1)
        return httpx.Response(response.status_code, content=body)

    telegram.handle = pad_the_wanted_answer
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    async with http:
        with pytest.raises(ProviderUnavailable):
            await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    assert padded == [wanted]


def test_the_deadline_for_a_request_is_tens_of_seconds_not_minutes():
    assert 5 <= telegram_oidc.REQUEST_DEADLINE_SECONDS <= 60


BROKEN_KEY_SETS = [
    ("server error", respond(500)),
    ("not found", respond(404)),
    ("redirect", respond(301, headers={"location": "https://evil.test/jwks"})),
    ("not json", respond(200, text="nope")),
    ("json list", respond(200, json=[])),
    ("no keys", respond(200, json={})),
    ("keys not a list", respond(200, json={"keys": "all of them"})),
    ("too large", respond(200, content=b" " * (MAX_RESPONSE_BYTES + 1))),
    ("timeout", fail_with(httpx.ReadTimeout("timed out"))),
    ("refused", fail_with(httpx.ConnectError("refused"))),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name, behaviour", BROKEN_KEY_SETS, ids=[c[0] for c in BROKEN_KEY_SETS])
async def test_a_key_set_that_cannot_be_fetched_fails_the_login(telegram, clock, settings, name, behaviour):
    telegram.jwks_failure = behaviour
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    async with http:
        with pytest.raises(ProviderUnavailable):
            await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    assert [url for _, url in telegram.requests] == [TOKEN_ENDPOINT, JWKS_URI]


@pytest.mark.asyncio
async def test_the_key_set_is_fetched_on_every_login_so_a_rotated_key_is_picked_up(telegram, clock, settings):
    oidc, http = oidc_over(telegram, clock)
    async with http:
        for _ in range(3):
            authorization, code = fresh_login(telegram, settings)
            await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    assert telegram.jwks_calls == 3


@pytest.mark.asyncio
async def test_a_rotated_key_is_used_as_soon_as_it_is_published(telegram, clock, settings):
    oidc, http = oidc_over(telegram, clock)
    rotated = make_rsa(kid="oidc-2")
    telegram.signer = rotated
    telegram.jwks_document = jwks_of(rotated)

    async with http:
        authorization, code = fresh_login(telegram, settings)
        assert await oidc.redeem(code, authorization.code_verifier, authorization.nonce) == SUBJECT


@pytest.mark.asyncio
async def test_a_token_signed_by_a_key_the_set_does_not_hold_fails_the_login(telegram, clock, settings):
    telegram.signer = make_rsa(kid="oidc-7")
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    async with http:
        with pytest.raises(InvalidIdToken) as caught:
            await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    assert caught.value.reason == "unknown_key"


@pytest.mark.asyncio
async def test_the_access_token_is_neither_used_nor_kept(telegram, clock, settings, caplog):
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    with caplog.at_level(logging.DEBUG):
        async with http:
            await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    assert "access-token-must-never-be-stored" not in caplog.text
    assert [method for method, _ in telegram.requests] == ["POST", "GET"]  # no userinfo call


@pytest.mark.asyncio
async def test_a_successful_exchange_logs_nothing_secret(telegram, clock, settings, caplog):
    oidc, http = oidc_over(telegram, clock)
    authorization, code = fresh_login(telegram, settings)

    with caplog.at_level(logging.DEBUG):
        async with http:
            await oidc.redeem(code, authorization.code_verifier, authorization.nonce)

    for secret in (code, authorization.code_verifier, authorization.nonce, authorization.state, CLIENT_SECRET):
        assert secret not in caplog.text
