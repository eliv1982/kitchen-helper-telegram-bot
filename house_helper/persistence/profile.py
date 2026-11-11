"""A person's profile: their display name, their text size, their household measures,
and the ways they sign in.

The profile is the account's own small set of settings, not a second identity. It reuses
users.display_name (the 200-character column every user already has; migration 0013 adds
users.text_size, migration 0014 the three measure columns) and it never says who the
request is for: every function takes the user id of the server-side session and finds a
row only through it. There is no user id in a request body and nothing here reads one.

Display name. Trimmed, 1 to 200 Unicode code points, and no character of Unicode
category C (control, format -- zero-width and bidirectional marks included -- surrogate,
private-use or unassigned): the same kind of rule the administrative commands apply to a
name they type, implemented here for this path rather than imported from them, because a
profile name is not an enrollment name. Duplicates ARE allowed: two people may have the
same display name, and a name never identifies anybody -- the session does. Reusing the
column means the name an operator set at enrollment is the name the person starts with.

Text size. One of TextSize: 'normal' (100%) or 'large' (120%). It is applied by the page
to the authenticated application only. The database's CHECK is the closed list; the
application validates before writing so a bad request is refused with a clear error
rather than a database failure.

Household measures. Three independent, optional capacities in whole millilitres (D18/D19):
glass 50--1000 (system default 250), teaspoon 1--20 (default 5) and tablespoon 5--50
(default 15). NULL removes the override and restores the default; nothing is seeded, and
a value equal to the default may be stored explicitly (it is still that person's choice).
They are target measures for presenting a converted recipe and never interpret a source
recipe. The ranges come from core.measures (the one place the approved numbers live), the
database repeats them as CHECKs, and this module validates a whole change before writing.

Sign-in methods. Read, never written here: the identity_links of the user, each joined to
its allowlist entry to tell an active identity from a revoked one. Only identities that
have actually been used to sign in have a link, so the list is "the ways this account has
signed in", not "the ways an administrator provisioned". A revoked entry keeps its link
(revoking withdraws access; it does not erase the record), so it appears with status
'revoked'. The Telegram subject is used only to find the allowlist entry and is never put
into a returned value: for an email identity the address is returned (the person's own
address, which is what the subject is); for Telegram nothing but the provider name is.

Writers write first, as everywhere in this layer: update_profile is one UPDATE whose WHERE
is the user id, so ownership is the statement's own condition. It never commits; the
caller owns the transaction (web.database.WebDatabase.run). Reads write nothing. Results
are plain values, not ORM objects.
"""

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.core.measures import (
    GLASS_ML_MAX,
    GLASS_ML_MIN,
    SYSTEM_GLASS_ML,
    SYSTEM_TABLESPOON_ML,
    SYSTEM_TEASPOON_ML,
    TABLESPOON_ML_MAX,
    TABLESPOON_ML_MIN,
    TEASPOON_ML_MAX,
    TEASPOON_ML_MIN,
)
from house_helper.persistence.models import (
    AllowlistEntry,
    AllowlistProvider,
    IdentityLink,
    TextSize,
    User,
)

# The column's width, in Unicode code points (users.display_name).
MAX_DISPLAY_NAME_LENGTH = 200

# Which text sizes exist. The database has the same closed list as a CHECK.
TEXT_SIZE_VALUES = frozenset(member.value for member in TextSize)

# The three personal household measures (D18/D19). The names are the API's own; the
# ranges and defaults are core.measures' product constants, never repeated as literals.
MEASURE_FIELDS = ("glass_ml", "teaspoon_ml", "tablespoon_ml")
MEASURE_RANGES = {
    "glass_ml": (int(GLASS_ML_MIN), int(GLASS_ML_MAX)),
    "teaspoon_ml": (int(TEASPOON_ML_MIN), int(TEASPOON_ML_MAX)),
    "tablespoon_ml": (int(TABLESPOON_ML_MIN), int(TABLESPOON_ML_MAX)),
}
MEASURE_DEFAULTS = {
    "glass_ml": int(SYSTEM_GLASS_ML),
    "teaspoon_ml": int(SYSTEM_TEASPOON_ML),
    "tablespoon_ml": int(SYSTEM_TABLESPOON_ML),
}
MEASURE_COLUMNS = {
    "glass_ml": "measure_glass_ml",
    "teaspoon_ml": "measure_teaspoon_ml",
    "tablespoon_ml": "measure_tablespoon_ml",
}

# The status a sign-in method is reported with.
LOGIN_ACTIVE = "active"
LOGIN_REVOKED = "revoked"

# Distinguishes "leave this field as it is" from "set it to this value" (None is not a
# value either field may take: display_name is never empty and text_size is never NULL).
_UNSET = object()


class ProfileNotFound(Exception):
    """No user with this id. A live session cannot normally reach it (a session is
    deleted with its user), but the read and the write report it rather than guessing."""


class InvalidDisplayName(ValueError):
    """The display name cannot be stored: empty, too long, or a forbidden character."""


class InvalidTextSize(ValueError):
    """The text size is not one of TEXT_SIZE_VALUES."""


class InvalidMeasure(ValueError):
    """A household measure is not a whole millilitre within its approved range, and None
    (which removes the override) is the only other value it may take."""


@dataclass(frozen=True)
class MeasureOverrides:
    """A person's three optional household capacities, in whole millilitres.

    None means no personal value: the system default applies (250/5/15 mL). The three are
    stored and read independently, and no rule relates one to another.
    """

    glass_ml: Optional[int]
    teaspoon_ml: Optional[int]
    tablespoon_ml: Optional[int]


@dataclass(frozen=True)
class LoginMethod:
    """One way the account has signed in, as the profile shows it.

    `address` is the verified email address for an email identity and None for every
    other provider: a Telegram subject is an internal identifier and is deliberately not
    part of this value (see the module docstring).
    """

    provider: str
    address: Optional[str]
    status: str  # LOGIN_ACTIVE or LOGIN_REVOKED


@dataclass(frozen=True)
class ProfileView:
    """What the profile API answers: the editable settings and the ways in."""

    display_name: Optional[str]
    text_size: str
    measures: MeasureOverrides
    login_methods: tuple[LoginMethod, ...]


def normalize_display_name(raw: str) -> str:
    """The display name as it is stored, or InvalidDisplayName.

    Trimmed the way every other stored text is (str.strip), non-empty, at most
    MAX_DISPLAY_NAME_LENGTH code points, and no Unicode category C character. The
    rule is the same one the administrative commands use, written here so that the
    profile path does not depend on them (and so a later change to one does not
    silently change the other).
    """
    if not isinstance(raw, str):
        raise InvalidDisplayName("the display name must be text")
    text = raw.strip()
    if not text:
        raise InvalidDisplayName("the display name must not be empty")
    if len(text) > MAX_DISPLAY_NAME_LENGTH:
        raise InvalidDisplayName(
            f"the display name is at most {MAX_DISPLAY_NAME_LENGTH} characters"
        )
    # Control, format (zero-width and bidirectional marks), surrogate, private-use and
    # unassigned characters: what a page, a log or a terminal could be made to
    # misrepresent. Combining marks are not category C and are ordinary text.
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise InvalidDisplayName(
            "the display name must not contain control or invisible characters"
        )
    return text


def normalize_text_size(raw: str) -> str:
    """'normal' or 'large', or InvalidTextSize."""
    if not isinstance(raw, str) or raw not in TEXT_SIZE_VALUES:
        raise InvalidTextSize("the text size must be 'normal' or 'large'")
    return raw


def normalize_measure(name: str, raw: Any) -> Optional[int]:
    """One household capacity as it is stored, or InvalidMeasure.

    None removes the override. Anything else must be a whole number of millilitres within
    that measure's approved inclusive range: a bool (which is an int subclass), a float, a
    string, an array or an out-of-range value is refused, never clamped or rounded.
    """
    if name not in MEASURE_FIELDS:
        raise ValueError(f"unknown measure {name!r}")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidMeasure(f"{name} must be a whole number of millilitres or None")
    minimum, maximum = MEASURE_RANGES[name]
    if not minimum <= raw <= maximum:
        raise InvalidMeasure(f"{name} must be within {minimum} and {maximum}")
    return raw


def normalize_measures(raw: Any) -> dict[str, Optional[int]]:
    """A mapping of measure names to values, every one validated before any is used.

    An unknown name is refused here too (the API refuses it as a malformed request
    before this point); leaving a name out means "keep that setting as it is".
    """
    if not isinstance(raw, dict):
        raise InvalidMeasure("measures must be an object")
    unknown = set(raw) - set(MEASURE_FIELDS)
    if unknown:
        raise InvalidMeasure(f"unknown measure {sorted(unknown)[0]!r}")
    return {name: normalize_measure(name, value) for name, value in raw.items()}


def get_profile(session: OrmSession, user_id: UUID) -> ProfileView:
    """The profile of `user_id`, read through the session's user id and nothing else."""
    return _view(session, user_id)


def update_profile(
    session: OrmSession,
    user_id: UUID,
    *,
    display_name: Any = _UNSET,
    text_size: Any = _UNSET,
    measures: Any = _UNSET,
    now: datetime,
) -> ProfileView:
    """Change the fields that are given (at least one), and return the profile.

    Ownership is the UPDATE's own WHERE: there is no separate read-check-write step. A
    field that is not named keeps its value; a measure named with None removes its
    override. Every value is validated before the one UPDATE (InvalidMeasure included), so
    a change with one bad field writes nothing at all. Raises InvalidDisplayName /
    InvalidTextSize / InvalidMeasure before writing, and ProfileNotFound when the user
    does not exist.
    """
    values: dict[str, Any] = {}
    if display_name is not _UNSET:
        values["display_name"] = normalize_display_name(display_name)
    if text_size is not _UNSET:
        values["text_size"] = normalize_text_size(text_size)
    if measures is not _UNSET:
        for name, value in normalize_measures(measures).items():
            values[MEASURE_COLUMNS[name]] = value
    if not values:
        raise ValueError("an update must name at least one field")
    values["updated_at"] = now
    result = session.execute(
        sa.update(User).where(User.id == user_id).values(**values)
    )
    if result.rowcount != 1:
        raise ProfileNotFound(str(user_id))
    return _view(session, user_id)


def _view(session: OrmSession, user_id: UUID) -> ProfileView:
    """The profile as one small SELECT by user id.

    The four settings columns are selected explicitly (text_size and the three measure
    columns are deferred on the model), so this is one read and does not depend on an ORM
    object having been loaded first.
    """
    row = session.execute(
        sa.select(
            User.display_name,
            User.text_size,
            User.measure_glass_ml,
            User.measure_teaspoon_ml,
            User.measure_tablespoon_ml,
        ).where(User.id == user_id)
    ).first()
    if row is None:
        raise ProfileNotFound(str(user_id))
    display_name, text_size, glass_ml, teaspoon_ml, tablespoon_ml = row
    return ProfileView(
        display_name=display_name,
        text_size=text_size or TextSize.NORMAL.value,
        measures=MeasureOverrides(
            glass_ml=glass_ml,
            teaspoon_ml=teaspoon_ml,
            tablespoon_ml=tablespoon_ml,
        ),
        login_methods=_login_methods(session, user_id),
    )


def _login_methods(session: OrmSession, user_id: UUID) -> tuple[LoginMethod, ...]:
    """The user's identities that have signed in, oldest link first.

    The email address is selected only for an email identity; the Telegram subject is
    used by the join and never selected into a result (see the module docstring).
    """
    rows = session.execute(
        sa.select(
            IdentityLink.provider,
            sa.case(
                (
                    IdentityLink.provider == AllowlistProvider.EMAIL.value,
                    IdentityLink.subject,
                ),
                else_=sa.null(),
            ).label("address"),
            AllowlistEntry.id.label("entry_id"),
            AllowlistEntry.revoked_at,
        )
        .outerjoin(
            AllowlistEntry,
            sa.and_(
                AllowlistEntry.user_id == IdentityLink.user_id,
                AllowlistEntry.provider == IdentityLink.provider,
                AllowlistEntry.subject == IdentityLink.subject,
            ),
        )
        .where(IdentityLink.user_id == user_id)
        .order_by(IdentityLink.created_at, IdentityLink.id)
    ).all()
    methods = []
    for provider, address, entry_id, revoked_at in rows:
        status = LOGIN_ACTIVE if entry_id is not None and revoked_at is None else LOGIN_REVOKED
        methods.append(LoginMethod(provider=provider, address=address, status=status))
    return tuple(methods)
