"""The assistant on a reasoning model (OPENAI_REASONING_EFFORT), end to end and offline.

What is pinned, in three layers:

  * the request shape: `reasoning_effort` and `max_completion_tokens` and NO `temperature`
    or `max_tokens` when an effort is set (OpenAI's GPT-6 migration guide: remove the
    sampling parameters whenever the effort is not "none"); the old shape, byte for byte,
    when it is not; and that no tools are ever sent (Chat Completions takes function
    calling on GPT-6 Luna only with the effort "none", and this application uses none);
  * the configuration that produces it, including the refusal to start a GPT-6 model
    without a chosen effort;
  * the product scenarios through the real application (HTTP route, turn runner, the real
    OpenAI backend, a database), with the SDK's client replaced by a fake that answers
    from a script. A mock proves that the right messages reach the model and that its
    answer reaches the person unchanged and in the right place; it cannot prove that a
    real model's answer is good. That is the live check in deploy/RUNBOOK.md, section 8.6.

No socket is opened and no key is real.
"""

import logging
import typing
from types import SimpleNamespace

import httpx
import openai
import pytest

from house_helper.core.kitchen_prompt import KITCHEN_SYSTEM_PROMPT
from house_helper.core.openai_chat import StandaloneOpenAIBackend
from house_helper.core.provider_settings import (
    REASONING_EFFORTS,
    ProviderSettings,
    completion_params,
    read_provider_settings,
)
from house_helper.web.settings import WebConfigError, load_provider_settings
from tests.chat_support import chat_app, error_of, log_text
from tests.test_openai_chat import CONVERSATION, MESSAGES, FakeClient, complete, response

KEY = {"OPENAI_API_KEY": "sk-test-key"}
LUNA = {**KEY, "OPENAI_MODEL": "gpt-6-luna", "OPENAI_REASONING_EFFORT": "low"}

REASONING = ProviderSettings(
    api_key="sk-test-key",
    model="gpt-6-luna",
    temperature=0.7,
    max_tokens=6000,
    timeout_seconds=50.0,
    max_retries=1,
    reasoning_effort="low",
)


def read(env, **options):
    errors: list[str] = []
    return read_provider_settings(env, errors, **options), errors


# --- the request shape --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reasoning_request_has_the_effort_and_the_completion_cap_and_no_sampling():
    client = FakeClient(response())

    await complete(StandaloneOpenAIBackend(REASONING, client=client))

    (call,) = client.calls
    assert call == {
        "model": "gpt-6-luna",
        "messages": [{"role": "system", "content": KITCHEN_SYSTEM_PROMPT}, *MESSAGES],
        "reasoning_effort": "low",
        "max_completion_tokens": 6000,
    }
    for forbidden in ("temperature", "max_tokens", "top_p", "tools", "tool_choice", "functions", "logprobs"):
        assert forbidden not in call, forbidden


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_no_reasoning_effort_other_than_none_is_sent_with_temperature_or_max_tokens(effort):
    params = completion_params(effort, 0.7, 1234)

    assert params == {"reasoning_effort": effort, "max_completion_tokens": 1234}


def test_with_the_effort_none_the_temperature_is_allowed_and_the_cap_is_still_the_completion_cap():
    assert completion_params("none", 0.4, 900) == {
        "reasoning_effort": "none",
        "max_completion_tokens": 900,
        "temperature": 0.4,
    }


def test_without_an_effort_the_request_is_the_one_every_earlier_model_takes():
    assert completion_params(None, 0.7, 1000) == {"temperature": 0.7, "max_tokens": 1000}
    assert ProviderSettings(api_key="k").completion_params() == {"temperature": 0.7, "max_tokens": 1000}


@pytest.mark.asyncio
async def test_the_reasoning_request_is_logged_by_its_own_names_and_never_with_the_text(caplog):
    caplog.set_level(logging.DEBUG)

    await complete(
        StandaloneOpenAIBackend(REASONING, client=FakeClient()),
        [{"role": "user", "content": "ЛИЧНЫЙ ВОПРОС"}],
    )

    lines = [r.getMessage() for r in caplog.records if r.name == "house_helper.core.openai_chat"]
    assert (
        "OpenAI request: model=gpt-6-luna, reasoning_effort=low, max_completion_tokens=6000, "
        f"conversation=web:{CONVERSATION.subject}, context_len=1"
    ) in lines
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert "ЛИЧНЫЙ" not in everything and "sk-test-key" not in everything


# --- the reasoning tokens ----------------------------------------------------------------------------


def with_reasoning_tokens(count, **options):
    outcome = response(**options)
    outcome.usage.completion_tokens_details = SimpleNamespace(reasoning_tokens=count)
    return outcome


@pytest.mark.asyncio
async def test_the_reasoning_tokens_are_reported_apart_and_logged(caplog):
    caplog.set_level(logging.DEBUG)

    reply = await complete(StandaloneOpenAIBackend(REASONING, client=FakeClient(with_reasoning_tokens(412))))

    assert reply.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "reasoning_tokens": 412}
    assert "OpenAI usage: input_tokens=10, output_tokens=5, total_tokens=15, reasoning_tokens=412" in caplog.text


@pytest.mark.parametrize("details", [None, SimpleNamespace(reasoning_tokens=None), SimpleNamespace(reasoning_tokens="many")])
@pytest.mark.asyncio
async def test_missing_or_malformed_reasoning_tokens_change_nothing(details):
    outcome = response()
    outcome.usage.completion_tokens_details = details

    reply = await complete(StandaloneOpenAIBackend(REASONING, client=FakeClient(outcome)))

    assert reply.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


@pytest.mark.asyncio
async def test_an_answer_the_reasoning_spent_the_whole_cap_on_is_an_empty_response_and_the_log_says_why(caplog):
    from house_helper.core.backend import BackendEmptyResponseError

    caplog.set_level(logging.DEBUG)
    spent = with_reasoning_tokens(6000, content=None, finish_reason="length")

    with pytest.raises(BackendEmptyResponseError):
        await complete(StandaloneOpenAIBackend(REASONING, client=FakeClient(spent)))

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [
        f"OpenAI answer is empty and hit the length cap for conversation=web:{CONVERSATION.subject} "
        "(the cap is spent before the first word when the model reasons; reasoning_tokens=6000)"
    ]


@pytest.mark.asyncio
async def test_an_empty_answer_that_was_not_cut_off_keeps_its_old_log_line(caplog):
    from house_helper.core.backend import BackendEmptyResponseError

    caplog.set_level(logging.DEBUG)

    with pytest.raises(BackendEmptyResponseError):
        await complete(StandaloneOpenAIBackend(REASONING, client=FakeClient(response(content=""))))

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [f"OpenAI response has empty or invalid content for conversation=web:{CONVERSATION.subject}"]


# --- what a failure logs: the provider's code, never its text -----------------------------------------


def failure(error_class, status, *, code=None, param=None, message="provider text"):
    body = {"code": code, "param": param, "message": message, "type": "invalid_request_error"}
    return error_class(
        message,
        response=httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
        body=body,
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (failure(openai.BadRequestError, 400, code="unsupported_parameter", param="max_tokens"),
         "(BadRequestError, status=400, code=unsupported_parameter, param=max_tokens)"),
        (failure(openai.RateLimitError, 429, code="insufficient_quota"),
         "(RateLimitError, status=429, code=insufficient_quota)"),
        (failure(openai.NotFoundError, 404, code="model_not_found"),
         "(NotFoundError, status=404, code=model_not_found)"),
        (failure(openai.AuthenticationError, 401, code="invalid_api_key"),
         "(AuthenticationError, status=401, code=invalid_api_key)"),
    ],
)
@pytest.mark.asyncio
async def test_a_failure_names_the_providers_machine_code_and_parameter(caplog, error, expected):
    from house_helper.core.backend import BackendError

    caplog.set_level(logging.DEBUG)

    with pytest.raises(BackendError):
        await complete(StandaloneOpenAIBackend(REASONING, client=FakeClient(error)))

    assert any(expected in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "hostile",
    [
        "Incorrect API key provided: sk-proj-SECRETKEY123",  # mixed case: a key
        "invalid key sk-proj",  # a sentence
        "x" * 65,  # too long to be an identifier
        "messages[0].content",  # a path into the request
        "",
    ],
)
@pytest.mark.asyncio
async def test_a_code_or_parameter_that_is_not_a_plain_identifier_is_not_logged(caplog, hostile):
    from house_helper.core.backend import BackendError

    caplog.set_level(logging.DEBUG)
    error = failure(openai.BadRequestError, 400, code=hostile, param=hostile)

    with pytest.raises(BackendError):
        await complete(StandaloneOpenAIBackend(REASONING, client=FakeClient(error)))

    line = next(r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    assert line.endswith("(BadRequestError, status=400)")
    assert "SECRETKEY" not in caplog.text and "sk-proj" not in caplog.text


# --- the configuration -------------------------------------------------------------------------------


def test_without_the_variable_there_is_no_effort_and_the_old_defaults_hold():
    settings, errors = read(KEY)

    assert errors == [] and settings.reasoning_effort is None


@pytest.mark.parametrize("effort", REASONING_EFFORTS)
def test_every_documented_effort_is_accepted(effort):
    settings, errors = read({**KEY, "OPENAI_REASONING_EFFORT": effort})

    assert errors == [] and settings.reasoning_effort == effort


def test_the_efforts_are_the_ones_the_model_page_and_the_sdk_both_list():
    assert REASONING_EFFORTS == ("none", "low", "medium", "high", "xhigh")
    from openai.types.shared.reasoning_effort import ReasoningEffort

    sdk_values = set(typing.get_args(typing.get_args(ReasoningEffort)[0]))
    assert set(REASONING_EFFORTS) <= sdk_values


def test_a_blank_variable_counts_as_unset():
    settings, errors = read({**KEY, "OPENAI_REASONING_EFFORT": ""})

    assert errors == [] and settings.reasoning_effort is None


@pytest.mark.parametrize("bad", ["minimal", "max", "LOW", "low ", "fast", "0"])
def test_any_other_effort_is_refused_naming_the_variable(bad):
    settings, errors = read({**KEY, "OPENAI_REASONING_EFFORT": bad})

    assert settings is None
    assert errors == [f"OPENAI_REASONING_EFFORT must be one of none, low, medium, high, xhigh, got {bad!r}"]


def test_the_web_process_never_quotes_the_value():
    with pytest.raises(WebConfigError) as raised:
        load_provider_settings({**KEY, "OPENAI_REASONING_EFFORT": "SECRET-LOOKING-VALUE"})

    assert "OPENAI_REASONING_EFFORT must be one of" in str(raised.value)
    assert "SECRET-LOOKING-VALUE" not in str(raised.value)


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"])
def test_a_gpt_6_model_without_a_chosen_effort_does_not_start(model):
    settings, errors = read({**KEY, "OPENAI_MODEL": model})

    assert settings is None
    assert len(errors) == 1 and errors[0].startswith("OPENAI_REASONING_EFFORT must be set")
    with pytest.raises(WebConfigError, match="OPENAI_REASONING_EFFORT must be set"):
        load_provider_settings({**KEY, "OPENAI_MODEL": model})


def test_the_production_configuration_for_luna_loads():
    env = {
        **LUNA,
        "MAX_TOKENS": "6000",
        "OPENAI_TIMEOUT_SECONDS": "50",
        "OPENAI_MAX_RETRIES": "1",
    }

    settings = load_provider_settings(env)

    assert settings == ProviderSettings(
        api_key="sk-test-key",
        model="gpt-6-luna",
        temperature=0.7,
        max_tokens=6000,
        timeout_seconds=50.0,
        max_retries=1,
        reasoning_effort="low",
    )
    assert settings.completion_params() == {"reasoning_effort": "low", "max_completion_tokens": 6000}
    assert "sk-test-key" not in repr(settings)


def test_the_telegram_bot_reads_the_same_effort():
    import config

    settings = config.load_settings({"BOT_TOKEN": "t", **LUNA})

    assert (settings.openai_model, settings.openai_reasoning_effort) == ("gpt-6-luna", "low")
    assert config.load_settings({"BOT_TOKEN": "t", **KEY}).openai_reasoning_effort is None
    with pytest.raises(config.ConfigError, match="OPENAI_REASONING_EFFORT must be set"):
        config.load_settings({"BOT_TOKEN": "t", **KEY, "OPENAI_MODEL": "gpt-6-luna"})


@pytest.mark.asyncio
async def test_the_telegram_provider_layer_sends_the_same_shape(monkeypatch):
    import api_client
    import config

    client = FakeClient(response("Сварите яйца."))
    monkeypatch.setattr(config, "_settings", config.load_settings({"BOT_TOKEN": "t", **LUNA, "MAX_TOKENS": "6000"}))
    monkeypatch.setattr(api_client, "_client", client)

    text, _ = await api_client.get_chat_response(MESSAGES, user_id="telegram:7", context_len=1)

    assert text == "Сварите яйца."
    (call,) = client.calls
    assert call["reasoning_effort"] == "low" and call["max_completion_tokens"] == 6000
    assert "temperature" not in call and "max_tokens" not in call


# --- the scenarios, through the real application -------------------------------------------------------


class Sdk(list):
    """The clients the application built (a list), and what the next one will answer."""

    outcome = None


@pytest.fixture
def sdk(monkeypatch):
    """The SDK's client class, replaced: every client built is kept."""
    built = Sdk()

    def build(**kwargs):
        client = FakeClient(built.outcome if built.outcome is not None else response("Ответ."), **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", build)
    return built


SCENARIOS = [
    pytest.param(
        "У меня есть курица, рис и морковь. Что можно приготовить?",
        "Из этого получится плов с курицей или куриный суп с рисом. Для плова на 4 порции нужно "
        "примерно 500 г курицы, 300 г риса и 2 моркови; готовьте около 40 минут. Какой вариант "
        "вам ближе?",
        id="ingredients-to-dishes",
    ),
    pytest.param(
        "Как приготовить борщ?",
        "Борщ на 6 порций. Ингредиенты: говядина на кости 500 г, свёкла 2 шт., капуста 300 г, "
        "картофель 3 шт., морковь 1 шт., лук 1 шт., томатная паста 2 ст. л. Шаги: 1. Варите мясо "
        "около 1,5 часа. 2. Обжарьте овощи. 3. Добавьте капусту и картофель на 20 минут.",
        id="specific-dish",
    ),
    pytest.param(
        "Не знаю, что приготовить на ужин",
        "Могу предложить три простых варианта: омлет с овощами, макароны с сыром или запечённую "
        "курицу с картофелем. Сколько человек будет за столом?",
        id="meal-ideas-with-one-question",
    ),
    pytest.param(
        "Чем заменить сметану в соусе?",
        "Можно взять густой натуральный йогурт в том же количестве. Вкус будет чуть кислее; "
        "нагревайте на слабом огне, чтобы он не свернулся.",
        id="substitution",
    ),
    pytest.param(
        "Какие специи любят ваши кошки?",
        "Я помогаю только с готовкой и едой. Спросите меня про блюдо или продукты.",
        id="off-topic-declined-in-russian",
    ),
]


def cyrillic_share(text):
    letters = [c for c in text if c.isalpha()]
    return sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in letters) / len(letters)


@pytest.mark.parametrize(("question", "answer"), SCENARIOS)
@pytest.mark.asyncio
async def test_a_question_reaches_the_model_in_the_reasoning_shape_and_the_answer_comes_back_whole(
    tmp_path, sdk, question, answer
):
    async with chat_app(tmp_path, None, provider=REASONING) as chat:
        conversation = await chat.create()
        sdk.outcome = response(answer)  # what the client the first message builds will say

        reply = await chat.send(conversation, question, "k-1")

        assert reply.status_code == 201
        assert reply.json()["assistant_message"]["text"] == answer  # unchanged, not trimmed or marked
        assert cyrillic_share(reply.json()["assistant_message"]["text"]) > 0.8
        (call,) = sdk[0].calls
        assert call["messages"][0] == {"role": "system", "content": KITCHEN_SYSTEM_PROMPT}
        assert call["messages"][-1] == {"role": "user", "content": question}
        assert call["reasoning_effort"] == "low" and call["max_completion_tokens"] == 6000
        assert not {"temperature", "max_tokens", "tools"} & set(call)
        assert [m.content for m in chat.messages(conversation)] == [question, answer]


@pytest.mark.asyncio
async def test_a_follow_up_about_servings_is_answered_with_the_whole_conversation_in_order(tmp_path, sdk):
    async with chat_app(tmp_path, None, provider=REASONING) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "Как приготовить омлет на двоих?", "k-1")
        sdk[0].outcome = response("На шестерых возьмите 12 яиц и 300 мл молока; жарьте 8–10 минут.")

        reply = await chat.send(conversation, "А если на шестерых?", "k-2")

        assert reply.status_code == 201
        assert sdk[0].calls[-1]["messages"][1:] == [
            {"role": "user", "content": "Как приготовить омлет на двоих?"},
            {"role": "assistant", "content": "Ответ."},
            {"role": "user", "content": "А если на шестерых?"},
        ]


@pytest.mark.asyncio
async def test_an_answer_cut_off_at_the_cap_is_said_to_be_cut_off(tmp_path, sdk):
    async with chat_app(tmp_path, None, provider=REASONING) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "привет", "k-0")
        sdk[0].outcome = response("1. Нарежьте лук. 2. Обжарьте", finish_reason="length")

        reply = await chat.send(conversation, "рецепт супа", "k-1")

        assert reply.status_code == 201
        text = reply.json()["assistant_message"]["text"]
        assert text.startswith("1. Нарежьте лук. 2. Обжарьте") and "оборвался" in text and "продолжи" in text


@pytest.mark.asyncio
async def test_two_people_never_see_each_others_questions_answers_or_conversations(tmp_path, sdk):
    mine, theirs = "СЕКРЕТ-ВЛАДЕЛЬЦА яйца и сыр", "СЕКРЕТ-МАМЫ гречка и грибы"
    async with chat_app(tmp_path, None, provider=REASONING) as chat:
        other = await chat.second_user()
        my_conversation = await chat.create()
        their_conversation = await chat.create(other)

        await chat.send(my_conversation, mine, "a-1")
        sdk[0].outcome = response("Ответ маме: гречка с грибами.")
        await chat.send(their_conversation, theirs, "b-1", client=other)
        await chat.send(my_conversation, "а без сыра?", "a-2")

        first, second, third = sdk[0].calls
        assert theirs not in repr(first) and mine not in repr(second)
        assert theirs not in repr(third)  # my second request holds my history only
        assert mine in repr(third) and "Ответ маме" not in repr(third)

        # and neither can reach into the other's conversation, to read or to write
        before = len(sdk[0].calls)
        stranger_read = await chat.history(my_conversation, client=other)
        stranger_write = await chat.send(my_conversation, "чужое сообщение", "b-2", client=other)
        assert stranger_read.status_code == stranger_write.status_code == 404
        assert len(sdk[0].calls) == before  # the model was not asked
        assert mine not in stranger_read.text + stranger_write.text


FAILURES = [
    pytest.param(
        lambda: failure(openai.RateLimitError, 429, code="insufficient_quota"),
        503, "provider_rate_limited", "insufficient_quota", id="quota-or-rate-limit",
    ),
    pytest.param(
        # What the hard spend limit answers (deploy/RUNBOOK.md, section 4.2). The person sees the
        # rate-limit message; the log says which limit it was.
        lambda: failure(openai.RateLimitError, 429, code="project_spend_limit_exceeded"),
        503, "provider_rate_limited", "project_spend_limit_exceeded", id="hard-spend-limit",
    ),
    pytest.param(
        lambda: openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
        502, "provider_unreachable", None, id="timeout",
    ),
    pytest.param(
        lambda: openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
        502, "provider_unreachable", None, id="connection",
    ),
    pytest.param(
        lambda: failure(openai.BadRequestError, 400, code="unsupported_parameter", param="temperature"),
        502, "provider_error", "unsupported_parameter", id="unsupported-parameter",
    ),
    pytest.param(
        lambda: failure(openai.NotFoundError, 404, code="model_not_found"),
        502, "provider_error", "model_not_found", id="model-not-found",
    ),
    pytest.param(
        lambda: failure(openai.AuthenticationError, 401, code="invalid_api_key"),
        502, "provider_error", "invalid_api_key", id="bad-key",
    ),
    pytest.param(
        lambda: failure(openai.InternalServerError, 500),
        502, "provider_error", None, id="server-error",
    ),
]


@pytest.mark.parametrize(("make_error", "status", "code", "logged_code"), FAILURES)
@pytest.mark.asyncio
async def test_a_provider_failure_is_a_russian_catalogue_error_that_leaks_nothing(
    tmp_path, sdk, caplog, make_error, status, code, logged_code
):
    caplog.set_level(logging.DEBUG)
    leaky = "Incorrect API key provided: sk-proj-SECRETKEY123. Prompt: СЕКРЕТНЫЙ ВОПРОС " + KITCHEN_SYSTEM_PROMPT[:40]
    async with chat_app(tmp_path, None, provider=REASONING) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "привет", "k-0")
        error = make_error()
        error.message = leaky
        sdk[0].outcome = error

        failed = await chat.send(conversation, "СЕКРЕТНЫЙ ВОПРОС", "k-1")

        assert failed.status_code == status
        body = error_of(failed)
        assert body["detail"] == code and body["retryable"] is True
        assert cyrillic_share(body["message"]) > 0.8
        everything = failed.text + log_text(caplog)
        for secret in ("SECRETKEY", "sk-proj", "sk-test-key", KITCHEN_SYSTEM_PROMPT[:40], "Incorrect API key"):
            assert secret not in everything, secret
        assert "СЕКРЕТНЫЙ" not in failed.text and "СЕКРЕТНЫЙ" not in log_text(caplog)
        if logged_code is not None:
            assert f"code={logged_code}" in log_text(caplog)


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(response(content=None, finish_reason="length"), id="reasoning-spent-the-cap"),
        pytest.param(response(content="   "), id="blank"),
        pytest.param(response(choices=False), id="no-choices"),
    ],
)
@pytest.mark.asyncio
async def test_an_empty_answer_is_a_retryable_error_not_an_empty_message(tmp_path, sdk, outcome):
    async with chat_app(tmp_path, None, provider=REASONING) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "привет", "k-0")
        sdk[0].outcome = outcome

        failed = await chat.send(conversation, "что из яиц?", "k-1")

        assert failed.status_code == 502 and error_of(failed)["detail"] == "provider_empty_response"
        assert [m.content for m in chat.messages(conversation)][-1] == "что из яиц?"  # no empty assistant message
        sdk[0].outcome = response("Омлет.")
        retried = await chat.send(conversation, "что из яиц?", "k-1")  # the same key tries again
        assert retried.status_code == 201 and retried.json()["assistant_message"]["text"] == "Омлет."
