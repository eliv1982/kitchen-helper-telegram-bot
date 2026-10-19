"""Shared helpers for the sign-in tests: a fake Telegram, signed test tokens, a
clock the tests move, and a harness that runs the real application against a real
(migrated) SQLite file.

Nothing leaves the process. Telegram is an httpx.MockTransport that implements the
provider's side of the protocol faithfully enough for a success to mean something:
it checks the client credentials, the redirect URI and the PKCE verifier against the
challenge it was given at the authorization request, hands out each code once, and
echoes the nonce of the authorization request into a token it signs with a key of
the test's own. Every departure from the protocol that the application commits is
recorded in `protocol_errors` (and answered as the provider would answer it), so a
test that expects a clean run asserts it is empty. Negative tests damage one thing
at a time through the hooks (`claims_hook`, `header_hook`, `signer`, `token_failure`,
`jwks_failure`).

The keys are generated once per test session: RSA key generation is the slow part.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, parse_qsl, urlsplit
from uuid import UUID

import httpx
import jwt
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm

from house_helper.persistence.allowlist import allow
from house_helper.persistence.database import (
    create_db_engine,
    create_session_factory,
    upgrade_database,
)
from house_helper.persistence.models import (
    AllowlistEntry,
    IdentityLink,
    OidcState,
    User,
)
from house_helper.persistence.models import Session as LoginSession
from house_helper.web.app import create_app
from house_helper.web.settings import AuthSettings, WebSettings
from house_helper.web.telegram_oidc import AUTHORIZATION_ENDPOINT, ISSUER, JWKS_URI, TOKEN_ENDPOINT
from tests.web_support import TIMEOUT, running, sqlite_url

ORIGIN = "https://kitchen.example.test"
CLIENT_ID = "1234567890"
CLIENT_SECRET = "client-secret-0f9e8d7c6b5a4938"
SUBJECT = "1234123412341234123"  # what Telegram's documentation shows for a `sub`
START = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime = START) -> None:
        self.current = now

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


# --- keys and tokens -------------------------------------------------------------


@dataclass(frozen=True)
class KeyPair:
    algorithm: str
    private: Any
    kid: str
    jwk: dict[str, Any]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwk(algorithm_class, public_key, kid: str, algorithm: str) -> dict[str, Any]:
    document = json.loads(algorithm_class.to_jwk(public_key))
    return {**document, "kid": kid, "alg": algorithm, "use": "sig"}


def make_rsa(kid: str = "oidc-1", bits: int = 2048) -> KeyPair:
    private = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    return KeyPair("RS256", private, kid, _jwk(RSAAlgorithm, private.public_key(), kid, "RS256"))


def make_ec(kid: str = "oidc-es256-1") -> KeyPair:
    private = ec.generate_private_key(ec.SECP256R1())
    return KeyPair("ES256", private, kid, _jwk(ECAlgorithm, private.public_key(), kid, "ES256"))


def make_ed(kid: str = "oidc-eddsa-1") -> KeyPair:
    private = ed25519.Ed25519PrivateKey.generate()
    return KeyPair("EdDSA", private, kid, _jwk(OKPAlgorithm, private.public_key(), kid, "EdDSA"))


def make_secp256k1_jwk(kid: str = "oidc-es256k-1") -> dict[str, Any]:
    """A key Telegram lists and this application never accepts."""
    private = ec.generate_private_key(ec.SECP256K1())
    document = json.loads(ECAlgorithm.to_jwk(private.public_key()))
    return {**document, "kid": kid, "alg": "ES256K", "use": "sig"}


_KEYS: dict[str, KeyPair] = {}


def key_for(algorithm: str) -> KeyPair:
    if not _KEYS:
        _KEYS.update({"RS256": make_rsa(), "ES256": make_ec(), "EdDSA": make_ed()})
    return _KEYS[algorithm]


def jwks_of(*keys: KeyPair, extra: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    return {"keys": [key.jwk for key in keys] + list(extra)}


def sign(
    claims: dict[str, Any],
    key: KeyPair,
    *,
    algorithm: Optional[str] = None,
    headers: Optional[dict[str, Any]] = None,
) -> str:
    # The JWS layer, not jwt.encode: that one checks the types of the claims, and the
    # tests need to sign claims of the wrong type to see that the application does.
    payload = json.dumps(claims, separators=(",", ":")).encode()
    return jwt.PyJWS().encode(
        payload,
        key.private,
        algorithm=algorithm or key.algorithm,
        headers={"kid": key.kid, **(headers or {})},
    )


def forge(header: dict[str, Any], claims: dict[str, Any], signature: bytes = b"") -> str:
    """A token assembled by hand: any header, any signature bytes."""
    head = _b64(json.dumps(header, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{head}.{body}.{_b64(signature)}"


def hmac_forgery(secret: bytes, header: dict[str, Any], claims: dict[str, Any]) -> str:
    """The classic key-confusion token: HS256 with the public key as the secret."""
    unsigned = forge(header, claims).rsplit(".", 1)[0]
    return f"{unsigned}.{_b64(hmac.new(secret, unsigned.encode(), hashlib.sha256).digest())}"


def _payload_segment(token: str) -> str:
    return token.split(".")[1]


def unsigned_variant(token: str, kid: str = "oidc-1", alg: str = "none") -> str:
    """The same claims, with the signature removed and `alg` changed."""
    header = _b64(json.dumps({"alg": alg, "typ": "JWT", "kid": kid}).encode())
    return f"{header}.{_payload_segment(token)}."


def hmac_variant(token: str, secret: bytes, kid: str = "oidc-1", alg: str = "HS256") -> str:
    """The same claims, 'signed' with `secret` as an HMAC key (key confusion)."""
    header = _b64(json.dumps({"alg": alg, "typ": "JWT", "kid": kid}).encode())
    unsigned = f"{header}.{_payload_segment(token)}"
    return f"{unsigned}.{_b64(hmac.new(secret, unsigned.encode(), hashlib.sha256).digest())}"


def tampered_variant(token: str, **changes: Any) -> str:
    """The same header and signature over claims that were changed afterwards."""
    head, body, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims.update(changes)
    return f"{head}.{_b64(json.dumps(claims, separators=(',', ':')).encode())}.{signature}"


def claims_for(
    clock: Callable[[], datetime],
    *,
    subject: str = SUBJECT,
    nonce: str = "nonce",
    audience: Any = CLIENT_ID,
    issuer: str = ISSUER,
) -> dict[str, Any]:
    issued = int(clock().timestamp())
    return {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "iat": issued,
        "exp": issued + 3600,
        "nonce": nonce,
    }


# --- Telegram ----------------------------------------------------------------------


@dataclass
class Grant:
    """One authorization request the fake provider accepted, and its code."""

    code: str
    subject: str
    state: str
    nonce: str
    challenge: str
    redirect_uri: str
    used: bool = False


class FakeTelegram:
    def __init__(
        self,
        clock: Callable[[], datetime],
        settings: AuthSettings,
        keys: Optional[tuple[KeyPair, ...]] = None,
    ) -> None:
        self.clock = clock
        self.settings = settings
        self.signer = key_for(settings.id_token_algorithm)
        self.keys = keys or (key_for("RS256"), key_for("ES256"), key_for("EdDSA"))
        self.grants: dict[str, Grant] = {}
        self.requests: list[tuple[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.protocol_errors: list[str] = []
        self.claims_hook: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None
        self.header_hook: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None
        self.token_hook: Optional[Callable[[str], str]] = None
        self.token_failure: Optional[Callable[[httpx.Request], httpx.Response]] = None
        self.jwks_failure: Optional[Callable[[httpx.Request], httpx.Response]] = None
        self.jwks_document: Optional[dict[str, Any]] = None
        self._gate: Optional[tuple[asyncio.Event, asyncio.Event]] = None
        self._codes = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def hold_token_requests(self) -> tuple[asyncio.Event, asyncio.Event]:
        """Make the token endpoint wait: returns (arrived, release). `arrived` is set
        when a request reaches it, and it answers once `release` is set."""
        self._gate = (asyncio.Event(), asyncio.Event())
        return self._gate

    @property
    def token_calls(self) -> int:
        return len(self.token_requests)

    @property
    def jwks_calls(self) -> int:
        return sum(1 for _, url in self.requests if url == JWKS_URI)

    # the browser's side ------------------------------------------------------------

    def authorize(self, location: str, subject: str = SUBJECT) -> str:
        """The person approves the login at Telegram: the URL the browser is sent
        back to (redirect_uri with a code and the state)."""
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZATION_ENDPOINT, location
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        assert all(len(values) == 1 for values in query.values()), "a repeated parameter"
        params = {name: values[0] for name, values in query.items()}
        expected = {
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "scope": "openid",
            "code_challenge_method": "S256",
        }
        assert {name: params.get(name) for name in expected} == expected, params
        assert set(params) == set(expected) | {"state", "nonce", "code_challenge"}, set(params)
        self._codes += 1
        code = f"code-{self._codes}-{params['state'][:8]}"
        self.grants[code] = Grant(
            code=code,
            subject=subject,
            state=params["state"],
            nonce=params["nonce"],
            challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
        )
        return f"{params['redirect_uri']}?code={code}&state={params['state']}"

    # the provider's side -------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, str(request.url)))
        if str(request.url) == TOKEN_ENDPOINT:
            if self._gate is not None:
                return self._held(request)
            return self._token(request)
        if str(request.url) == JWKS_URI:
            return self._jwks(request)
        self.protocol_errors.append(f"unexpected request to {request.url}")
        return httpx.Response(404)

    async def _held(self, request: httpx.Request) -> httpx.Response:
        arrived, release = self._gate
        arrived.set()
        await release.wait()
        return self._token(request)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
        self.token_requests.append(form)
        if self.token_failure is not None:
            return self.token_failure(request)
        if request.method != "POST":
            self.protocol_errors.append("the token endpoint was not called with POST")
        if set(form) != {"grant_type", "code", "redirect_uri", "client_id", "code_verifier"}:
            self.protocol_errors.append(f"token request fields: {sorted(form)}")
        credentials = base64.b64encode(
            f"{self.settings.client_id}:{self.settings.client_secret}".encode()
        ).decode()
        if request.headers.get("authorization") != f"Basic {credentials}":
            self.protocol_errors.append("client authentication")
            return httpx.Response(401, json={"error": "invalid_client"})
        grant = self.grants.get(form.get("code", ""))
        if grant is None or grant.used or form.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "invalid_grant"})
        grant.used = True
        challenge = _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
        if (
            challenge != grant.challenge
            or form.get("redirect_uri") != grant.redirect_uri
            or form.get("client_id") != self.settings.client_id
        ):
            self.protocol_errors.append("PKCE, redirect_uri or client_id mismatch")
            return httpx.Response(400, json={"error": "invalid_grant"})
        claims = claims_for(self.clock, subject=grant.subject, nonce=grant.nonce)
        if self.claims_hook is not None:
            claims = self.claims_hook(claims)
        headers = self.header_hook({}) if self.header_hook is not None else None
        token = sign(claims, self.signer, headers=headers)
        if self.token_hook is not None:
            token = self.token_hook(token)
        return httpx.Response(
            200,
            json={
                "access_token": "access-token-must-never-be-stored",
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": token,
            },
        )

    def _jwks(self, request: httpx.Request) -> httpx.Response:
        if self.jwks_failure is not None:
            return self.jwks_failure(request)
        document = self.jwks_document or jwks_of(*self.keys, extra=(make_secp256k1_jwk(),))
        return httpx.Response(200, json=document)


# --- the harness ---------------------------------------------------------------------


@dataclass
class Harness:
    app: Any
    client: httpx.AsyncClient
    telegram: FakeTelegram
    clock: FakeClock
    settings: AuthSettings
    factory: Any
    user_id: UUID
    origin: str = ORIGIN
    subject: str = SUBJECT
    extra_clients: list[httpx.AsyncClient] = field(default_factory=list)

    def new_client(self) -> httpx.AsyncClient:
        """A second browser: its own cookie jar, the same application."""
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=self.origin
        )
        self.extra_clients.append(client)
        return client


async def begin_sign_in(client: httpx.AsyncClient) -> httpx.Response:
    response = await client.get("/auth/telegram/login")
    assert response.status_code == 303, response.text
    return response


async def finish_sign_in(
    client: httpx.AsyncClient, telegram: FakeTelegram, started: httpx.Response, subject: str = SUBJECT
) -> httpx.Response:
    return await client.get(telegram.authorize(started.headers["location"], subject))


async def sign_in(
    client: httpx.AsyncClient, telegram: FakeTelegram, subject: str = SUBJECT
) -> httpx.Response:
    """A complete, honest sign-in by one browser; the callback's response."""
    return await finish_sign_in(client, telegram, await begin_sign_in(client), subject)


def logout_as(user_id: Any, **headers: str) -> dict[str, str]:
    """The headers of a logout that means to end `user_id`'s session (X-Expected-User),
    plus any others (an Origin, a cookie), given as keyword arguments."""
    return {"x-expected-user": str(user_id), **{name.replace("_", "-"): value for name, value in headers.items()}}


def auth_settings(origin: str = ORIGIN, algorithm: str = "RS256", **options: Any) -> AuthSettings:
    return AuthSettings(
        origin=origin,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        id_token_algorithm=algorithm,
        **options,
    )


def seed_user(factory, *, subject: str = SUBJECT, display_name: str = "Елена") -> UUID:
    """The one provisioned user, with a Telegram entry on the allowlist."""
    with factory() as session, session.begin():
        user = User(display_name=display_name)
        session.add(user)
        session.flush()
        allow(session, user, "telegram", subject)
        return user.id


@asynccontextmanager
async def auth_app(
    tmp_path,
    *,
    origin: str = ORIGIN,
    seed: bool = True,
    configure: Optional[Callable[[Any], None]] = None,
    chat_backend: Optional[Any] = None,
    provider: Optional[Any] = None,
    **options: Any,
):
    """The application with Telegram login on, started, and a client for it.
    `configure(app)` runs before the application starts (to add a test-only route).
    `chat_backend` answers chat messages (None: the chat is not configured, unless
    `provider` -- a ProviderSettings -- is given, which makes the application build the
    real OpenAI backend from it)."""
    url = sqlite_url(tmp_path / "auth.db")
    engine = create_db_engine(url)
    upgrade_database(engine)
    factory = create_session_factory(engine)
    user_id = seed_user(factory) if seed else None
    clock = FakeClock()
    settings = auth_settings(origin, **options)
    telegram = FakeTelegram(clock, settings)
    app = create_app(
        WebSettings(database_url=url, auth=settings, provider=provider),
        provider_transport=telegram.transport(),
        clock=clock,
        chat_backend=chat_backend,
    )
    if configure is not None:
        configure(app)
    harness = None
    try:
        async with running(app):
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=origin, timeout=TIMEOUT
            )
            harness = Harness(app, client, telegram, clock, settings, factory, user_id, origin)
            try:
                yield harness
            finally:
                for opened in [client, *harness.extra_clients]:
                    await asyncio.wait_for(opened.aclose(), TIMEOUT)
    finally:
        engine.dispose()


def cookie_attributes(response: httpx.Response, name: str) -> dict[str, Any]:
    """The attributes of the Set-Cookie header for `name`, lower-cased keys; a flag
    with no value maps to True. Fails if there is not exactly one such header."""
    matching = [
        value
        for value in response.headers.get_list("set-cookie")
        if value.split("=", 1)[0] == name
    ]
    assert len(matching) == 1, response.headers.get_list("set-cookie")
    pieces = [piece.strip() for piece in matching[0].split(";")]
    attributes: dict[str, Any] = {"value": pieces[0].split("=", 1)[1]}
    for piece in pieces[1:]:
        key, _, value = piece.partition("=")
        attributes[key.lower()] = value if value else True
    return attributes


# --- looking at the outcome ---------------------------------------------------------


def rows(harness: Harness, model: Any) -> list[Any]:
    """Every row of `model`, read by an independent session."""
    with harness.factory() as db:
        return list(db.scalars(sa.select(model)))


def counts(harness: Harness) -> dict[str, int]:
    with harness.factory() as db:
        return {
            model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model))
            for model in (User, AllowlistEntry, IdentityLink, LoginSession, OidcState)
        }


def database_text(harness: Harness) -> str:
    """Every value in every authentication table, as one string to search."""
    with harness.factory() as db:
        return "\n".join(
            repr(tuple(row))
            for table in ("oidc_states", "sessions", "identity_links", "allowlist_entries", "users")
            for row in db.execute(sa.text(f"SELECT * FROM {table}")).all()
        )


def app_log_text(caplog) -> str:
    """What the application and its servers logged, tracebacks included. The test
    client's own request lines are left out: httpx logs them (with the callback's
    code and state in the URL) for every request the *test* makes, which is not
    something the application did."""
    formatter = logging.Formatter()
    lines = []
    for record in caplog.records:
        message = record.getMessage()
        if record.name == "httpx" and "kitchen.example.test" in message:
            continue
        if record.name == "httpx" and "localhost" in message:
            continue
        lines.append(f"{record.name}: {message}")
        if record.exc_info:
            lines.append(formatter.formatException(record.exc_info))
    return "\n".join(lines)


def watch_logs(caplog) -> None:
    """Capture everything the application's loggers say at DEBUG, and nothing from
    SQLAlchemy (whose engine logger would print bound parameters if asked to)."""
    for name in ("house_helper", "httpx", "uvicorn", "starlette", "fastapi"):
        caplog.set_level(logging.DEBUG, logger=name)
