import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError

import api_client
import context_manager
from config import BOT_TOKEN, validate_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

RESET_PHRASE = "очистить контекст"

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я Kitchen Helper — помогу придумать простое блюдо из того, "
        "что есть дома.\n\n"
        "Напиши, какие продукты у тебя есть, и я предложу варианты.\n\n"
        "Команды:\n"
        "/reset — очистить историю диалога\n"
        'или напиши «очистить контекст»'
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    user_id = message.from_user.id
    context_manager.clear_context(user_id)
    logger.info("Context cleared via /reset for user_id=%s", user_id)
    await message.answer("Контекст очищен. Можем начать с чистого листа!")


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    if not message.text or not message.from_user:
        return

    user_id = message.from_user.id
    text = message.text.strip()

    if text.lower() == RESET_PHRASE:
        context_manager.clear_context(user_id)
        logger.info("Context cleared via phrase for user_id=%s", user_id)
        await message.answer("Контекст очищен. Можем начать с чистого листа!")
        return

    context_manager.add_message(user_id, "user", text)
    ctx_len = context_manager.context_length(user_id)

    try:
        reply, usage = api_client.get_chat_response(
            context_manager.get_context(user_id),
            user_id=user_id,
            context_len=ctx_len,
        )
    except RateLimitError:
        context_manager.get_context(user_id).pop()
        await message.answer(
            "Сейчас слишком много запросов к AI. Подожди немного и попробуй снова."
        )
        return
    except (APITimeoutError, APIConnectionError):
        context_manager.get_context(user_id).pop()
        await message.answer(
            "Не удалось связаться с OpenAI. Проверь интернет и попробуй позже."
        )
        return
    except APIError:
        context_manager.get_context(user_id).pop()
        await message.answer(
            "Ошибка при обращении к OpenAI. Попробуй позже или проверь настройки API."
        )
        return

    context_manager.add_message(user_id, "assistant", reply)

    try:
        await message.answer(reply)
    except (TelegramNetworkError, TelegramAPIError) as exc:
        logger.error("Telegram error for user_id=%s: %s", user_id, exc)
        await message.answer("Не удалось отправить ответ. Попробуй ещё раз.")


async def main() -> None:
    errors = validate_config()
    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        sys.exit(1)

    bot = Bot(token=BOT_TOKEN)
    logger.info("Kitchen Helper bot is starting...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
