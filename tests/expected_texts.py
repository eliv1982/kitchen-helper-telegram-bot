"""Literal copies of every user-facing outcome message, as shipped today.

Deliberately NOT imported from production code: when these strings move into
a dedicated catalog (decision D3) the tests must keep comparing against these
independent literals, so any accidental wording change fails loudly.
"""

START = (
    "Привет! Я Kitchen Helper — помогу придумать простое блюдо из того, "
    "что есть дома.\n\n"
    "Напиши, какие продукты у тебя есть, и я предложу варианты.\n\n"
    "Обрати внимание: текст твоих сообщений передаётся во внешний сервис "
    "OpenAI для обработки и генерации ответа.\n\n"
    "Команды:\n"
    "/reset — очистить историю диалога\n"
    "или напиши «очистить контекст»"
)

RESET_CONFIRMATION = "Контекст очищен. Можем начать с чистого листа!"

# Rendered with the default limit of 2000 characters.
TOO_LONG_2000 = (
    "Сообщение слишком длинное (максимум 2000 символов). "
    "Сократи запрос и отправь снова."
)

# Rendered with the limit patched down to 10 characters.
TOO_LONG_10 = (
    "Сообщение слишком длинное (максимум 10 символов). "
    "Сократи запрос и отправь снова."
)

RATE_LIMITED = "Сейчас слишком много запросов к AI. Подожди немного и попробуй снова."

CONNECTION_FAILED = "Не удалось связаться с OpenAI. Проверь интернет и попробуй позже."

EMPTY_RESPONSE = (
    "AI вернул пустой ответ. Попробуй переформулировать запрос или повторить позже."
)

API_ERROR = "Ошибка при обращении к OpenAI. Попробуй позже или проверь настройки API."

DELIVERY_FAILED = "Не удалось отправить ответ. Попробуй ещё раз."
