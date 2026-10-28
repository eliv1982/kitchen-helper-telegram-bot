"""What the chat API says when something goes wrong, and how it is traced.

Every error the chat API answers is one of the entries of CATALOGUE: a stable code
(the client's contract: it never changes wording), the HTTP status, a message in
Russian that is fixed text (nothing from the request, from an exception or from the
provider is ever put into it), and whether trying the same thing again can help. The
body is

    {"detail": <code>, "message": <Russian text>, "retryable": <bool>,
     "correlation_id": <id>}

(`detail` as in the other endpoints of this application), plus `retry_after_seconds`
where waiting is the advice, and `user_message_id` for a failure after the message was
stored, so the page can mark it and offer to send it again.

Correlation ids. Every request to the chat API gets a new id from the SERVER
(new_correlation_id): 64 random bits, "hh-" and 16 hex digits, short enough to read
off a screenshot. It is in the response header X-Correlation-ID, in the body of every
error, and in the log line that describes the error, so a person who reports "hh-..."
can be matched to what the server saw. A value the client sends (an X-Correlation-ID
header of its own, or anything else) is never read: a client-chosen id could collide
with, or impersonate, another request's.

The log line (log_error) holds the id, the code, the HTTP status, the route (the
template, not the URL: no identifier in it) and a diagnostic category: the NAME of an
exception class, or a fixed word. Never an exception's message (SQLAlchemy's and the
provider's contain statement parameters, text of the request and credentials), never
a traceback, never what the user wrote or the assistant answered.
"""

import logging
import secrets
from dataclasses import dataclass
from typing import Any, Optional

from fastapi.responses import JSONResponse

CORRELATION_HEADER = "X-Correlation-ID"

logger = logging.getLogger("house_helper.web.chat")


def new_correlation_id() -> str:
    return "hh-" + secrets.token_hex(8)


@dataclass(frozen=True)
class ErrorSpec:
    status: int
    message: str
    retryable: bool


def _spec(status: int, message: str, retryable: bool = False) -> ErrorSpec:
    return ErrorSpec(status, message, retryable)


CATALOGUE: dict[str, ErrorSpec] = {
    # The request itself
    "invalid_request": _spec(400, "Запрос не удалось разобрать."),
    "unsupported_media_type": _spec(415, "Запрос должен быть в формате JSON."),
    "request_too_large": _spec(413, "Запрос слишком большой."),
    "forbidden_origin": _spec(403, "Запрос отклонён: он пришёл не с нашего сайта."),
    "idempotency_key_required": _spec(
        400, "Не удалось отправить сообщение: у запроса нет ключа. Обновите страницу и попробуйте ещё раз."
    ),
    "invalid_idempotency_key": _spec(
        400, "Не удалось отправить сообщение: ключ запроса некорректен. Обновите страницу и попробуйте ещё раз."
    ),
    "empty_message": _spec(400, "Сообщение не должно быть пустым."),
    "invalid_message_text": _spec(400, "В сообщении есть недопустимые символы."),
    "message_too_long": _spec(
        400, "Сообщение слишком длинное (максимум {max_length} символов). Сократите его и отправьте снова."
    ),
    "invalid_query": _spec(400, "Параметры запроса указаны неверно."),
    # Who and what
    "not_authenticated": _spec(401, "Войдите в аккаунт, чтобы продолжить."),
    "conversation_not_found": _spec(404, "Разговор не найден."),
    "message_not_found": _spec(404, "Этот ответ не найден: возможно, он уже удалён."),
    "favorite_not_found": _spec(404, "Этого рецепта нет в избранном."),
    "favorites_full": _spec(
        409, "В избранном набралось максимальное число рецептов. Уберите ненужные, чтобы добавить новый."
    ),
    # Idempotency and concurrency
    "idempotency_key_reused": _spec(
        409, "Этот ключ запроса уже использован для другого сообщения. Отправьте сообщение заново."
    ),
    "conversation_busy": _spec(
        409,
        "В этом разговоре ещё готовится предыдущий ответ. Подождите немного и повторите.",
        retryable=True,
    ),
    "turn_in_progress": _spec(409, "Ответ на это сообщение ещё готовится. Подождите немного.", retryable=True),
    "turn_superseded": _spec(
        409,
        "Это сообщение уже нельзя повторить: в разговоре появились новые. Отправьте его заново как новое.",
    ),
    "idempotency_key_unusable": _spec(
        409, "Этот запрос больше нельзя повторить. Отправьте сообщение заново."
    ),
    # The assistant
    "provider_rate_limited": _spec(
        503, "Сейчас слишком много запросов к помощнику. Подождите немного и попробуйте снова.", True
    ),
    "provider_unreachable": _spec(
        502, "Не удалось связаться с помощником. Попробуйте ещё раз чуть позже.", True
    ),
    "provider_timeout": _spec(504, "Помощник слишком долго готовил ответ. Попробуйте ещё раз.", True),
    "provider_empty_response": _spec(
        502,
        "Помощник вернул пустой ответ. Попробуйте переформулировать вопрос или повторить позже.",
        True,
    ),
    "provider_error": _spec(
        502, "Помощник сейчас не может ответить. Попробуйте ещё раз чуть позже.", True
    ),
    "interrupted": _spec(
        503, "Работа над ответом была прервана. Отправьте сообщение ещё раз.", True
    ),
    "chat_unavailable": _spec(503, "Помощник сейчас недоступен."),
    # The service
    "service_unavailable": _spec(
        503, "Сервис временно недоступен. Попробуйте ещё раз чуть позже.", True
    ),
    "internal_error": _spec(500, "Что-то пошло не так. Попробуйте ещё раз чуть позже.", True),
}

# Seconds, for the codes where the advice is to wait and ask again.
RETRY_AFTER_SECONDS: dict[str, int] = {
    "conversation_busy": 5,
    "turn_in_progress": 5,
    "provider_rate_limited": 30,
}


class ApiError(Exception):
    """A failure with a code of the CATALOGUE.

    `category` is the diagnostic word for the log (an exception class name or a fixed
    word); `user_message_id` is set when the user's message had been stored before the
    failure. `logged` tells the route wrapper that the failure has been written to the
    log already, with the correlation id it was given.
    """

    def __init__(
        self,
        code: str,
        *,
        category: Optional[str] = None,
        user_message_id: Optional[int] = None,
        context: Optional[dict[str, Any]] = None,
        **format_values: Any,
    ) -> None:
        super().__init__(code)
        if code not in CATALOGUE:
            raise KeyError(code)
        self.code = code
        self.category = category
        self.user_message_id = user_message_id
        self.context = context or {}
        self.format_values = format_values
        self.logged = False

    @property
    def spec(self) -> ErrorSpec:
        return CATALOGUE[self.code]

    @property
    def message(self) -> str:
        return self.spec.message.format(**self.format_values)


def error_body(error: ApiError, correlation_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "detail": error.code,
        "message": error.message,
        "retryable": error.spec.retryable,
        "correlation_id": correlation_id,
    }
    retry_after = RETRY_AFTER_SECONDS.get(error.code)
    if retry_after is not None:
        body["retry_after_seconds"] = retry_after
    if error.user_message_id is not None:
        body["user_message_id"] = error.user_message_id
    return body


def error_response(
    error: ApiError, correlation_id: str, headers: dict[str, str]
) -> JSONResponse:
    extra = dict(headers)
    retry_after = RETRY_AFTER_SECONDS.get(error.code)
    if retry_after is not None:
        extra["Retry-After"] = str(retry_after)
    return JSONResponse(
        error_body(error, correlation_id), status_code=error.spec.status, headers=extra
    )


def log_error(error: ApiError, correlation_id: str, route: str) -> None:
    """One line about a failed request; see the module docstring for what is in it."""
    spec = error.spec
    if error.code == "chat_unavailable" or error.code in _NOTEWORTHY:
        level = logging.WARNING  # a missing configuration; probing
    elif spec.status >= 500:
        level = logging.ERROR
    else:
        level = logging.INFO
    # The context holds only values the server made itself or validated (a conversation
    # id that parsed as a UUID, an attempt number): nothing the client typed.
    context = "".join(f" {name}={value}" for name, value in error.context.items())
    logger.log(
        level,
        "Chat request failed: correlation_id=%s code=%s status=%d route=%s category=%s%s",
        correlation_id,
        error.code,
        spec.status,
        route,
        error.category or "-",
        context,
    )
    error.logged = True


# 4xx answers worth a warning rather than an info line: someone tried something that
# looks like probing, not a typo.
_NOTEWORTHY = frozenset({"forbidden_origin", "idempotency_key_reused"})
