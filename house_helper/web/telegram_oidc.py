"""Telegram as an OpenID Connect provider: the Authorization Code flow with PKCE.

https://core.telegram.org/bots/telegram-login . What is used, and how:

  * The three endpoints and the issuer are constants here, the values of
    Telegram's discovery document (https://oauth.telegram.org/.well-known/
    openid-configuration). They are not discovered at run time: a discovery
    response is one more thing to trust, and the issuer is exactly what the ID
    token is checked against.
  * The authorization request asks for scope `openid` only. That is all the login
    needs (`sub`), and nothing else of the person's (name, picture, phone) is
    requested, received or stored.
  * state, nonce and the PKCE verifier come from the operating system's CSPRNG.
    The challenge is the S256 transform of the verifier.
  * The code is exchanged on the back channel, with the client credentials in an
    HTTP Basic header (as the documentation prescribes) and the verifier in the
    form body. Redirects are not followed, every request has a deadline for the
    whole exchange (httpx's own timeouts are per read, which a slow trickle never
    trips) and a response is read only up to a size limit.
  * The ID token is verified against the provider's JWKS, fetched on each login
    (logins are rare; not caching means a rotated key is picked up at once and
    there is no stale-key state to get wrong). The signature is checked by PyJWT's
    JWS layer for exactly ONE algorithm, the one configured (it must be what
    @BotFather is set to), with the key chosen by the token's `kid` from the
    provider's own key set; `none`, HMAC and every other algorithm are refused
    before any key is touched, and an RSA key under 2048 bits is refused. The
    claims are then validated here, against an injected clock: `iss` is exactly
    the issuer, `aud` is exactly this client, `exp` has not passed, `iat` is not in
    the future (60 s of clock skew), `nbf` if present, `nonce` equals the one
    generated for this login, and `sub` is a non-empty string. The `sub` is the
    external identity; nothing else in the token is read.

Assumptions NOT verified against a real token (no live credentials are used in
this repository): that Telegram echoes the authorization request's `nonce` into
the ID token (its documentation says so only for its JavaScript library; the flow
fails closed, with reason `wrong_nonce`, if it does not), and the format of `sub`
(documented as a string of digits, not promised).

Errors carry a fixed reason from a small vocabulary and never provider data: not a
token, not a code, not a response body. The logging here is the same: status codes
and reasons only.
"""

import base64
import hashlib
import hmac
import json
import logging
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import httpx
import anyio
import jwt
from jwt import PyJWK, PyJWS

from house_helper.web.settings import AuthSettings

logger = logging.getLogger(__name__)

PROVIDER = "telegram"

ISSUER = "https://oauth.telegram.org"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/auth"
TOKEN_ENDPOINT = f"{ISSUER}/token"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"
SCOPE = "openid"

# token_urlsafe(n) is ceil(4n/3) characters: 43, 43 and 64 (RFC 7636 wants a
# verifier of 43 to 128).
STATE_BYTES = 32
NONCE_BYTES = 32
VERIFIER_BYTES = 48

# Seconds for one whole request to the provider, connecting to the last byte.
REQUEST_DEADLINE_SECONDS = 20.0
MAX_RESPONSE_BYTES = 64 * 1024
MAX_ID_TOKEN_LENGTH = 16 * 1024
CLOCK_SKEW = timedelta(seconds=60)

# Header parameters that name a key, or change how a token is processed. The key
# comes from the provider's key set by `kid` and from nowhere else.
_FORBIDDEN_HEADERS = frozenset({"crit", "jku", "jwk", "x5u", "x5c", "x5t", "x5t#S256", "b64"})
_KEY_TYPES = {"RS256": "RSA", "ES256": "EC", "EdDSA": "OKP"}
_CURVES = {"ES256": "P-256", "EdDSA": "Ed25519"}


class OidcError(Exception):
    """Base of the errors below. Messages are fixed strings."""


class ProviderUnavailable(OidcError):
    """The provider could not be reached, or answered with something unusable."""


class ProviderRejected(OidcError):
    """The token endpoint refused the request (an expired or reused code, or bad
    client credentials)."""


class InvalidIdToken(OidcError):
    """The ID token is not acceptable. `reason` is one of a fixed set of words."""

    def __init__(self, reason: str) -> None:
        super().__init__("the ID token was rejected")
        self.reason = reason


@dataclass(frozen=True, repr=False)
class AuthorizationRequest:
    """A login about to start. Every field is a secret of the login (the URL holds
    the state), so the repr shows none of them."""

    url: str
    state: str
    nonce: str
    code_verifier: str

    def __repr__(self) -> str:
        return "AuthorizationRequest(...)"


def code_challenge(code_verifier: str) -> str:
    """PKCE S256: base64url(SHA-256(verifier)) without padding."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_authorization(settings: AuthSettings) -> AuthorizationRequest:
    state = secrets.token_urlsafe(STATE_BYTES)
    nonce = secrets.token_urlsafe(NONCE_BYTES)
    code_verifier = secrets.token_urlsafe(VERIFIER_BYTES)
    query = urlencode(
        [
            ("response_type", "code"),
            ("client_id", settings.client_id),
            ("redirect_uri", settings.redirect_uri),
            ("scope", SCOPE),
            ("state", state),
            ("nonce", nonce),
            ("code_challenge", code_challenge(code_verifier)),
            ("code_challenge_method", "S256"),
        ]
    )
    return AuthorizationRequest(
        url=f"{AUTHORIZATION_ENDPOINT}?{query}", state=state, nonce=nonce, code_verifier=code_verifier
    )


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class TelegramOidc:
    """The provider side of a login. `http` is the client the requests go through
    (the application owns it; tests give it a mock transport), `clock` the source
    of "now" for the claims."""

    def __init__(
        self, settings: AuthSettings, http: httpx.AsyncClient, clock: Callable[[], datetime]
    ) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock

    async def redeem(self, code: str, code_verifier: str, nonce: str) -> str:
        """Exchange the authorization code and return the verified `sub`.
        Raises ProviderUnavailable, ProviderRejected or InvalidIdToken."""
        id_token = await self._exchange_code(code, code_verifier)
        jwks = await self._fetch_jwks()
        return self.verify_id_token(id_token, jwks, nonce=nonce)

    # --- the back channel ---------------------------------------------------------

    async def _exchange_code(self, code: str, code_verifier: str) -> str:
        status, body = await self._request(
            "POST",
            TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.redirect_uri,
                "client_id": self._settings.client_id,
                "code_verifier": code_verifier,
            },
            auth=httpx.BasicAuth(self._settings.client_id, self._settings.client_secret),
        )
        if 400 <= status < 500:
            logger.warning("The Telegram token endpoint answered HTTP %d", status)
            raise ProviderRejected("the token endpoint refused the request")
        if status != 200:
            logger.warning("The Telegram token endpoint answered HTTP %d", status)
            raise ProviderUnavailable("the token endpoint did not answer usefully")
        id_token = _json_object(body).get("id_token")
        if not isinstance(id_token, str) or not 0 < len(id_token) <= MAX_ID_TOKEN_LENGTH:
            raise ProviderUnavailable("the token response holds no usable ID token")
        return id_token

    async def _fetch_jwks(self) -> dict[str, Any]:
        status, body = await self._request("GET", JWKS_URI)
        if status != 200:
            logger.warning("The Telegram key set answered HTTP %d", status)
            raise ProviderUnavailable("the key set could not be fetched")
        jwks = _json_object(body)
        if not isinstance(jwks.get("keys"), list):
            raise ProviderUnavailable("the key set is malformed")
        return jwks

    async def _request(self, method: str, url: str, **options: Any) -> tuple[int, bytes]:
        """One bounded request: (status, body). Network trouble, a redirect and an
        oversized body are all ProviderUnavailable, without a word of detail."""
        try:
            with anyio.fail_after(REQUEST_DEADLINE_SECONDS):
                async with self._http.stream(
                    method, url, headers={"Accept": "application/json"}, **options
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ProviderUnavailable("the response is too large")
                    return response.status_code, bytes(body)
        except (httpx.HTTPError, TimeoutError) as exc:
            logger.warning("A request to Telegram failed (%s)", type(exc).__name__)
            raise ProviderUnavailable("the provider could not be reached") from None

    # --- the ID token ---------------------------------------------------------------

    def verify_id_token(self, id_token: str, jwks: dict[str, Any], *, nonce: str) -> str:
        """The `sub` of an ID token that passed every check, or InvalidIdToken."""
        algorithm = self._settings.id_token_algorithm
        if not isinstance(id_token, str) or not 0 < len(id_token) <= MAX_ID_TOKEN_LENGTH:
            raise InvalidIdToken("malformed")
        jws = PyJWS()
        try:
            header = jws.get_unverified_header(id_token)
        except jwt.PyJWTError:
            raise InvalidIdToken("malformed") from None
        if header.get("alg") != algorithm:
            raise InvalidIdToken("algorithm_not_allowed")
        if _FORBIDDEN_HEADERS & header.keys():
            raise InvalidIdToken("unsupported_header")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidIdToken("missing_key_id")

        key = _select_key(jwks, kid, algorithm)
        try:
            decoded = jws.decode_complete(
                id_token,
                key=key,
                algorithms=[algorithm],
                options={"enforce_minimum_key_length": True},
            )
        except jwt.InvalidSignatureError:
            raise InvalidIdToken("bad_signature") from None
        except jwt.PyJWTError:
            raise InvalidIdToken("invalid_token") from None
        try:
            claims = json.loads(decoded["payload"])
        except ValueError:
            raise InvalidIdToken("invalid_claims") from None
        if not isinstance(claims, dict):
            raise InvalidIdToken("invalid_claims")
        return _validated_subject(claims, self._settings.client_id, nonce, self._clock())


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except ValueError:
        raise ProviderUnavailable("the response is not JSON") from None
    if not isinstance(value, dict):
        raise ProviderUnavailable("the response is not a JSON object")
    return value


def _select_key(jwks: dict[str, Any], kid: str, algorithm: str) -> PyJWK:
    """The one key of the provider's key set with this `kid`, if it is a
    verification key for exactly the configured algorithm."""
    published = jwks.get("keys")
    if not isinstance(published, list):
        raise InvalidIdToken("unknown_key")
    keys = [key for key in published if isinstance(key, dict) and key.get("kid") == kid]
    if len(keys) != 1:
        raise InvalidIdToken("unknown_key")
    key = keys[0]
    key_ops = key.get("key_ops")
    if (
        key.get("kty") != _KEY_TYPES[algorithm]
        or key.get("alg", algorithm) != algorithm
        or key.get("use", "sig") != "sig"
        or (key_ops is not None and (not isinstance(key_ops, list) or "verify" not in key_ops))
        or (algorithm in _CURVES and key.get("crv") != _CURVES[algorithm])
    ):
        raise InvalidIdToken("unsuitable_key")
    try:
        return PyJWK.from_dict(key, algorithm=algorithm)
    except Exception:  # PyJWT, cryptography and ValueError/TypeError all mean "unusable"
        raise InvalidIdToken("unusable_key") from None


def _validated_subject(claims: dict[str, Any], client_id: str, nonce: str, now: datetime) -> str:
    """The `sub`, after the claims checks of OpenID Connect Core 3.1.3.7 that apply
    to this flow. Each failure is a distinct reason, for the operator's log."""
    if claims.get("iss") != ISSUER:
        raise InvalidIdToken("wrong_issuer")
    audience = claims.get("aud")
    if audience != client_id and audience != [client_id]:
        raise InvalidIdToken("wrong_audience")
    authorized_party = claims.get("azp")
    if authorized_party is not None and authorized_party != client_id:
        raise InvalidIdToken("wrong_authorized_party")

    moment = now.timestamp()
    skew = CLOCK_SKEW.total_seconds()
    expiry = claims.get("exp")
    if not _is_number(expiry):
        raise InvalidIdToken("missing_expiry")
    if moment >= expiry:
        raise InvalidIdToken("expired")
    issued_at = claims.get("iat")
    if not _is_number(issued_at):
        raise InvalidIdToken("missing_issue_time")
    if issued_at > moment + skew:
        raise InvalidIdToken("issued_in_the_future")
    not_before = claims.get("nbf")
    if not_before is not None and (not _is_number(not_before) or not_before > moment + skew):
        raise InvalidIdToken("not_yet_valid")

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(
        token_nonce.encode("utf-8"), nonce.encode("utf-8")
    ):
        raise InvalidIdToken("wrong_nonce")
    subject: Optional[object] = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise InvalidIdToken("missing_subject")
    return subject
