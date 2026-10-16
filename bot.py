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
import context_manager
from house_helper.core import messages_ru
from house_helper.core.backend import (
    BackendConnectionError,
    BackendEmptyResponseError,
    BackendError,
    BackendRateLimitedError,
    ChatBackend,
    OpenAIBackend,
)
from house_helper.core.identity import ConversationKey
from house_helper.core.outcomes import ChatOutcome, OutcomeStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

RESET_PHRASE = "очистить контекст"

# Application-level cap on a single incoming message, independent of
# Telegram's own ~4096-character limit. Keeps a single request's OpenAI cost
# and the context it adds predictable, regardless of how large a message a
# user sends.
MAX_INPUT_LENGTH = 2000

dp = Dispatcher()

TELEGRAM_CHANNEL = "telegram"

# The AI backend every conversation is answered by. Stateless (the shared
# OpenAI client lives in api_client), so tests may simply replace it.
_backend: ChatBackend = OpenAIBackend()


def _conversation_key(user_id: int) -> ConversationKey:
    """The conversation a Telegram sender belongs to.

    A conversation is scoped to the sending user, not to the chat: the same
    user writing from a private chat and from a group shares one history and
    one lock. The id is stored as a str so every channel's keys have one shape.
    """
    return ConversationKey(TELEGRAM_CHANNEL, str(user_id))


# One lock per conversation key (currently: per Telegram sender), created
# lazily. Guarantees that the whole "user message -> context update -> OpenAI
# request -> assistant response / rollback" transaction never overlaps for the
# same conversation, while different conversations are still processed
# concurrently.
_user_locks: dict[ConversationKey, asyncio.Lock] = {}


def _get_user_lock(key: ConversationKey) -> asyncio.Lock:
    lock = _user_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[key] = lock
    return lock


async def _clear_user_context(user_id: int, source: str) -> None:
    """Clear a user's conversation context. Caller must hold the user's lock."""
    context_manager.clear_context(_conversation_key(user_id))
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
    async with _get_user_lock(_conversation_key(user_id)):
        await _clear_user_context(user_id, "/reset")
    await message.answer(
        messages_ru.render(ChatOutcome(OutcomeStatus.CONTEXT_CLEARED))
    )


@dp.message(F.text)
async def handle_text(message: Message, bot: Bot) -> None:
    if not message.text or not message.from_user:
        return

    user_id = message.from_user.id
    key = _conversation_key(user_id)
    text = message.text.strip()

    async with _get_user_lock(key):
        if text.lower() == RESET_PHRASE:
            await _clear_user_context(user_id, "reset phrase")
            await message.answer(
                messages_ru.render(ChatOutcome(OutcomeStatus.CONTEXT_CLEARED))
            )
            return

        if len(text) > MAX_INPUT_LENGTH:
            await message.answer(
                messages_ru.render(
                    ChatOutcome(
                        OutcomeStatus.INPUT_TOO_LONG,
                        max_input_length=MAX_INPUT_LENGTH,
                    )
                )
            )
            return

        context_manager.add_message(key, "user", text)
        ctx_len = context_manager.context_length(key)

        try:
            # Single rollback point for the whole transaction: any exception
            # at all -- a recognized OpenAI failure, cancellation, or a
            # completely unexpected one -- means the turn never completed, so
            # the pending user message always comes back out here before the
            # exception is re-raised for the except clauses below (or the
            # caller) to interpret.
            try:
                async with ChatActionSender.typing(chat_id=message.chat.id, bot=bot):
                    backend_reply = await _backend.complete(
                        context_manager.get_context(key),
                        user_id=user_id,
                        context_len=ctx_len,
                    )
                reply = backend_reply.text
            except BaseException:
                context_manager.remove_last_message(key)
                raise
        except asyncio.CancelledError:
            # Never convert cancellation into an API error reply -- just let
            # it propagate untouched now that the rollback above has run.
            raise
        except BackendRateLimitedError:
            await message.answer(
                messages_ru.render(ChatOutcome(OutcomeStatus.RATE_LIMITED))
            )
            return
        except BackendConnectionError:
            await message.answer(
                messages_ru.render(ChatOutcome(OutcomeStatus.CONNECTION_FAILED))
            )
            return
        except BackendEmptyResponseError:
            await message.answer(
                messages_ru.render(ChatOutcome(OutcomeStatus.EMPTY_RESPONSE))
            )
            return
        except BackendError:
            await message.answer(
                messages_ru.render(ChatOutcome(OutcomeStatus.API_ERROR))
            )
            return
        # Any other exception (not one of the recognized failure types above)
        # is unexpected: it must propagate unchanged, never be swallowed or
        # turned into a friendly reply. The rollback already happened above.

        context_manager.add_message(key, "assistant", reply)
        context_manager.trim_history(key)

        try:
            await message.answer(reply)
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
