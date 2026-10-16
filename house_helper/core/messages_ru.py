"""Russian texts for application-level chat outcomes.

The one place that decides what the user is told when a turn ends. Plain
constants and one lookup function: there is a single language, so there is
nothing to select and no framework.

Not here, because they belong to the Telegram adapter: the /start greeting and
the notice sent when a reply could not be delivered.
"""

from house_helper.core.outcomes import ChatOutcome, OutcomeStatus

CONTEXT_CLEARED = "Контекст очищен. Можем начать с чистого листа!"

INPUT_TOO_LONG = (
    "Сообщение слишком длинное (максимум {max_input_length} символов). "
    "Сократи запрос и отправь снова."
)

RATE_LIMITED = "Сейчас слишком много запросов к AI. Подожди немного и попробуй снова."

CONNECTION_FAILED = "Не удалось связаться с OpenAI. Проверь интернет и попробуй позже."

EMPTY_RESPONSE = (
    "AI вернул пустой ответ. Попробуй переформулировать запрос или повторить позже."
)

API_ERROR = "Ошибка при обращении к OpenAI. Попробуй позже или проверь настройки API."

_FIXED_TEXTS = {
    OutcomeStatus.CONTEXT_CLEARED: CONTEXT_CLEARED,
    OutcomeStatus.RATE_LIMITED: RATE_LIMITED,
    OutcomeStatus.CONNECTION_FAILED: CONNECTION_FAILED,
    OutcomeStatus.EMPTY_RESPONSE: EMPTY_RESPONSE,
    OutcomeStatus.API_ERROR: API_ERROR,
}


def render(outcome: ChatOutcome) -> str:
    """The text to show the user for an outcome.

    A reply is the assistant's own text and is returned untouched; every other
    status has a fixed wording.
    """
    if outcome.status is OutcomeStatus.REPLIED:
        return outcome.reply
    if outcome.status is OutcomeStatus.INPUT_TOO_LONG:
        return INPUT_TOO_LONG.format(max_input_length=outcome.max_input_length)
    return _FIXED_TEXTS[outcome.status]
