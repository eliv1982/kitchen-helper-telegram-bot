import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

import api_client
import config
from house_helper.core import messages_ru
from house_helper.core.backend import OpenAIBackend
from house_helper.core.chat_service import ChatService
from house_helper.core.identity import ConversationKey
from house_helper.core.outcomes import OutcomeStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

RESET_PHRASE = "очистить контекст"

dp = Dispatcher()

TELEGRAM_CHANNEL = "telegram"

# Answers every Telegram conversation. It keeps no conversation state of its
# own (history and per-conversation locks live in context_manager) and
# OpenAIBackend is stateless (the shared OpenAI client lives in api_client), so
# tests may simply replace the backend it answers with.
_chat_service = ChatService(OpenAIBackend())


def _conversation_key(user_id: int) -> ConversationKey:
    """The conversation a Telegram sender belongs to.

    A conversation is scoped to the sending user, not to the chat: the same
    user writing from a private chat and from a group shares one history and
    one lock. The id is stored as a str so every channel's keys have one shape.
    """
    return ConversationKey(TELEGRAM_CHANNEL, str(user_id))


def _log_context_cleared(user_id: int, source: str) -> None:
    logger.info("Context cleared via %s for user_id=%s", source, user_id)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я Kitchen Helper — помогу придумать простое блюдо из того, "
        "что есть дома.\n\n"
        "Напиши, какие продукты у тебя есть, и я предложу варианты.\n\n"
        "Обрати внимание: текст твоих сообщений передаётся во внешний сервис "
        "OpenAI для обработки и генерации ответа.\n\n"
        "Команды:\n"
        "/reset — очистить историю диалога\n"
        'или напиши «очистить контекст»'
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    # The state is reset under the conversation's lock; the lock is released
    # before the confirmation is delivered, so a slow delivery never holds up
    # the user's next message.
    outcome = await _chat_service.reset(_conversation_key(user_id), source="/reset")
    _log_context_cleared(user_id, "/reset")
    await message.answer(messages_ru.render(outcome))


@dp.message(F.text)
async def handle_text(message: Message, bot: Bot) -> None:
    if not message.text or not message.from_user:
        return

    user_id = message.from_user.id
    key = _conversation_key(user_id)
    text = message.text.strip()

    # The whole exchange -- the AI call and the delivery of whatever the user
    # is told about it -- happens inside one turn, so the next message from the
    # same sender cannot start until this one has been delivered.
    async with _chat_service.turn(key) as turn:
        if text.lower() == RESET_PHRASE:
            outcome = await turn.reset(source="reset phrase")
            _log_context_cleared(user_id, "reset phrase")
            await message.answer(messages_ru.render(outcome))
            return

        # "typing..." is shown only while the AI request is in flight: the
        # service enters it right before that call and leaves it right after.
        outcome = await turn.send(
            text,
            while_working=lambda: ChatActionSender.typing(
                chat_id=message.chat.id, bot=bot
            ),
        )

        if outcome.status is not OutcomeStatus.REPLIED:
            # Nothing was exchanged (too long, or the AI failed): tell the user why.
            await message.answer(messages_ru.render(outcome))
            return

        # The exchange is already committed to the history, so a failure to
        # deliver the answer does not undo it.
        try:
            await message.answer(messages_ru.render(outcome))
        except (TelegramNetworkError, TelegramAPIError) as exc:
            logger.error("Telegram error for user_id=%s: %s", user_id, exc)
            await message.answer("Не удалось отправить ответ. Попробуй ещё раз.")


async def main() -> None:
    errors = config.validate_config()
    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        sys.exit(1)

    settings = config.get_settings()
    bot = Bot(token=settings.bot_token)
    logger.info("Kitchen Helper bot is starting...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await api_client.close_client()


if __name__ == "__main__":
    asyncio.run(main())
