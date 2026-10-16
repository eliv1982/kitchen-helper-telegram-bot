"""Test-only offline guard: nothing in the test run may leave the machine.

tests/conftest.py installs it first thing, before the environment is forced and
before the application (or the OpenAI / aiogram / httpx stacks under it) is
imported, so neither import-time code nor fixtures can open a connection.

It wraps the few Python-level choke points every client library in this
project goes through, and lets only loopback traffic pass:

  * socket.socket.connect / connect_ex   -- blocking sockets, ssl, and the
    selector event loop (the default on Linux and macOS);
  * IocpProactor.connect                 -- the Windows default loop calls
    ConnectEx directly and never touches socket.socket.connect;
  * socket.getaddrinfo, gethostbyname, gethostbyname_ex, gethostbyaddr,
    getnameinfo                          -- name resolution, which asyncio
    (loop.getaddrinfo), anyio, httpx and aiohttp all route through.

A blocked attempt raises NetworkAccessBlocked and is recorded. The record
matters: the OpenAI SDK turns every exception into APIConnectionError, and the
application turns that into a friendly reply, so an accidentally unmocked call
could otherwise end as a test that still passes. The autouse fixture below,
which tests/conftest.py imports, fails any test that leaves a recorded attempt
unclaimed (see take_attempts).

It is a guard against accidents, not a sandbox. It does not see UDP datagrams
sent without connect(), AF_UNIX sockets, subprocesses, or C code that calls the
OS directly, and it only exists inside the pytest process that installed it.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from collections.abc import Callable
from typing import Any

import pytest

_MARK = "_house_helper_network_guard"

# The unpatched callables the wrappers forward to, looked up at call time so a
# test can swap one for a tripwire. Filled by install().
originals: dict[str, Callable[..., Any]] = {}

_attempts: list[str] = []
_modules_at_install: frozenset[str] = frozenset()

_NAME_LOOKUPS: dict[str, Callable[[Any], Any]] = {
    # name in the socket module -> the host inside its first argument
    "getaddrinfo": lambda host: host,
    "gethostbyname": lambda host: host,
    "gethostbyname_ex": lambda host: host,
    "gethostbyaddr": lambda host: host,
    "getnameinfo": lambda sockaddr: sockaddr[0],
}


class NetworkAccessBlocked(Exception):
    """A test tried to reach beyond the local machine.

    Deliberately not an OSError: libraries that retry or translate OSError
    (socket.create_connection, aiohttp's resolver, httpcore) must not mistake
    it for an ordinary connection failure.
    """


def _blocked(kind: str, target: object) -> NetworkAccessBlocked:
    attempt = f"{kind} {target!r}"
    _attempts.append(attempt)
    return NetworkAccessBlocked(
        f"external network access is blocked in tests ({attempt}); "
        "stub the call instead (see tests/network_guard.py)"
    )


def _is_loopback_ip(host: object) -> bool:
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_local_name(host: object) -> bool:
    if host is None:  # e.g. getaddrinfo(None, port) when binding a local server
        return True
    return isinstance(host, str) and (
        host.lower().rstrip(".") == "localhost" or _is_loopback_ip(host)
    )


def _require_loopback_peer(kind: str, family: int, address: object) -> None:
    # AF_UNIX and other non-IP families cannot reach the network by themselves.
    if family not in (socket.AF_INET, socket.AF_INET6):
        return
    host = address[0] if isinstance(address, tuple) and address else address
    # A hostname is refused even if it would resolve locally: connect() resolves
    # it in C, past the getaddrinfo guard. asyncio and create_connection resolve
    # first, so legitimate local callers always arrive here with an IP literal.
    if not _is_loopback_ip(host):
        raise _blocked(kind, address)


def _guarded_connect(key: str) -> Callable[..., Any]:
    def connect(self: socket.socket, address: Any) -> Any:
        _require_loopback_peer(key, self.family, address)
        return originals[key](self, address)

    return connect


def _guarded_lookup(key: str, host_of: Callable[[Any], Any]) -> Callable[..., Any]:
    def lookup(target: Any, /, *args: Any, **kwargs: Any) -> Any:
        host = host_of(target)
        if not _is_local_name(host):
            raise _blocked(key, host)
        return originals[key](target, *args, **kwargs)

    return lookup


def _guarded_proactor_connect(self: Any, conn: socket.socket, address: Any) -> Any:
    _require_loopback_peer("IocpProactor.connect", conn.family, address)
    return originals["IocpProactor.connect"](self, conn, address)


def _targets() -> list[tuple[Any, str, str]]:
    """(owner, attribute, key) for every call the guard replaces."""
    targets = [(socket.socket, name, f"socket.{name}") for name in ("connect", "connect_ex")]
    targets += [(socket, name, f"socket.{name}") for name in _NAME_LOOKUPS]
    if sys.platform == "win32":
        from asyncio import windows_events

        targets.append((windows_events.IocpProactor, "connect", "IocpProactor.connect"))
    return targets


def _wrapper_for(key: str) -> Callable[..., Any]:
    if key == "IocpProactor.connect":
        return _guarded_proactor_connect
    if key in ("socket.connect", "socket.connect_ex"):
        return _guarded_connect(key)
    return _guarded_lookup(key, _NAME_LOOKUPS[key.removeprefix("socket.")])


def install() -> None:
    """Patch the choke points for the rest of the process. Idempotent."""
    global _modules_at_install
    if is_installed():
        return
    _modules_at_install = frozenset(sys.modules)
    for owner, attribute, key in _targets():
        originals[key] = getattr(owner, attribute)
        wrapper = _wrapper_for(key)
        setattr(wrapper, _MARK, True)
        setattr(owner, attribute, wrapper)


def is_installed() -> bool:
    """True only if every guarded call is currently a guard wrapper."""
    return all(getattr(getattr(owner, name), _MARK, False) for owner, name, _ in _targets())


def modules_loaded_at_install() -> frozenset[str]:
    """Top-level names of the modules that were already imported when the
    guard went in; lets a test prove the application came after it."""
    return frozenset(name.partition(".")[0] for name in _modules_at_install)


def take_attempts() -> list[str]:
    """Return and clear the blocked attempts recorded so far.

    A test that blocks an attempt on purpose calls this to claim it; any
    attempt still recorded when a test ends is a failure (see
    assert_no_unclaimed_attempts).
    """
    taken = _attempts[:]
    del _attempts[: len(taken)]
    return taken


def assert_no_unclaimed_attempts() -> None:
    attempts = take_attempts()
    if attempts:
        raise AssertionError(
            "the test attempted external network access, which tests/network_guard.py "
            "blocked (the exception may have been swallowed by the code under test):\n  "
            + "\n  ".join(attempts)
        )


@pytest.fixture(autouse=True)
def no_unclaimed_network_attempts():
    """Fail the test if it made a blocked attempt it did not claim.

    Lives here, not in conftest.py, so the guard's own tests can load just this
    module (and not the whole application) into a nested pytest run.
    """
    yield
    assert_no_unclaimed_attempts()
