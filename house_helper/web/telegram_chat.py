"""The Telegram bot: the same assistant, the same conversations, from a phone.

What this is. A long-polling loop inside the web process that reads the messages people send
to the bot and answers them through the SAME turn runner as the website (turn_runner.py), so a
Telegram message is stored in the same database tables, in the same conversation list, under
the same limits, idempotency and retention as one typed on the page. There is no second history
to reconcile: "the conversation started on the website continues in Telegram and the other way
round" is a property of there being one conversation store, not of copying. The model of this
is the owner's Plain English bot (one shared log per person, read by both clients); what it did
not have, and this does, is a verified tie between the sender and the person, no answer for
strangers, and a delivery that cannot be played twice.

Why inside the web process, and polling. The application is one process on SQLite (Dockerfile:
"One worker, deliberately"); a second process writing the same file would break the assumptions
the turn runner and the lease logic are built on. Polling needs no public route, no secret in a
URL, no Traefik label and no setWebhook step, and works the same on the owner's machine and on
the server. The price is that exactly one process may poll with a token (a second one gets 409,
which is logged, and waits).

Who is served. A sender is served only if their Telegram account is linked to a user who is
still enrolled (persistence.telegram.enrolled_user_for_telegram, asked on every message). The
tie is made by a pairing code from a signed-in website session, presented to the bot as
/start <code> (persistence.telegram says why that, and not the sign-in identity). Anybody else
gets one short fixed notice, at most once in ten minutes, and costs no request to the
assistant and no row in the database. Private chats only: an update from a group, a channel or
a bot is dropped without an answer.

Who is served, at the moment of serving. The sender is resolved when the update arrives, to
decide what kind of update it is; but a message to the assistant may wait a long time for its
turn in the chat, and the tie may be gone by then (the person was unlinked, or their access
withdrawn, or the account was linked to somebody else). So the tie is asked again, as a
precondition, in two places: after the chat's lock is taken and before anything is done for the
message (no photo is fetched and nothing is sent to the assistant for a tie that is gone), once
more inside the transaction that claims the turn (so "still this person's" and "claimed" are one
step), and, under a second per-chat lock (the binding lock), immediately before anything that
belongs to the user is sent to the chat (the answer, the copy of a website turn). What is queued for a tie that
is gone is DROPPED: the sender is told, in fixed words that carry nothing of the message, and
nothing is redirected to whoever the account belongs to now. Unlinking (`/unlink` and the
website's button: TelegramChat.unlink) takes the same binding lock, and `/new` is one
transaction with its check, so a change of the tie falls entirely before or entirely after a
verify-and-send. Unlinking removes a link only with a statement that names its account, under that
account's lock, and keeps the lock until its database unit has ended even if the unlinking
task is cancelled meanwhile (WebDatabase.run_to_completion); an unlink that finds no link
withdraws the pairing codes and removes nothing, so a link made after it looked is not touched. The limit: the operator's revoke and disable commands run in another process
and cannot take an in-process lock, so for them the window between the last check and
Telegram's taking the message is the length of that one request; an answer that is already
sent cannot be recalled.

Photos. A photograph with an optional caption is a message like any other, with the picture
attached: the bot takes the largest size Telegram offers that is within the limit, downloads it
(through the bot's own file endpoint, so no file address or token ever leaves this process),
checks by its bytes that it is a JPEG, a PNG or a WebP, keeps it as a photo of the person
(persistence.attachments) and hands the message to the same turn runner with the photo
attached. The assistant is shown it; the website shows it in the history, served only to its
owner; it expires with the message. A photo costs no download when its update is a redelivery
(the first delivery already attached it). A stranger's photo is never downloaded. An album
arrives as one update per picture and is answered one picture at a time.

Order and duplicates. Messages of one chat are handled one at a time, in the order they came
(a lock per chat), as the old bot did. The idempotency key of a turn is "tg-<update id>", and
the assistant is not run a second time for an update once an attempt at it has begun. An update
that Telegram delivers again is, by what the first delivery left in the database:

  * completed: answered from the first completion (turn_runner: the key is replayed, nothing is
    generated), in the conversation the first delivery used. The answer may reach the person a
    second time;
  * claimed and not completed (the provider failed or timed out, the lease ran out, the process
    was restarted): NOT run again, whatever it failed with. The claim of an attempt is committed
    before the provider is called, so that fact is in the database and survives a restart; what
    cannot be known is whether the first request reached the provider. The person is told, in
    fixed words, that the message was not answered and to write it again (a new message is a new
    update, which is the person's choice and not an automatic repeat). Nothing is fabricated;
  * never claimed: handled as a new message;
  * claimed for ANOTHER user (the account was unlinked from them and tied to somebody else since,
    and Telegram brings the update again): dropped, whatever state the first delivery is in. An
    update id belongs to the bot, not to whoever the account is tied to this week, so it is
    looked up across owners (persistence.telegram.update_admitted_for_another), before anything
    is fetched, and again inside the transaction that claims the turn (admit_update). The
    person behind the account gets the fixed "the connection changed" notice and nothing of
    the first owner's message, photo or answer is read, stored, sent to the assistant or sent
    on; the first owner's rows are not touched. The record is the turn request the claim wrote;
    an update that never reached a claim left none, and cannot be told from a new message.

This is a promise about the assistant, not "exactly once" delivery. An answer can reach Telegram
twice (the first send may have been lost on the way), and an acknowledged message that was being
handled when the process was killed gets no answer at all (the website's history then shows it
as one that was not answered). Updates are acknowledged to Telegram by the next getUpdates call,
which the loop makes as soon as the updates are handed to handlers; the offset lives in memory
only and has nothing to do with answering. So an unfinished handler is not delivered again after
a restart (it was acknowledged); a redelivery is the rare case in which the acknowledging call
itself did not reach Telegram.

From the website to Telegram. The runner tells this class when a turn has completed
(on_completion). A turn that came from the website is copied to the person's Telegram chat,
if they have one linked: first what they wrote there, labelled, then the answer, as Plain
English does. A turn that came from Telegram is NEVER copied back, which is the whole of the
loop prevention (the bot also never receives its own messages). A replayed turn is not a
completion, so a retry of a web request does not copy it twice. The copy is sent from a task of
its own: the website's answer does not wait for Telegram and is not affected by it.

One consumer, one instance. Telegram lets exactly one getUpdates consumer read a token, and none
while a webhook is set; a second reader (another copy of this application, the old bot.py, an n8n
Telegram trigger) answers 409 to whichever asks second. The bot never sets, reads or deletes a
webhook and never takes the token over: it reads nothing, says so (below) and tries again.

When it is not working, it says so. The first getUpdates after start asks for no wait, so a
refused token (401) or a second consumer (409) shows within a second, not after a long poll. While
the last getUpdates failed, `failure` is the category of the failure ("api:409", "api:401",
"network"...) and None otherwise; the pairing routes read it (telegram_api.py), so the website
neither offers to connect a bot that is not reading nor shows a connected one as working. The log
has a line for the start ("The Telegram bot is polling"), one error line when polling is blocked or
refused, and one for the recovery. That is the whole signal: no metric, no probe, and /healthz
stays a liveness check of the process.

What is logged: fixed words and the names of exception classes, and the Bot API's numeric
error codes. Never a Telegram id, a name, a message or an answer.
"""

import asyncio
import enum
import logging
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional
from uuid import UUID

from house_helper.persistence import attachments as photos
from house_helper.persistence import conversations
from house_helper.persistence import telegram as store
from house_helper.persistence import turns
from house_helper.persistence.identifiers import MAX_ATTACHMENT_BYTES
from house_helper.web.chat_api import clean_message_text
from house_helper.web.database import WebDatabase
from house_helper.web.diagnostics import ApiError, new_correlation_id
from house_helper.web.settings import BotSettings
from house_helper.web.telegram_botapi import BotApi, BotApiError, split_text
from house_helper.web.turn_runner import (
    TELEGRAM_CHANNEL,
    WEB_CHANNEL,
    TurnCompletion,
    TurnNotAuthorized,
    WebTurnRunner,
)

logger = logging.getLogger("house_helper.web.telegram")

# How many updates may be in the hands of handlers at once. Far above what two people send;
# the bound is for a flood from somebody who is not one of them (each of those is cheap).
MAX_IN_FLIGHT = 64

# A stranger gets the notice at most this often, and the table of who was told is bounded.
NOTICE_INTERVAL = timedelta(minutes=10)
# A sender may present this many unusable pairing codes in the window, and then is not
# answered about codes until it passes.
MAX_BAD_CODES = 5
BAD_CODE_WINDOW = timedelta(minutes=10)
TRACKED_SENDERS = 1000
# How many times the website's unlink looks the link up and takes the lock of the account it saw
# before it gives up because the link keeps changing under it.
UNLINK_ATTEMPTS = 3

# --- what the bot says -------------------------------------------------------------------

PRIVACY = (
    "Обратите внимание: текст ваших сообщений и фотографии передаются во внешний сервис OpenAI "
    "для обработки и подготовки ответа. История разговоров хранится 365 дней и затем удаляется; "
    "рецепты, сохранённые в избранное на сайте, остаются, пока вы сами их не уберёте."
)

HELP = (
    "Я помогаю с домашней кухней: напишите, какие продукты есть дома или какое блюдо "
    "хочется приготовить. Можно прислать и фото: продуктов, блюда или кухонной техники.\n\n"
    "Этот разговор общий с сайтом House Helper: начатое там можно продолжить здесь, "
    "и наоборот. Всё, что вы пишете помощнику на сайте, вместе с его ответом я пересылаю сюда; "
    "чтобы это прекратить, отключите Telegram командой /unlink или на сайте.\n\n"
    "Команды:\n"
    "/new — начать новый разговор\n"
    "/unlink — отключить Telegram от аккаунта\n"
    "/help — эта подсказка\n\n" + PRIVACY
)

GREETING = "Здравствуйте! Я House Helper — помощник по домашней кухне.\n\n" + HELP

NOT_LINKED = (
    "Этот помощник работает только для подключённых аккаунтов House Helper.\n\n"
    "Если у вас есть доступ, откройте сайт House Helper, нажмите «Подключить Telegram» "
    "и перейдите по полученной ссылке."
)

LINKED = (
    "Готово! Telegram подключён к вашему аккаунту House Helper.\n\n"
    "Пишите мне сюда: разговор общий с сайтом. С этого момента всё, что вы пишете помощнику "
    "на сайте, и его ответы будут приходить и сюда. Подсказка — /help.\n\n" + PRIVACY
)
ALREADY_LINKED = "Этот Telegram уже подключён к вашему аккаунту. Можно писать, подсказка — /help."
BAD_CODE = (
    "Эта ссылка недействительна или устарела. Откройте сайт House Helper, "
    "нажмите «Подключить Telegram» и перейдите по новой ссылке."
)
TOO_MANY_CODES = "Слишком много попыток. Подождите несколько минут и попробуйте снова."
ACCOUNT_UNAVAILABLE = "Не удалось подключить Telegram: доступ к этому аккаунту закрыт."
TELEGRAM_TAKEN = "Этот Telegram уже подключён к другому аккаунту House Helper."
USER_HAS_ANOTHER = (
    "К вашему аккаунту уже подключён другой Telegram. "
    "Сначала отключите его на сайте, затем подключите этот."
)
UNLINKED = (
    "Telegram отключён от аккаунта. Чтобы подключить снова, "
    "нажмите «Подключить Telegram» на сайте."
)
NEW_CONVERSATION = "Начинаем новый разговор. О чём поговорим?"
UNKNOWN_COMMAND = "Не знаю такой команды. Подсказка — /help."
UNSUPPORTED = "Пока я понимаю текст и фотографии. Напишите, что хотите приготовить, или пришлите фото."
PHOTO_TOO_BIG = "Фото слишком большое. Пришлите, пожалуйста, обычное фото (до 5 МБ)."
PHOTO_FAILED = "Не удалось получить фото из Telegram. Пришлите его, пожалуйста, ещё раз."
PHOTO_UNSUPPORTED = "Не получилось разобрать этот файл. Пришлите обычное фото (JPEG, PNG или WebP)."
TROUBLE = "Сервис временно недоступен. Попробуйте ещё раз чуть позже."
STILL_BUSY = "Ещё готовлю предыдущий ответ. Подождите немного и напишите ещё раз."
FROM_WEBSITE = "Вы (на сайте):\n"
BINDING_CHANGED = (
    "Подключение Telegram изменилось, поэтому это сообщение не обработано. "
    "Если оно ещё нужно, напишите его заново."
)
NOT_RUN_AGAIN = (
    "Ответ на это сообщение уже начинали готовить, но он не был получен, и повторно я его не запрашиваю. "
    "Если сообщение ещё нужно, напишите его заново."
)


def _count(value: Any) -> Optional[int]:
    """A non-negative integer the Bot API sent, or None (a bool is not one)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _display_name(sender: dict[str, Any]) -> Optional[str]:
    username = sender.get("username")
    if isinstance(username, str) and username:
        return "@" + username
    parts = [sender.get("first_name"), sender.get("last_name")]
    return " ".join(part for part in parts if isinstance(part, str) and part) or None


def _command(text: str, bot_username: str) -> Optional[tuple[str, str]]:
    """("start", "payload") for "/start payload" or "/start@bot payload"; None when the text
    is not a command. Whatever begins with "/" is one and is never sent to the assistant: a
    command addressed to another bot (or with no name) comes out as the name "" and is
    answered like any unknown command."""
    if not text.startswith("/"):
        return None
    head, _, argument = text.partition(" ")
    name, _, addressee = head[1:].partition("@")
    if addressee and addressee.lower() != bot_username.lower():
        name = ""
    return name.lower(), argument.strip()


def _largest_photo(sizes: list) -> Optional[str]:
    """The file id of the biggest (by area) of the sizes Telegram offers that is within the
    limit, or None. Everything in the list is the Bot API's word, checked before it is used."""
    best: Optional[tuple[int, str]] = None
    for item in sizes:
        if not isinstance(item, dict):
            continue
        file_id, width, height = item.get("file_id"), _count(item.get("width")), _count(item.get("height"))
        size = item.get("file_size")
        if not isinstance(file_id, str) or not 0 < len(file_id) <= 256 or not width or not height:
            continue
        if size is not None and (_count(size) is None or size > MAX_ATTACHMENT_BYTES):
            continue
        if best is None or width * height > best[0]:
            best = (width * height, file_id)
    return None if best is None else best[1]


class Delivery(enum.Enum):
    """What became of a message that belongs to a user and was meant for their chat."""

    SENT = "sent"
    NOT_TAKEN = "not_taken"  # Telegram refused it or could not be reached (logged by _say)
    REFUSED = "refused"  # the account is no longer the enrolled sender of that user: nothing sent


class TelegramChat:
    """The bot. start() begins polling, aclose() stops it and lets what it is doing finish.

    `runner` returns the application's turn runner (None while the assistant is not
    configured); `database` is the application's; `clock` is the application's clock.
    `sleep` and `typing_interval` are seams for tests.
    """

    def __init__(
        self,
        settings: BotSettings,
        api: BotApi,
        database: WebDatabase,
        runner: Callable[[], Optional[WebTurnRunner]],
        clock: Callable[[], datetime],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        typing_interval: float = 4.0,
        drain_seconds: float = 15.0,
    ) -> None:
        self._settings = settings
        self._api = api
        self._database = database
        self._runner = runner
        self._clock = clock
        self._sleep = sleep
        self._typing_interval = typing_interval
        self._drain = drain_seconds
        self._poller: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self._failure: Optional[str] = None
        self._announced = False
        self._chat_locks: dict[int, list] = {}  # chat id -> [lock, users]: the order of its work
        self._binding_locks: dict[int, list] = {}  # chat id -> [lock, users]: tie vs. delivery
        self._told: "OrderedDict[int, datetime]" = OrderedDict()
        self._bad_codes: "OrderedDict[int, deque[datetime]]" = OrderedDict()

    # --- lifecycle ------------------------------------------------------------------------

    @property
    def failure(self) -> Optional[str]:
        """None while polling works (and before the first failure); otherwise the category of
        the failure of the last getUpdates ("api:409", "api:401", "network", ...)."""
        return self._failure

    def start(self) -> None:
        if self._poller is None and not self._closing:
            self._poller = asyncio.ensure_future(self._poll())
            self._poller.add_done_callback(self._poller_ended)

    def _poller_ended(self, task: asyncio.Task) -> None:
        """A polling loop that dies of a bug must not leave the bot looking healthy: its end is a
        failure, with a line in the log. (An end after a refused token, or by aclose, is not.)"""
        if task.cancelled() or self._closing:
            return
        error = task.exception()
        if error is not None:
            self._failure = type(error).__name__
            logger.error(
                "The Telegram bot stopped: polling failed with %s. The website works without it; restart to retry.",
                type(error).__name__,
            )

    async def aclose(self) -> None:
        """Stop reading, let the handlers in hand finish for a while, cancel what is left."""
        self._closing = True
        poller, self._poller = self._poller, None
        if poller is not None:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
        pending = set(self._tasks)
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=self._drain)
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
                logger.warning(
                    "The Telegram bot stopped with %d handler(s) unfinished", len(unfinished)
                )

    def _spawn(self, work: Awaitable[None]) -> None:
        if self._closing or len(self._tasks) >= MAX_IN_FLIGHT:
            if hasattr(work, "close"):
                work.close()  # never awaited: no "coroutine was never awaited" warning
            if not self._closing:
                logger.warning("The Telegram bot is overloaded; an update was dropped")
            return
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._forget)

    def _forget(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # retrieved: handle_update does not raise, so this is a bug's trace

    # --- reading ---------------------------------------------------------------------------

    def _polling_works(self) -> None:
        if not self._announced:
            logger.info("The Telegram bot is polling")
        elif self._failure is not None:
            logger.info("The Telegram bot is polling again")
        self._announced = True
        self._failure = None

    async def _poll(self) -> None:
        offset: Optional[int] = None
        delay = 1.0
        started = False
        while not self._closing:
            try:
                # (The first call waits for nothing: it is the start-up check, answered at once.)
                updates = await self._api.get_updates(offset, self._settings.poll_seconds if started else 0)
            except asyncio.CancelledError:
                raise
            except BotApiError as error:
                if self._closing:
                    return
                new_failure = self._failure != error.category
                self._failure = error.category
                if error.permanent:
                    logger.error(
                        "The Telegram bot stopped: its token was refused (%s). "
                        "The website works without it; fix the token and restart.",
                        error.category,
                    )
                    return
                wait = float(error.retry_after) if error.retry_after else (30.0 if error.conflict else delay)
                if error.conflict and new_failure:
                    logger.error(
                        "The Telegram bot is blocked: another consumer or a webhook holds its token "
                        "(category=%s). It reads nothing until that is removed; the website works without it.",
                        error.category,
                    )
                else:
                    logger.warning(
                        "Telegram polling failed: category=%s, trying again in %.0f s",
                        error.category,
                        wait,
                    )
                delay = min(delay * 2, 60.0)
                await self._sleep(min(wait, 120.0))
                continue
            except Exception as exc:  # a bug; polling must not die of it
                self._failure = type(exc).__name__
                logger.error("Telegram polling failed: category=%s", type(exc).__name__)
                await self._sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            started = True
            self._polling_works()
            delay = 1.0
            for update in updates:
                update_id = _count(update.get("update_id")) if isinstance(update, dict) else None
                if update_id is not None:
                    offset = max(offset or 0, update_id + 1)
                self._spawn(self.handle_update(update))

    # --- one update ---------------------------------------------------------------------------

    async def handle_update(self, update: Any) -> None:
        """Handle one update. Never raises (cancellation excepted): a failure is logged by
        the class of its exception and, if the sender is known, answered with a plain notice."""
        account: Optional[int] = None
        try:
            parsed = self._parse(update)
            if parsed is None:
                return
            account, update_id, message = parsed
            await self._serve(account, update_id, message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("A Telegram update could not be handled: category=%s", type(exc).__name__)
            if account is not None:
                try:
                    await self._say(account, TROUBLE)
                except Exception:  # (_say does not raise Bot API errors; this is for a bug)
                    pass

    @staticmethod
    def _parse(update: Any) -> Optional[tuple[int, int, dict[str, Any]]]:
        """(account, update id, message) for a message from a person in a private chat; None
        for anything else (another kind of update, a group, a bot, a malformed one)."""
        if not isinstance(update, dict):
            return None
        update_id = _count(update.get("update_id"))
        message = update.get("message")
        if update_id is None or not isinstance(message, dict):
            return None
        sender, chat = message.get("from"), message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return None
        account = _count(sender.get("id"))
        if (
            not account
            or sender.get("is_bot") is True
            or chat.get("type") != "private"
            or chat.get("id") != account
        ):
            return None
        return account, update_id, message

    async def _serve(self, account: int, update_id: int, message: dict[str, Any]) -> None:
        text = message.get("text")
        command = _command(text, self._settings.username) if isinstance(text, str) else None
        user_id = await self._bound_user(account)

        if command is not None and command[0] == "start":
            await self._on_start(account, message, command[1], user_id)
            return
        if user_id is None:
            await self._notify_stranger(account)
            return
        if command is not None:
            await self._on_command(account, user_id, command[0])
            return
        if isinstance(message.get("photo"), list) and message["photo"]:
            await self._chat_turn(account, user_id, update_id, message.get("caption"), message["photo"])
            return
        if isinstance(text, str):
            await self._chat_turn(account, user_id, update_id, text)
            return
        await self._say(account, UNSUPPORTED)

    # --- commands --------------------------------------------------------------------------------

    async def _on_start(
        self, account: int, message: dict[str, Any], payload: str, user_id: Optional[UUID]
    ) -> None:
        if payload:
            await self._pair(account, message, payload)
        elif user_id is None:
            await self._notify_stranger(account)
        else:
            await self._say(account, GREETING)

    async def _on_command(self, account: int, user_id: UUID, name: str) -> None:
        if name == "help":
            await self._say(account, HELP)
        elif name == "new":
            now = self._clock()
            started = await self._database.run(
                lambda session: store.start_conversation_for(session, account, user_id, now=now)
            )
            if started is None:  # the tie was gone by the time it was this message's turn
                await self._refuse_stale(account)
            else:
                await self._say(account, NEW_CONVERSATION)
        elif name == "unlink":
            if await self.unlink(user_id, account=account):
                await self._say(account, UNLINKED)
            else:  # already unlinked, or the account was linked to somebody else since
                await self._refuse_stale(account)
        else:
            await self._say(account, UNKNOWN_COMMAND)

    async def _pair(self, account: int, message: dict[str, Any], code: str) -> None:
        now = self._clock()
        if self._too_many_bad_codes(account, now):
            await self._say(account, TOO_MANY_CODES)
            return
        sender = message["from"]
        name = _display_name(sender)
        result = await self._database.run(
            lambda session: store.consume_link_code(session, code, account, name, now=now)
        )
        outcome = result.outcome
        if outcome is store.LinkOutcome.INVALID_CODE:
            self._note_bad_code(account, now)
        logger.info("Telegram pairing: outcome=%s", outcome.value)
        await self._say(
            account,
            {
                store.LinkOutcome.LINKED: LINKED,
                store.LinkOutcome.ALREADY_LINKED: ALREADY_LINKED,
                store.LinkOutcome.INVALID_CODE: BAD_CODE,
                store.LinkOutcome.ACCOUNT_UNAVAILABLE: ACCOUNT_UNAVAILABLE,
                store.LinkOutcome.TELEGRAM_TAKEN: TELEGRAM_TAKEN,
                store.LinkOutcome.USER_HAS_ANOTHER: USER_HAS_ANOTHER,
            }[outcome],
        )

    # --- strangers and guessing -------------------------------------------------------------------

    async def _notify_stranger(self, account: int) -> None:
        """The one fixed notice, at most once in NOTICE_INTERVAL per sender. Costs nothing:
        no database write, no request to the assistant."""
        now = self._clock()
        last = self._told.get(account)
        if last is not None and now - last < NOTICE_INTERVAL:
            return
        self._told[account] = now
        self._told.move_to_end(account)
        while len(self._told) > TRACKED_SENDERS:
            self._told.popitem(last=False)
        await self._say(account, NOT_LINKED)

    def _too_many_bad_codes(self, account: int, now: datetime) -> bool:
        attempts = self._bad_codes.get(account)
        if attempts is None:
            return False
        while attempts and now - attempts[0] >= BAD_CODE_WINDOW:
            attempts.popleft()
        return len(attempts) >= MAX_BAD_CODES

    def _note_bad_code(self, account: int, now: datetime) -> None:
        attempts = self._bad_codes.setdefault(account, deque())
        attempts.append(now)
        self._bad_codes.move_to_end(account)
        while len(self._bad_codes) > TRACKED_SENDERS:
            self._bad_codes.popitem(last=False)

    # --- a message to the assistant ---------------------------------------------------------------

    async def _chat_turn(
        self,
        account: int,
        user_id: UUID,
        update_id: int,
        raw: Any,
        sizes: Optional[list] = None,
    ) -> None:
        """One message to the assistant: `raw` is its text, or, with `sizes` (the sizes of a
        photo that Telegram offers), its caption. `user_id` is who the sender was when the
        update arrived; whether they still are is asked again once it is this message's turn."""
        async with self._chat_lock(account):
            if await self._bound_user(account) != user_id:
                await self._refuse_stale(account)  # before anything is fetched or asked of the assistant
                return
            if await self._database.run(lambda session: store.update_admitted_for_another(session, user_id, update_id)):
                # Telegram brought back an update that was admitted while this account was tied to
                # somebody else. It is not this person's: nothing is read, fetched, stored or asked
                # of the assistant, and what the first owner wrote or was answered goes nowhere.
                # (This look is for the common case and for what it saves; the claim asks again
                # in its own transaction, which is what decides.)
                logger.warning("A redelivered Telegram update was dropped: it was admitted for another account")
                await self._refuse_stale(account)
                return
            try:
                if sizes is not None and not (isinstance(raw, str) and raw.strip()):
                    text = photos.PHOTO_PLACEHOLDER
                else:
                    text = clean_message_text(raw)
            except ApiError as error:
                await self._say(account, error.message)
                return
            runner = self._runner()
            if runner is None:
                await self._say(account, "Помощник сейчас недоступен. Попробуйте позже.")
                return
            key = store.update_key(update_id)
            async with self._typing(account):
                attachment_id = None
                if sizes is not None:
                    # (A redelivered update already has its photo attached: nothing to fetch.)
                    seen = await self._database.run(
                        lambda session: store.conversation_of_request(session, user_id, key)
                    )
                    if seen is None:
                        attachment_id = await self._take_photo(account, user_id, sizes)
                        if attachment_id is None:
                            return
                conversation_id = await self._conversation_for(user_id, key)
                correlation_id = new_correlation_id()
                try:
                    result = await runner.submit(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        idempotency_key=key,
                        text=text,
                        correlation_id=correlation_id,
                        route="telegram",
                        origin=TELEGRAM_CHANNEL,
                        attachment_id=attachment_id,
                        reclaim=False,  # nobody chose to send this again: Telegram did, by itself
                        # The tie, and whose update this is, are asked once more in the claim's
                        # own transaction: "still this person's", "not another owner's update"
                        # and "claimed" are one step, so the assistant is never asked for a tie
                        # that went away, or for an update that belongs to somebody else, in the
                        # moments since the checks above.
                        authorize=lambda session: store.admit_update(session, account, user_id, update_id),
                    )
                except TurnNotAuthorized:
                    if attachment_id is not None:
                        await self._discard_photo(user_id, attachment_id)
                    await self._refuse_stale(account)
                    return
                except turns.TurnAttempted:
                    # An attempt at this very update began before and did not complete. The
                    # provider is not asked again; the person is told so (nothing is invented).
                    logger.warning("A redelivered Telegram update was not run again: an attempt had begun")
                    if attachment_id is not None:
                        await self._discard_photo(user_id, attachment_id)
                    await self._say(account, NOT_RUN_AGAIN)
                    return
                except ApiError as error:
                    if attachment_id is not None:
                        await self._discard_photo(user_id, attachment_id)
                    await self._say(account, self._failure_text(error, correlation_id))
                    return
            if await self._deliver(account, user_id, result.completed.assistant_message.text) is Delivery.REFUSED:
                await self._refuse_stale(account)  # the answer is stored for its owner; it is not sent on

    async def _discard_photo(self, user_id: UUID, attachment_id: UUID) -> None:
        """A turn that was refused leaves its photo unattached: delete it now instead of letting
        it wait out its hour (and count against the person's few uploads). A photo that the turn
        did take before it failed stays with its message. Best effort."""
        try:
            await self._database.run(lambda session: photos.discard(session, user_id, attachment_id))
        except Exception as exc:
            logger.warning("An unused photo could not be removed: category=%s", type(exc).__name__)

    async def _take_photo(self, account: int, user_id: UUID, sizes: list) -> Optional[UUID]:
        """Download the photo, check it and keep it as the person's. The id of the stored photo,
        or None after the person has been told why there is none."""
        file_id = _largest_photo(sizes)
        if file_id is None:
            await self._say(account, PHOTO_TOO_BIG)
            return None
        try:
            path, _ = await self._api.get_file(file_id)
            data = await self._api.download(path, MAX_ATTACHMENT_BYTES)
        except BotApiError as error:
            logger.warning("A photo could not be fetched from Telegram: category=%s", error.category)
            await self._say(account, PHOTO_TOO_BIG if error.kind == "too_large" else PHOTO_FAILED)
            return None
        now = self._clock()
        try:
            stored = await self._database.run(
                lambda session: photos.store(session, user_id, data, now=now)
            )
        except photos.NotAPhoto:
            await self._say(account, PHOTO_UNSUPPORTED)
            return None
        except photos.TooManyUploads:
            await self._say(account, TROUBLE)
            return None
        return stored.id

    @staticmethod
    def _failure_text(error: ApiError, correlation_id: str) -> str:
        if error.code in ("conversation_busy", "turn_in_progress"):
            return STILL_BUSY
        text = error.message
        if error.spec.status >= 500:
            text += f"\n\nКод для поддержки: {correlation_id}"
        return text

    async def _conversation_for(self, user_id: UUID, key: str) -> UUID:
        """The conversation this message belongs to: the one its first delivery used, or the
        person's most recently active one (the one the website opens), or a new one."""
        found = await self._database.run(
            lambda session: store.conversation_of_request(session, user_id, key)
            or store.latest_conversation_id(session, user_id)
        )
        if found is not None:
            return found
        now = self._clock()
        created = await self._database.run(
            lambda session: conversations.create_conversation(session, user_id, now=now)
        )
        return created.id

    # --- from the website ------------------------------------------------------------------------

    def on_completion(self, completion: TurnCompletion) -> None:
        """The turn runner's hook. Only a turn that came from the website is copied to
        Telegram; one that came from Telegram never is (see the module docstring)."""
        if completion.origin != WEB_CHANNEL or self._closing:
            return
        self._spawn(self._mirror(completion))

    async def _mirror(self, completion: TurnCompletion) -> None:
        """Copy a website turn to its user's chat. The chat is looked up first only to know
        whose turn in the line to wait for. By the time the line reaches this copy the account
        may no longer be the user's (unlinked, linked to somebody else, access withdrawn), so
        _deliver asks again, under the binding lock, and a copy that has lost its tie is
        dropped: it is not sent to whoever the account belongs to now."""
        try:
            account = await self._database.run(
                lambda session: store.chat_of_user(session, completion.user_id)
            )
            if account is None:
                return
            async with self._chat_lock(account):
                outcome = await self._deliver(
                    account, completion.user_id, FROM_WEBSITE + completion.user_text, completion.reply_text
                )
            if outcome is Delivery.REFUSED:
                logger.info("A turn was not copied to Telegram: the chat is no longer the user's")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("A turn could not be copied to Telegram: category=%s", type(exc).__name__)

    # --- the tie between an account and a user ---------------------------------------------------

    async def _bound_user(self, account: int) -> Optional[UUID]:
        """The enrolled user this Telegram account is tied to right now, or None."""
        return await self._database.run(
            lambda session: store.enrolled_user_for_telegram(session, account)
        )

    async def _deliver(self, account: int, user_id: UUID, *texts: str) -> Delivery:
        """Send `texts`, which belong to `user_id`, to the chat of `account`, but only while that
        account is still the enrolled sender of that user. The question and the sends are made
        under the binding lock, which unlinking takes too: the tie cannot be taken apart (and the
        account given to somebody else) between the check and the send. Nothing is sent, and
        REFUSED returned, when the tie is gone."""
        async with self._binding(account):
            if await self._bound_user(account) != user_id:
                return Delivery.REFUSED
            for text in texts:
                if not await self._say(account, text):
                    return Delivery.NOT_TAKEN
        return Delivery.SENT

    async def _refuse_stale(self, account: int) -> None:
        """Say that a message was dropped because its sender has no tie to its user any more.
        The words are fixed and carry nothing of the message, of the user or of whoever the
        account belongs to now: a sender who is nobody's gets the stranger's notice, one who is
        somebody's gets BINDING_CHANGED."""
        logger.info("A Telegram message was dropped: it is not this sender's to be served any more")
        if await self._bound_user(account) is None:
            await self._notify_stranger(account)
        else:
            await self._say(account, BINDING_CHANGED)

    async def unlink(self, user_id: UUID, account: Optional[int] = None) -> bool:
        """Remove the Telegram link of a user (and the pairing codes they hold), under the
        binding lock of the linked account, so that it cannot fall between a delivery check and
        its send. True if a link was removed.

        Without `account` (the website button) it is the link the user has when it looks. With it
        (the bot command /unlink, asked by that account) it is the link between that account and
        that user only, and a tie made since is left alone (False).

        A link is only ever removed by a statement that names its account, while the binding lock
        of that account is held, and the lock is kept until the database unit has ended, whoever
        cancels this call meanwhile (WebDatabase.run_to_completion). So:

          * no link when it looks: the pairing codes the user holds are withdrawn, atomically with
            that fact, and False is returned. A link made after the look is not removed (it is not
            what was asked to be removed); the person can ask again;
          * a link when it looks: it is removed under the lock of its account; if it is another
            link by the time the lock is held (the person unlinked and linked again, racing
            themselves), it looks again, UNLINK_ATTEMPTS times, and then raises ApiError
            service_unavailable with the link as it was found last still in place."""
        if account is not None:
            async with self._binding(account):
                return await self._database.run_to_completion(
                    lambda session: store.unlink(session, user_id, telegram_user_id=account)
                )
        for _ in range(UNLINK_ATTEMPTS):
            link = await self._database.run(lambda session: store.get_link(session, user_id))
            if link is None:
                await self._database.run(lambda session: store.withdraw_codes_unless_linked(session, user_id))
                return False
            target = link.telegram_user_id
            async with self._binding(target):
                removed = await self._database.run_to_completion(
                    lambda session: store.unlink(session, user_id, telegram_user_id=target)
                )
            if removed:
                return True
        raise ApiError("service_unavailable", category="UnlinkContended")

    # --- talking to Telegram -----------------------------------------------------------------------

    async def _say(self, account: int, text: str) -> bool:
        """Send `text` (in pieces if it is long). False, with the reason logged, when
        Telegram refused or could not be reached; never raises a Bot API error."""
        for piece in split_text(text):
            try:
                await self._api.send_message(account, piece)
            except BotApiError as error:
                logger.warning("Telegram did not take a message: category=%s", error.category)
                return False
        return True

    @asynccontextmanager
    async def _typing(self, account: int):
        """"typing…" in the chat for as long as the block lasts."""

        async def beat() -> None:
            while True:
                try:
                    await self._api.send_chat_action(account)
                except BotApiError:
                    pass
                await asyncio.sleep(self._typing_interval)

        task = asyncio.ensure_future(beat())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @asynccontextmanager
    async def _chat_lock(self, account: int):
        """One thing at a time in a chat, in the order it was asked for (asyncio.Lock is
        fair). The entry goes when the last user of it does."""
        async with self._keyed_lock(self._chat_locks, account):
            yield

    @asynccontextmanager
    async def _binding(self, account: int):
        """The binding lock of a chat: held by whoever sends something that belongs to the
        user the account is tied to (from the check of the tie to the end of the send), and
        by whoever takes the tie apart. It is held for the length of a check and a few sends,
        never for the length of an answer, and is only ever taken after the chat lock (never
        the other way round), so it cannot deadlock with it."""
        async with self._keyed_lock(self._binding_locks, account):
            yield

    @staticmethod
    @asynccontextmanager
    async def _keyed_lock(table: dict[int, list], account: int):
        entry = table.get(account)
        if entry is None:
            entry = table[account] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                table.pop(account, None)
