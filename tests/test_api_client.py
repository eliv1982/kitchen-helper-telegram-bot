from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from openai import AsyncOpenAI

import api_client
import config
from tests.helpers import make_openai_response


@pytest.mark.asyncio
async def test_get_chat_response_awaits_async_client_without_network():
    fake_response = make_openai_response("Свари яйца всмятку.")

    with patch.object(
        api_client._client.chat.completions,
        "create",
        new=AsyncMock(return_value=fake_response),
    ) as mock_create:
        content, usage = await api_client.get_chat_response(
            [{"role": "user", "content": "У меня есть яйца"}],
            user_id=1,
            context_len=1,
        )

    mock_create.assert_awaited_once()
    _, kwargs = mock_create.call_args
    assert kwargs["messages"][0] == {"role": "system", "content": api_client.SYSTEM_MESSAGE}
    assert kwargs["messages"][-1] == {"role": "user", "content": "У меня есть яйца"}
    assert content == "Свари яйца всмятку."
    assert usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


@pytest.mark.asyncio
async def test_empty_choices_raises_invalid_response_error():
    empty_response = SimpleNamespace(choices=[], usage=None)

    with patch.object(
        api_client._client.chat.completions,
        "create",
        new=AsyncMock(return_value=empty_response),
    ):
        with pytest.raises(api_client.InvalidResponseError):
            await api_client.get_chat_response(
                [{"role": "user", "content": "hi"}], user_id=1, context_len=1
            )


@pytest.mark.asyncio
async def test_blank_content_raises_invalid_response_error():
    blank_response = make_openai_response(content="", usage=None)

    with patch.object(
        api_client._client.chat.completions,
        "create",
        new=AsyncMock(return_value=blank_response),
    ):
        with pytest.raises(api_client.InvalidResponseError):
            await api_client.get_chat_response(
                [{"role": "user", "content": "hi"}], user_id=1, context_len=1
            )


@pytest.mark.asyncio
async def test_missing_message_content_raises_invalid_response_error():
    response_without_content = SimpleNamespace(
        choices=[SimpleNamespace(message=None)], usage=None
    )

    with patch.object(
        api_client._client.chat.completions,
        "create",
        new=AsyncMock(return_value=response_without_content),
    ):
        with pytest.raises(api_client.InvalidResponseError):
            await api_client.get_chat_response(
                [{"role": "user", "content": "hi"}], user_id=1, context_len=1
            )


@pytest.mark.asyncio
async def test_whitespace_only_content_raises_invalid_response_error():
    whitespace_response = make_openai_response(content="   \n\t  ", usage=None)

    with patch.object(
        api_client._client.chat.completions,
        "create",
        new=AsyncMock(return_value=whitespace_response),
    ):
        with pytest.raises(api_client.InvalidResponseError):
            await api_client.get_chat_response(
                [{"role": "user", "content": "hi"}], user_id=1, context_len=1
            )


@pytest.mark.asyncio
async def test_non_string_content_raises_invalid_response_error():
    non_string_response = make_openai_response(content=["not", "a", "string"], usage=None)

    with patch.object(
        api_client._client.chat.completions,
        "create",
        new=AsyncMock(return_value=non_string_response),
    ):
        with pytest.raises(api_client.InvalidResponseError):
            await api_client.get_chat_response(
                [{"role": "user", "content": "hi"}], user_id=1, context_len=1
            )


@pytest.mark.asyncio
async def test_valid_content_with_missing_usage_attribute_still_succeeds():
    # SimpleNamespace built without a `usage` kwarg at all -- unlike the
    # existing "usage=None" cases, accessing `.usage` on this object would
    # raise AttributeError if the extraction code assumed the attribute
    # always exists.
    response_without_usage_attr = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Свари яйца."))]
    )

    with patch.object(
        api_client._client.chat.completions,
        "create",
        new=AsyncMock(return_value=response_without_usage_attr),
    ):
        content, usage = await api_client.get_chat_response(
            [{"role": "user", "content": "hi"}], user_id=1, context_len=1
        )

    assert content == "Свари яйца."
    assert usage is None


@pytest.mark.asyncio
async def test_valid_content_with_malformed_usage_degrades_to_none():
    # `usage` is present but missing the expected numeric fields entirely.
    malformed_usage_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Свари яйца."))],
        usage=SimpleNamespace(),
    )

    with patch.object(
        api_client._client.chat.completions,
        "create",
        new=AsyncMock(return_value=malformed_usage_response),
    ):
        content, usage = await api_client.get_chat_response(
            [{"role": "user", "content": "hi"}], user_id=1, context_len=1
        )

    assert content == "Свари яйца."
    assert usage is None


@pytest.mark.asyncio
async def test_valid_content_with_partial_usage_degrades_to_none():
    # `usage` is present but only partially populated (missing total_tokens).
    partial_usage_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Свари яйца."))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )

    with patch.object(
        api_client._client.chat.completions,
        "create",
        new=AsyncMock(return_value=partial_usage_response),
    ):
        content, usage = await api_client.get_chat_response(
            [{"role": "user", "content": "hi"}], user_id=1, context_len=1
        )

    assert content == "Свари яйца."
    assert usage is None


def test_openai_timeout_and_retries_are_explicit_and_bounded():
    # A throwaway client with no explicit timeout shows the SDK default
    # (10 minutes read timeout) that this stage deliberately avoids relying on.
    default_client = AsyncOpenAI(api_key="unused-default-comparison-key")

    assert api_client._client.timeout == config.OPENAI_TIMEOUT_SECONDS
    assert api_client._client.timeout != default_client.timeout
    assert api_client._client.timeout <= 30

    assert api_client._client.max_retries == config.OPENAI_MAX_RETRIES
    assert api_client._client.max_retries <= 2
