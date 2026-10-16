"""The shape of every identifier that comes from outside: idempotency keys and
the (provider, subject) pair of the allowlist and of identity links.

These are the application-side rules. models.py spells the same rules as CHECK
constraints (printable ASCII, length bounds, the email structure), so that a
value which gets past the application, or never went through it, is still
refused by the database; tests/test_persistence_identifiers.py runs one list of
values through both and fails if they disagree. SQLite does not enforce the
declared width of a VARCHAR column, which is why the widths are CHECKs too, and
it does not enforce the storage class either: a Python bytes value is stored as a
BLOB, which is not an identifier. So the application refuses anything but str,
and the character-set CHECK requires typeof() = 'text'.

One character set for all of them: the printable ASCII characters "!" to "~".
No whitespace, no control character (NUL included), nothing outside ASCII. That
is deliberate and strict:

  * it keeps look-alikes out (zero-width characters, a no-break space, Cyrillic
    letters that look Latin, and "K" the Kelvin sign, which str.lower() turns
    into an ASCII "k": an address containing it would otherwise normalize to an
    allowlisted one), so the text is checked for ASCII *before* it is
    lower-cased;
  * the database can say the same thing in both of its dialects, which it
    cannot for Unicode categories.

The price: an internationalized email address (non-ASCII local part or domain)
is refused, which fails closed. Supporting one means choosing a normalization
(IDNA, NFC) first; that is a decision for the sign-in increment, not a default.
"""

import enum
import re
from typing import Final

MAX_IDEMPOTENCY_KEY_LENGTH: Final = 128  # the width of turn_requests.idempotency_key
MAX_SUBJECT_LENGTH: Final = 255  # the width of allowlist_entries.subject / identity_links.subject
MAX_EMAIL_LENGTH: Final = 320  # the width of identity_links.email (a display snapshot)

# Written out here, not imported from models.py: models.py imports this module.
PROVIDER_TELEGRAM: Final = "telegram"
PROVIDER_EMAIL: Final = "email"
PROVIDERS: Final = frozenset({PROVIDER_TELEGRAM, PROVIDER_EMAIL})

_PRINTABLE_ASCII = re.compile(r"[!-~]+")


def is_printable_ascii(text: object) -> bool:
    """True for a non-empty str made only of "!" to "~". fullmatch, not $: a
    trailing newline must not slip through."""
    return isinstance(text, str) and _PRINTABLE_ASCII.fullmatch(text) is not None


def validate_idempotency_key(key: object) -> str:
    """The key itself, or ValueError. Chosen by the client, so nothing about it
    is trusted: 1 to 128 printable ASCII characters."""
    if not isinstance(key, str):
        raise ValueError("an idempotency key must be text")
    if not 1 <= len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(f"an idempotency key is 1 to {MAX_IDEMPOTENCY_KEY_LENGTH} characters")
    if not is_printable_ascii(key):
        raise ValueError("an idempotency key is printable ASCII without spaces")
    return key


def normalize_identity(provider: object, subject: object) -> tuple[str, str]:
    """The (provider, subject) pair as it is stored; ValueError if it cannot be one.

    telegram  the `sub` claim of a validated Telegram OIDC ID token, exactly as
              given (never trimmed, never a username: usernames change hands).
              Telegram's documentation shows a 19-digit numeric string but does
              not promise the format, so any printable ASCII is accepted, minus
              "@" so that it can never be mistaken for an address or a handle.
    email     a verified address: surrounding ASCII whitespace removed,
              lower-cased, exactly one "@" with something on both sides. Nothing
              more is folded together: "cook+tag@example.com" and
              "cook@example.com" are different identities, because treating
              aliases as one would let anyone who can create an alias walk in.
    """
    if isinstance(provider, enum.Enum):  # an AllowlistProvider
        provider = provider.value
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("unknown identity provider")
    if not isinstance(subject, str):
        raise ValueError("an identity subject must be text")

    if provider == PROVIDER_EMAIL:
        subject = subject.strip(" \t\n\r\f\v")
        if not is_printable_ascii(subject):  # before lower(): see the module docstring
            raise ValueError("an email address is printable ASCII")
        subject = subject.lower()
        local, _, domain = subject.partition("@")
        if subject.count("@") != 1 or not local or not domain:
            raise ValueError("not an email address")
    else:
        if not is_printable_ascii(subject):
            raise ValueError("a telegram subject is printable ASCII")
        if "@" in subject:
            raise ValueError("a telegram subject is not an email address or a handle")

    if len(subject) > MAX_SUBJECT_LENGTH:
        raise ValueError(f"an identity subject is at most {MAX_SUBJECT_LENGTH} characters")
    return provider, subject
