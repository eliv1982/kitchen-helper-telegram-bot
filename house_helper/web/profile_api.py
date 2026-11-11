"""The profile API: the signed-in person's own settings and the ways they sign in.

Two routes under /api, built the way the favorites API's are (the same route class, so
the same correlation id, headers, error catalogue and log line; the same session, Origin
and body rules):

  GET    /api/profile   the profile
  PATCH  /api/profile   change the display name, the text size and/or the household
                        measures

Who. The user is whoever the session cookie resolves to on the server; a user id or an
owner in the request is never read. There is no path that names another account, and a
body that carries any field other than the two below (a user id included) is refused as
a request that cannot be understood. The profile is always edited by its owner, because
the UPDATE's WHERE is the session's own user id (persistence.profile).

The body of PATCH is a JSON object naming at least one of:

  display_name  a string, trimmed, 1 to 200 Unicode code points, without control, format,
                surrogate, private-use or unassigned characters. Duplicates are allowed.
  text_size     "normal" or "large" ("large" is 120% of the browser's size).
  measures      an object naming any of glass_ml, teaspoon_ml and tablespoon_ml with a
                whole number of millilitres within its approved range, or null to remove
                the override and restore the default. An absent key keeps its value; an
                empty object changes nothing else and is not a change of its own.

A field that is not named keeps its value. The complete body is validated before anything
is written, so one unusable field means nothing changes. Answers:

  200  the profile, in the same shape as GET
  400  invalid_request (a body that is not an object of these fields, an unknown field or
       measure, a value of the wrong kind, or an empty change), invalid_display_name,
       invalid_text_size or invalid_measure (a value that cannot be stored); nothing is
       written
  401  not_authenticated (the usual auth-loss answer)
  403  forbidden_origin (a PATCH from another site; a GET carries no such check)
  503  the database is not available; nothing is written

The answer. Both routes return

  {"display_name": <string or null>,
   "text_size": "normal" | "large",
   "measures": {"glass_ml": {"override": <int or null>, "effective": <int>,
                             "default": 250, "min": 50, "max": 1000},
                "teaspoon_ml": {...}, "tablespoon_ml": {...}},
   "login_methods": [{"provider": "telegram" | "email",
                      "address": <the email address, or null>,
                      "status": "active" | "revoked"}]}

`measures` is additive (KH-F01-B1): `override` is the person's stored value or null,
`effective` is that value or the system default, and the bounds are the approved ones.
No account is seeded with an override, so every existing person starts with three nulls.
These are the person's target measures for presenting a converted recipe; they never
interpret a source recipe.

login_methods is read-only and derived from the identity links that have actually signed
in (persistence.profile), oldest first; an identity whose allowlist entry was revoked is
reported as "revoked". A Telegram subject is never part of an answer or of a log line:
only the provider name is. Nothing here calls a provider or the assistant, and the profile
is never sent to one.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from house_helper.persistence import profile as profile_store
from house_helper.persistence.authn import AuthenticatedSession
from house_helper.web.chat_api import (
    ChatRoute,
    _read_json_object,
    authenticated,
    authenticated_mutation,
)
from house_helper.web.database import WebDatabase
from house_helper.web.dependencies import get_database
from house_helper.web.diagnostics import ApiError

# The largest body a valid change can send: 200 code points of a name, each at worst a
# twelve-byte JSON escape, the text size and the field names. Far smaller than a request
# may claim, and larger than any valid one.
MAX_PROFILE_BODY = 12 * profile_store.MAX_DISPLAY_NAME_LENGTH + 1024

_FIELDS = frozenset({"display_name", "text_size", "measures"})

router = APIRouter(prefix="/api", route_class=ChatRoute)


def _measures_json(measures: profile_store.MeasureOverrides) -> dict[str, Any]:
    """The additive measures object: the stored override, the effective value and the
    approved bounds, for each of the three independent settings."""
    result: dict[str, Any] = {}
    for name in profile_store.MEASURE_FIELDS:
        override = getattr(measures, name)
        minimum, maximum = profile_store.MEASURE_RANGES[name]
        result[name] = {
            "override": override,
            "effective": (
                profile_store.MEASURE_DEFAULTS[name] if override is None else override
            ),
            "default": profile_store.MEASURE_DEFAULTS[name],
            "min": minimum,
            "max": maximum,
        }
    return result


def _profile_json(profile: profile_store.ProfileView) -> dict[str, Any]:
    return {
        "display_name": profile.display_name,
        "text_size": profile.text_size,
        "measures": _measures_json(profile.measures),
        "login_methods": [
            {
                "provider": method.provider,
                "address": method.address,
                "status": method.status,
            }
            for method in profile.login_methods
        ],
    }


def _changes(document: dict) -> dict[str, Any]:
    """The fields a PATCH changes. Unknown fields, no field at all and a value of the
    wrong kind are invalid_request; a value that cannot be stored is the field's own
    error code (persistence.profile decides that). The whole body is validated here, so
    a change with one unusable field writes nothing."""
    if set(document) - _FIELDS or not document:
        raise ApiError("invalid_request", category="UnexpectedFields")
    changes: dict[str, Any] = {}
    if "display_name" in document:
        if not isinstance(document["display_name"], str):
            raise ApiError("invalid_request", category="UnexpectedFields")
        try:
            changes["display_name"] = profile_store.normalize_display_name(
                document["display_name"]
            )
        except profile_store.InvalidDisplayName:
            raise ApiError("invalid_display_name") from None
    if "text_size" in document:
        try:
            changes["text_size"] = profile_store.normalize_text_size(
                document["text_size"]
            )
        except profile_store.InvalidTextSize:
            raise ApiError("invalid_text_size") from None
    if "measures" in document:
        raw = document["measures"]
        if not isinstance(raw, dict) or set(raw) - set(profile_store.MEASURE_FIELDS):
            raise ApiError("invalid_request", category="UnexpectedFields")
        changes["measures"] = dict(raw)
    if not changes or (set(changes) == {"measures"} and not changes["measures"]):
        # An empty object names no measure and, alone, is not a change at all.
        raise ApiError("invalid_request", category="UnexpectedFields")
    # The values are validated (and the whole change refused) before the one UPDATE.
    try:
        for name, value in (
            changes["measures"].items() if "measures" in changes else ()
        ):
            profile_store.normalize_measure(name, value)
    except profile_store.InvalidMeasure:
        raise ApiError("invalid_measure") from None
    return changes


@router.get("/profile")
async def read_profile(
    current: AuthenticatedSession = Depends(authenticated),
    database: WebDatabase = Depends(get_database),
) -> Response:
    try:
        profile = await database.run(
            lambda session: profile_store.get_profile(session, current.user_id)
        )
    except profile_store.ProfileNotFound:
        # A live session whose user row is gone cannot be represented (the session would
        # have been deleted with the user); the answer is the ordinary auth-loss one.
        raise ApiError("not_authenticated") from None
    return JSONResponse(_profile_json(profile))


@router.patch("/profile")
async def update_profile(
    request: Request,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    changes = _changes(await _read_json_object(request, limit=MAX_PROFILE_BODY))
    now = request.app.state.clock()
    try:
        profile = await database.run(
            lambda session: profile_store.update_profile(
                session, current.user_id, now=now, **changes
            )
        )
    except profile_store.ProfileNotFound:
        raise ApiError("not_authenticated") from None
    except profile_store.InvalidDisplayName:
        raise ApiError("invalid_display_name") from None
    except profile_store.InvalidTextSize:
        raise ApiError("invalid_text_size") from None
    except profile_store.InvalidMeasure:
        raise ApiError("invalid_measure") from None
    return JSONResponse(_profile_json(profile))
