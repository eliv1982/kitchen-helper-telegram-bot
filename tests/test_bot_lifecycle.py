from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import api_client
import bot as bot_module


def _install_fake_bot(monkeypatch) -> AsyncMock:
    """Replace bot_module.Bot with a fake constructor returning an object
    whose session.close() is an AsyncMock, and return that mock so tests can
    assert on it. Avoids constructing a real aiogram Bot / HTTP session."""
    session_close = AsyncMock()
    fake_bot_instance = SimpleNamespace(session=SimpleNamespace(close=session_close))
    monkeypatch.setattr(bot_module, "Bot", Mock(return_value=fake_bot_instance))
    return session_close


@pytest.mark.asyncio
async def test_main_closes_openai_client_after_normal_polling_exit(monkeypatch):
    session_close = _install_fake_bot(monkeypatch)

    start_polling = AsyncMock(return_value=None)
    monkeypatch.setattr(bot_module.dp, "start_polling", start_polling)

    close_client = AsyncMock()
    monkeypatch.setattr(api_client, "close_client", close_client)

    await bot_module.main()

    start_polling.assert_awaited_once()
    session_close.assert_awaited_once()
    close_client.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_closes_openai_client_when_polling_fails(monkeypatch):
    session_close = _install_fake_bot(monkeypatch)

    start_polling = AsyncMock(side_effect=RuntimeError("polling failed"))
    monkeypatch.setattr(bot_module.dp, "start_polling", start_polling)

    close_client = AsyncMock()
    monkeypatch.setattr(api_client, "close_client", close_client)

    # Cleanup must still run even though main() re-raises the polling
    # failure (it only has a try/finally, no except).
    with pytest.raises(RuntimeError, match="polling failed"):
        await bot_module.main()

    session_close.assert_awaited_once()
    close_client.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_exits_before_polling_when_config_is_invalid(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)

    fake_bot_class = Mock()
    monkeypatch.setattr(bot_module, "Bot", fake_bot_class)

    start_polling = AsyncMock()
    monkeypatch.setattr(bot_module.dp, "start_polling", start_polling)

    close_client = AsyncMock()
    monkeypatch.setattr(api_client, "close_client", close_client)

    with pytest.raises(SystemExit):
        await bot_module.main()

    fake_bot_class.assert_not_called()
    start_polling.assert_not_called()
    close_client.assert_not_called()
