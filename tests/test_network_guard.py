"""The offline guard (tests/network_guard.py) blocks everything but loopback.

Every attempt below is aimed at a documentation-only address (RFC 5737 /
RFC 3849) or at api.openai.com, and none of them can leave the machine even if
the guard were broken: inside `tripwire()` the real network primitives the
guard forwards to are replaced with recorders. "Blocked" therefore means two
things at once -- NetworkAccessBlocked was raised, and the real primitive was
never reached. The loopback cases are the positive controls that show the
tripwire does see traffic the guard lets through.
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import call

import openai
import pytest
from aiogram import Bot

import api_client
from tests import expected_texts, network_guard, seams
from tests.helpers import FakeBot, FakeMessage
from tests.network_guard import NetworkAccessBlocked

EXTERNAL_IP = "203.0.113.7"  # TEST-NET-3, reserved for documentation
EXTERNAL_IPV6 = "2001:db8::1"  # documentation prefix
EXTERNAL_HOST = "api.openai.com"
REPO_ROOT = Path(__file__).resolve().parent.parent

NAME_LOOKUPS = {
    "getaddrinfo": (EXTERNAL_HOST, 443),
    "gethostbyname": (EXTERNAL_HOST,),
    "gethostbyname_ex": (EXTERNAL_HOST,),
    "gethostbyaddr": (EXTERNAL_IP,),
    "getnameinfo": ((EXTERNAL_IP, 443), 0),
}

LOOP_FACTORIES = [pytest.param(asyncio.SelectorEventLoop, id="selector-loop")]
if sys.platform == "win32":
    # The Windows default; it connects through IocpProactor, not socket.connect.
    LOOP_FACTORIES.append(pytest.param(asyncio.ProactorEventLoop, id="proactor-loop"))


class Tripwire:
    """Records every call that gets past the guard to a real primitive."""

    def __init__(self) -> None:
        self.reached: list[str] = []


@contextlib.contextmanager
def tripwire():
    """While active, nothing the guard lets through is actually performed.

    Arm it around the attempt only, never around event-loop creation: on
    Windows a selector loop connects a loopback socketpair to itself, and a
    tripwire would swallow that connect and leave the loop waiting forever.
    """
    # If the guard is not in place there is nothing to catch the attempt, and
    # the test would be a real one. Refuse to run it.
    assert network_guard.is_installed(), "offline guard is not installed"
    wire = Tripwire()
    with pytest.MonkeyPatch.context() as patch:
        for key in network_guard.originals:

            def recorder(*args, _key=key, **kwargs):
                wire.reached.append(_key)

            patch.setitem(network_guard.originals, key, recorder)
        yield wire


@pytest.fixture(params=LOOP_FACTORIES)
def loop(request):
    # Created here, before any tripwire is armed (see tripwire()).
    event_loop = request.param()
    yield event_loop
    event_loop.run_until_complete(event_loop.shutdown_default_executor())
    event_loop.close()


def _blocked_in_chain(error: BaseException | None) -> bool:
    while error is not None:
        if isinstance(error, NetworkAccessBlocked):
            return True
        error = error.__cause__ or error.__context__
    return False


# --- the guard itself -------------------------------------------------------


def test_guard_was_installed_before_the_application_was_imported():
    imported_before_guard = network_guard.modules_loaded_at_install()

    network_modules = {
        "api_client", "bot", "config", "context_manager",
        "openai", "aiogram", "httpx", "aiohttp",
    }  # fmt: skip
    assert imported_before_guard & network_modules == set()


def test_installing_again_does_not_wrap_the_guard_in_itself():
    before = dict(network_guard.originals)

    network_guard.install()

    assert network_guard.originals == before


def test_an_attempt_swallowed_by_the_code_under_test_still_fails_the_test():
    with tripwire() as wire:
        with contextlib.suppress(NetworkAccessBlocked):
            socket.getaddrinfo(EXTERNAL_HOST, 443)

    # This is what the autouse fixture runs when the test ends.
    with pytest.raises(AssertionError, match=EXTERNAL_HOST):
        network_guard.assert_no_unclaimed_attempts()

    assert wire.reached == []
    assert network_guard.take_attempts() == []  # and the record is now consumed


def test_unclaimed_attempts_are_checked_after_every_test(request):
    assert "no_unclaimed_network_attempts" in request.fixturenames


def test_a_swallowed_attempt_fails_the_test_that_made_it(tmp_path):
    # A real nested pytest run: the test below swallows the blocked attempt
    # the way the OpenAI SDK would, and only the autouse check can fail it.
    (tmp_path / "conftest.py").write_text(
        "from tests import network_guard\n"
        "from tests.network_guard import no_unclaimed_network_attempts  # noqa: F401\n"
        "network_guard.install()\n",
        encoding="utf-8",
    )
    (tmp_path / "test_swallowing.py").write_text(
        textwrap.dedent(
            f"""
            import socket

            from tests import network_guard


            def test_swallows_the_blocked_attempt():
                assert network_guard.is_installed()  # never attempt it unguarded
                try:
                    socket.getaddrinfo({EXTERNAL_HOST!r}, 443)
                except network_guard.NetworkAccessBlocked:
                    pass
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
            "--rootdir", str(tmp_path), "--confcutdir", str(tmp_path), str(tmp_path),
        ],  # fmt: skip
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode != 0, result.stdout
    assert "1 passed, 1 error" in result.stdout, result.stdout
    assert f"getaddrinfo {EXTERNAL_HOST!r}" in result.stdout, result.stdout


# --- sockets and name resolution -------------------------------------------


@pytest.mark.parametrize("name", NAME_LOOKUPS)
def test_external_name_lookups_are_blocked(name):
    with tripwire() as wire, pytest.raises(NetworkAccessBlocked):
        getattr(socket, name)(*NAME_LOOKUPS[name])

    assert wire.reached == []
    assert len(network_guard.take_attempts()) == 1


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("getaddrinfo", ("127.0.0.1", 80)),
        ("getaddrinfo", ("::1", 80)),
        ("getaddrinfo", ("localhost", 80)),
        ("getaddrinfo", (None, 80)),
        ("gethostbyname", ("localhost",)),
        ("getnameinfo", (("127.0.0.1", 80), 0)),
    ],
)
def test_loopback_name_lookups_pass_through(name, args):
    with tripwire() as wire:
        getattr(socket, name)(*args)

    assert wire.reached == [f"socket.{name}"]
    assert network_guard.take_attempts() == []


@pytest.mark.parametrize("address", [(EXTERNAL_IP, 443), (EXTERNAL_HOST, 443)])
def test_external_tcp_connections_are_blocked(address):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with tripwire() as wire:
            with pytest.raises(NetworkAccessBlocked):
                sock.connect(address)
            with pytest.raises(NetworkAccessBlocked):
                sock.connect_ex(address)

    assert wire.reached == []
    assert len(network_guard.take_attempts()) == 2


def test_external_ipv6_connections_are_blocked():
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("IPv6 sockets are unavailable on this machine")
    with sock, tripwire() as wire, pytest.raises(NetworkAccessBlocked):
        sock.connect((EXTERNAL_IPV6, 443, 0, 0))

    assert wire.reached == []
    assert len(network_guard.take_attempts()) == 1


def test_create_connection_to_an_external_host_is_blocked():
    with tripwire() as wire, pytest.raises(NetworkAccessBlocked):
        socket.create_connection((EXTERNAL_HOST, 443), timeout=1)

    assert wire.reached == []
    assert len(network_guard.take_attempts()) == 1


def test_loopback_connections_pass_through():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, tripwire() as wire:
        sock.connect(("127.0.0.1", 9))
        sock.connect_ex(("127.0.0.1", 9))

    assert wire.reached == ["socket.connect", "socket.connect_ex"]
    assert network_guard.take_attempts() == []


# --- asyncio ----------------------------------------------------------------


@pytest.mark.parametrize("host", [EXTERNAL_IP, EXTERNAL_HOST], ids=["ip", "hostname"])
def test_asyncio_connections_to_an_external_host_are_blocked(loop, host):
    async def attempt():
        await asyncio.get_running_loop().create_connection(asyncio.Protocol, host, 443)

    with tripwire() as wire, pytest.raises(NetworkAccessBlocked):
        loop.run_until_complete(attempt())

    assert wire.reached == []
    assert len(network_guard.take_attempts()) == 1


def test_asyncio_still_works_over_loopback(loop):
    # No tripwire: this talks to a real server on this machine, which is the
    # one kind of traffic the suite is allowed to have.
    async def echo_once():
        async def handle(reader, writer):
            writer.write(await reader.readexactly(4))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping")
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(4), timeout=5)
        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()
        return echoed

    assert loop.run_until_complete(echo_once()) == b"ping"
    assert network_guard.take_attempts() == []


# --- the libraries this project actually uses -------------------------------


@pytest.mark.asyncio
async def test_an_unmocked_openai_call_fails_locally_without_a_request(monkeypatch):
    client = api_client.get_client()
    monkeypatch.setattr(client, "max_retries", 0)  # one attempt, no backoff sleep

    try:
        with tripwire() as wire, pytest.raises(openai.APIConnectionError) as failure:
            await api_client.get_chat_response(
                [{"role": "user", "content": "привет"}], user_id=1, context_len=1
            )
    finally:
        await api_client.close_client()

    assert _blocked_in_chain(failure.value)
    assert wire.reached == []
    [attempt] = network_guard.take_attempts()
    assert EXTERNAL_HOST in attempt


@pytest.mark.asyncio
async def test_a_bot_test_with_an_unpatched_ai_seam_cannot_reach_openai(monkeypatch):
    # The mutation-testing incident: the seam that stands in for the AI call
    # was broken, so the real client ran. Here no stand-in is installed at all.
    client = api_client.get_client()
    monkeypatch.setattr(client, "max_retries", 0)
    msg = FakeMessage(user_id=5, text="привет")

    try:
        with tripwire() as wire:
            await seams.process_text(msg, FakeBot())
    finally:
        await api_client.close_client()

    assert msg.answer.await_args_list == [call(expected_texts.CONNECTION_FAILED)]
    assert seams.history(5) == []
    assert wire.reached == []
    [attempt] = network_guard.take_attempts()
    assert EXTERNAL_HOST in attempt


@pytest.mark.asyncio
async def test_an_unmocked_telegram_call_fails_locally_without_a_request():
    bot = Bot(token="123456:TEST-TOKEN")  # aiogram's real HTTP session
    try:
        with tripwire() as wire, pytest.raises(NetworkAccessBlocked):
            await bot.get_me()
    finally:
        await bot.session.close()

    assert wire.reached == []
    [attempt] = network_guard.take_attempts()
    assert "api.telegram.org" in attempt
