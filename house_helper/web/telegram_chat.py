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

Saving a recipe. An answer that carries structured recipes gets an inline keyboard under it:
one "Сохранить рецепт" button when it carries one, one "Сохранить «<title>»" button per recipe
when it carries several. An ordinary answer gets no controls. The button's callback data names
only the recipe's origin -- (assistant message id, position) -- never a user, a conversation or
a title. A click is resolved the way a message is: the account comes from the update, the user
from the current Telegram link (asked again inside the saving transaction, with the link held),
and the recipe from that user's own conversation, so a crafted callback cannot reach somebody
else's favorite. The user the click was authorized for is carried into that transaction and must
be the user the link resolves to there; a tie that changed in between (unlinked, or linked to
somebody else, even if it came back) makes the save refuse, reading nothing and writing nothing,
so the recipe a confirmation would name always belongs to the owner that confirmation is sent
for. Saving copies the validated structured recipe through the same
persistence.favorites.save_favorite as the Web API: no provider call, no parsing of the
rendered text, no second favorites store. (user_id, source_message_id, source_recipe_index) is
unique, so a repeated click answers "already saved" and writes nothing. The keyboard is created
from the validated recipes the turn runner stored beside the answer; the callback is answered
from the database, so a button keeps working after a restart until its answer expires. A
newly saved recipe is followed by one short message naming it and carrying an "Открыть
избранное" button: the callback's toast disappears, and a persistent confirmation must not
rewrite the answer's keyboard (with several recipes its other save buttons have to keep
working, and rebuilding a multi-button markup from what Telegram sends back would make the
confirmation depend on a structure this layer does not trust). A repeated click adds no
message; it answers "already saved".

Reading them back. `/favorites` opens the person's own favorites in the chat: their titles as
buttons (newest first, the total order persistence.favorites.list_favorites gives), one page
at a time, and the complete saved text of the chosen one -- the same content the website
serves -- with a "Назад к избранному" button that returns to the page it was opened from. The
callback data of a list or a recipe names only a page number, or a favorite's id and the page
to return to. It is not a capability: the sender comes from the update, the user from the
current Telegram link, and the favorite is read with that user's id through
favorites.get_favorite, so a button from somebody else's list, a favorite deleted since, and a
made-up id are all one "not found". The list is read again on every open and every page click;
a page that no longer exists because favorites were removed is clamped to the last one.
Nothing here writes: Telegram cannot edit or remove a favorite in this stage, and no provider
is called. A recipe longer than one message is sent in pieces, with the way back under the
last piece.

The command menu. At start the bot registers the commands it wants in Telegram's own menu
(setMyCommands): /start, /favorites and /help, with Russian descriptions. That call replaces
the whole list for the default scope, and the list is fixed, so every start applies the same
menu and a restart adds nothing and removes nothing. A failure is one warning, never a stop
of polling, and the next restart tries again. No BotFather command list is needed or changed:
the runtime call is the native mechanism, and BotFather remains the owner's to change.

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
import re
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional
from uuid import UUID

from house_helper.core.recipes import MAX_RECIPES_PER_REPLY
from house_helper.persistence import attachments as photos
from house_helper.persistence import conversations
from house_helper.persistence import favorites
from house_helper.persistence import telegram as store
from house_helper.persistence import turns
from house_helper.persistence.conversations import ConversationNotFound
from house_helper.persistence.identifiers import MAX_ATTACHMENT_BYTES
from house_helper.persistence.message_recipes import MessageNotFound, RecipeNotFound, StoredRecipe
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
    "рецепты, сохраненные в избранное, остаются, пока вы сами их не уберете."
)

HELP = (
    "Я помогаю с домашней кухней: напишите, какие продукты есть дома или какое блюдо "
    "хочется приготовить. Можно прислать и фото: продуктов, блюда или кухонной техники.\n\n"
    "Сохранять рецепты в избранное можно кнопкой под ответом помощника (когда она есть) "
    "или на сайте House Helper, а открыть сохраненное — командой /favorites. Редактировать "
    "рецепты и заметки можно на сайте House Helper.\n\n"
    "Этот разговор общий с сайтом House Helper: начатое там можно продолжить здесь, "
    "и наоборот. Все, что вы пишете помощнику на сайте, вместе с его ответом я пересылаю сюда; "
    "чтобы это прекратить, отключите Telegram командой /unlink или на сайте.\n\n"
    "Команды:\n"
    "/new — начать новый разговор\n"
    "/favorites — избранное\n"
    "/unlink — отключить Telegram от аккаунта\n"
    "/help — эта подсказка\n\n" + PRIVACY
)

# The native command menu (setMyCommands), the whole list, in this order. The descriptions
# are what Telegram shows under the menu button, so they say what the command is for without
# repeating the command. The list is deliberately the three a person needs to find their way;
# /new and /unlink remain available and are listed in HELP. No BotFather configuration is
# needed for this call, and none is made or read.
BOT_COMMANDS = [
    {"command": "favorites", "description": "📚 Избранное"},
    {"command": "start", "description": "🏠 Начало"},
    {"command": "help", "description": "ℹ️ Помощь"},
]

GREETING = "Здравствуйте! Я House Helper — помощник по домашней кухне.\n\n" + HELP

NOT_LINKED = (
    "Этот помощник работает только для подключенных аккаунтов House Helper.\n\n"
    "Если у вас есть доступ, откройте сайт House Helper, нажмите «Подключить Telegram» "
    "и перейдите по полученной ссылке."
)

LINKED = (
    "Готово! Telegram подключен к вашему аккаунту House Helper.\n\n"
    "Пишите мне сюда: разговор общий с сайтом. С этого момента все, что вы пишете помощнику "
    "на сайте, и его ответы будут приходить и сюда. Подсказка — /help.\n\n" + PRIVACY
)
ALREADY_LINKED = "Этот Telegram уже подключен к вашему аккаунту. Можно писать, подсказка — /help."
BAD_CODE = (
    "Эта ссылка недействительна или устарела. Откройте сайт House Helper, "
    "нажмите «Подключить Telegram» и перейдите по новой ссылке."
)
TOO_MANY_CODES = "Слишком много попыток. Подождите несколько минут и попробуйте снова."
ACCOUNT_UNAVAILABLE = "Не удалось подключить Telegram: доступ к этому аккаунту закрыт."
TELEGRAM_TAKEN = "Этот Telegram уже подключен к другому аккаунту House Helper."
USER_HAS_ANOTHER = (
    "К вашему аккаунту уже подключен другой Telegram. "
    "Сначала отключите его на сайте, затем подключите этот."
)
UNLINKED = (
    "Telegram отключен от аккаунта. Чтобы подключить снова, "
    "нажмите «Подключить Telegram» на сайте."
)
NEW_CONVERSATION = "Начинаем новый разговор. О чем поговорим?"
UNKNOWN_COMMAND = "Не знаю такой команды. Подсказка — /help."
UNSUPPORTED = "Пока я понимаю текст и фотографии. Напишите, что хотите приготовить, или пришлите фото."
PHOTO_TOO_BIG = "Фото слишком большое. Пришлите, пожалуйста, обычное фото (до 5 МБ)."
PHOTO_FAILED = "Не удалось получить фото из Telegram. Пришлите его, пожалуйста, еще раз."
PHOTO_UNSUPPORTED = "Не получилось разобрать этот файл. Пришлите обычное фото (JPEG, PNG или WebP)."
TROUBLE = "Сервис временно недоступен. Попробуйте еще раз чуть позже."
STILL_BUSY = "Еще готовлю предыдущий ответ. Подождите немного и напишите еще раз."
FROM_WEBSITE = "Вы (на сайте):\n"
BINDING_CHANGED = (
    "Подключение Telegram изменилось, поэтому это сообщение не обработано. "
    "Если оно еще нужно, напишите его заново."
)
NOT_RUN_AGAIN = (
    "Ответ на это сообщение уже начинали готовить, но он не был получен, и повторно я его не запрашиваю. "
    "Если сообщение еще нужно, напишите его заново."
)

# --- saving a recipe from a button --------------------------------------------------------

# The callback payload names the recipe's origin and nothing else. It is not a capability:
# the sender is resolved from the update, the owner from the database, and the recipe is
# only ever saved into that owner's favorites. 4 + 19 + 1 + 2 bytes at most, far within
# Telegram's 64-byte callback_data limit.
SAVE_CALLBACK_PREFIX = "fav"
SAVE_BUTTON = "Сохранить рецепт"
# Telegram shows a button's text in one line and bounds it. The bound is measured on the
# UTF-8 form (Cyrillic is two bytes a character) and left under 64 for every client; the
# ellipsis is three bytes.
SAVE_BUTTON_BYTES = 60
BUTTON_ELLIPSIS = "…"

SAVED = "Рецепт сохранен в избранное."
ALREADY_SAVED = "Рецепт уже сохранен в избранном."
RECIPE_UNAVAILABLE = "Этот рецепт больше недоступен. Попросите помощника показать его снова."
SAVE_FAILED = "Не удалось сохранить рецепт. Попробуйте еще раз чуть позже."
FAVORITES_FULL = (
    "В избранном набралось максимальное число рецептов. "
    "Уберите ненужные, чтобы добавить новый."
)
CALLBACK_NOT_LINKED = "Этот помощник работает только для подключенных аккаунтов House Helper."

# --- reading favorites from a button ------------------------------------------------------

# The favorites list: how many titles one page shows, and the callback shapes. A page is
# small enough that the titles stay readable on a phone and the keyboard stays a list rather
# than a wall; 100 favorites (persistence.favorites.FAVORITES_LIMIT) make at most 13 pages.
# A page callback is "fl:<page>"; a recipe callback is "fo:<32 hex digits of the id>:<page it
# was opened from>". Both fit Telegram's 64-byte callback_data limit.
FAVORITES_PAGE_SIZE = 8
FAVORITES_LIST_PREFIX = "fl"
FAVORITES_OPEN_PREFIX = "fo"
MAX_FAVORITE_PAGES = (
    favorites.FAVORITES_LIMIT + FAVORITES_PAGE_SIZE - 1
) // FAVORITES_PAGE_SIZE

FAVORITES_HEADER = "📚 Ваше избранное."
FAVORITES_HINT = "Нажмите на рецепт, чтобы открыть его."
FAVORITES_EMPTY = (
    "В избранном пока пусто.\n\n"
    "Сохранить рецепт можно кнопкой под ответом помощника, если она есть, "
    "или на сайте House Helper."
)
FAVORITES_UNAVAILABLE = "Не удалось открыть избранное. Попробуйте еще раз чуть позже."
FAVORITE_UNAVAILABLE = "Этот рецепт не найден в избранном. Возможно, он был удален."
FAVORITES_BACK = "◀ Назад к избранному"
FAVORITES_OPEN = "Открыть избранное"

_FAVORITES_LIST_CALLBACK = re.compile(r"fl:([0-9]{1,2})")
_FAVORITE_OPEN_CALLBACK = re.compile(r"fo:([0-9a-f]{32}):([0-9]{1,2})")

_SAVE_CALLBACK = re.compile(r"fav:([1-9][0-9]{0,18}):([0-9]{1,2})")


def _save_target(data: Any) -> Optional[tuple[int, int]]:
    """(message id, recipe position) named by a save button's callback data, or None.

    Only the exact shape this bot writes is accepted, and the numbers are bounded the way
    the rest of the layer bounds them. Anything else is an invalid or foreign callback and
    is answered as an unavailable recipe; nothing is looked up."""
    if not isinstance(data, str):
        return None
    found = _SAVE_CALLBACK.fullmatch(data)
    if found is None:
        return None
    message_id, position = int(found.group(1)), int(found.group(2))
    if message_id > favorites.MAX_MESSAGE_ID or position >= MAX_RECIPES_PER_REPLY:
        return None
    return message_id, position


def _bounded_label(label: str) -> str:
    """A button label no longer than SAVE_BUTTON_BYTES in UTF-8, cut at a character boundary
    with an ellipsis when it does not fit. Telegram bounds a button's text; measuring bytes
    keeps the bound true whatever the alphabet (Cyrillic is two bytes a character)."""
    encoded = label.encode("utf-8")
    if len(encoded) <= SAVE_BUTTON_BYTES:
        return label
    head = encoded[: SAVE_BUTTON_BYTES - len(BUTTON_ELLIPSIS.encode("utf-8"))]
    return head.decode("utf-8", "ignore").rstrip() + BUTTON_ELLIPSIS


def _save_button_label(title: str, *, multiple: bool) -> str:
    """The text of one save button: the fixed words for a single recipe, and the recipe's
    own title when one answer carries several (so the buttons can be told apart)."""
    return _bounded_label(f"Сохранить «{title}»" if multiple else SAVE_BUTTON)


def _favorite_label(title: str) -> str:
    """The text of one favorites-list button: the recipe's own title, on one line and within
    the button bound. The stored title is already one line; whitespace is normalised again
    because this is a display string built from data."""
    return _bounded_label(" ".join(title.split()) or favorites.UNTITLED)


def _favorites_page(data: Any) -> Optional[int]:
    """The page number named by a favorites-list button, or None when the callback is not
    one this bot writes. Only the exact shape is accepted; a page beyond the largest possible
    list is not a page."""
    if not isinstance(data, str):
        return None
    found = _FAVORITES_LIST_CALLBACK.fullmatch(data)
    if found is None:
        return None
    page = int(found.group(1))
    return page if page < MAX_FAVORITE_PAGES else None


def _favorite_target(data: Any) -> Optional[tuple[UUID, int]]:
    """(favorite id, page to return to) named by a recipe button, or None.

    The id is only a name to look up for the sender's user; it is never an authorization.
    Only the exact shape this bot writes is accepted, and the page is bounded like a list
    page."""
    if not isinstance(data, str):
        return None
    found = _FAVORITE_OPEN_CALLBACK.fullmatch(data)
    if found is None:
        return None
    page = int(found.group(2))
    if page >= MAX_FAVORITE_PAGES:
        return None
    try:
        return UUID(hex=found.group(1)), page
    except ValueError:  # not reachable with the 32 hex digits the pattern requires
        return None


def _favorites_view(
    kept: list[favorites.FavoriteSummary], page: int
) -> tuple[str, Optional[dict[str, Any]]]:
    """The message and keyboard for one page of the user's favorites.

    The order is the one list_favorites returns (newest first, ties by id): the page is a
    window on that list, so the same list always gives the same buttons in the same order. A
    page that no longer exists (favorites were removed since its button was sent) falls back
    to the last page; an empty list is the friendly empty state with no keyboard.
    """
    if not kept:
        return FAVORITES_EMPTY, None
    pages = (len(kept) + FAVORITES_PAGE_SIZE - 1) // FAVORITES_PAGE_SIZE
    page = max(0, min(page, pages - 1))
    start = page * FAVORITES_PAGE_SIZE
    lines = [FAVORITES_HEADER]
    if pages > 1:
        lines.append(f"Страница {page + 1} из {pages}.")
    lines.extend(["", FAVORITES_HINT])
    rows = [
        [
            {
                "text": _favorite_label(item.title),
                "callback_data": f"{FAVORITES_OPEN_PREFIX}:{item.id.hex}:{page}",
            }
        ]
        for item in kept[start : start + FAVORITES_PAGE_SIZE]
    ]
    navigation = []
    if page > 0:
        navigation.append(
            {"text": "◀ Назад", "callback_data": f"{FAVORITES_LIST_PREFIX}:{page - 1}"}
        )
    if page < pages - 1:
        navigation.append(
            {"text": "Далее ▶", "callback_data": f"{FAVORITES_LIST_PREFIX}:{page + 1}"}
        )
    if navigation:
        rows.append(navigation)
    return "\n".join(lines), {"inline_keyboard": rows}


def _favorite_back(page: int) -> dict[str, Any]:
    """The keyboard under a saved recipe: one way back to the page it was opened from."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": FAVORITES_BACK,
                    "callback_data": f"{FAVORITES_LIST_PREFIX}:{page}",
                }
            ]
        ]
    }


def _open_favorites(page: int = 0) -> dict[str, Any]:
    """The keyboard of the saved-recipe confirmation: one way to the list."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": FAVORITES_OPEN,
                    "callback_data": f"{FAVORITES_LIST_PREFIX}:{page}",
                }
            ]
        ]
    }


def _save_keyboard(recipes: tuple[StoredRecipe, ...]) -> Optional[dict[str, Any]]:
    """The inline keyboard under an answer that carries recipes: one button per recipe, in
    answer order. None when there are none, so an ordinary answer has no controls at all."""
    if not recipes:
        return None
    multiple = len(recipes) > 1
    return {
        "inline_keyboard": [
            [
                {
                    "text": _save_button_label(recipe.title, multiple=multiple),
                    "callback_data": (
                        f"{SAVE_CALLBACK_PREFIX}:{recipe.message_id}:{recipe.position}"
                    ),
                }
            ]
            for recipe in recipes
        ]
    }


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
            # The menu is registered once per start, beside polling (never inside it): a
            # failed menu call must not delay or stop reading updates, and a restart simply
            # applies the same whole list again. It is tracked like a handler so that
            # closing waits for it in the same way.
            self._spawn(self._register_commands())

    async def _register_commands(self) -> None:
        """Set the bot's native command menu. Best effort: a refusal or a network failure is
        one warning (the error's category only), the previous menu stays, and polling is not
        affected. The list is fixed, so every call has the same result."""
        try:
            await self._api.set_my_commands(BOT_COMMANDS)
        except asyncio.CancelledError:
            raise
        except BotApiError as error:
            logger.warning(
                "The Telegram command menu was not set: category=%s", error.category
            )
        except Exception as exc:
            logger.error(
                "The Telegram command menu was not set: category=%s", type(exc).__name__
            )
        else:
            logger.info("The Telegram command menu is set")

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
            if isinstance(update, dict) and "callback_query" in update:
                await self._handle_callback(update.get("callback_query"))
                return
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

    # --- a click on a save button ---------------------------------------------------------------

    @staticmethod
    def _favorites_callback(data: Any) -> bool:
        """True when `data` names a favorites action (a list page or a recipe), whether or not
        its shape is valid. Used only to choose the fixed notice for a click this bot cannot
        use, never to decide anything about identity."""
        return isinstance(data, str) and (
            data.startswith(FAVORITES_LIST_PREFIX + ":")
            or data.startswith(FAVORITES_OPEN_PREFIX + ":")
        )

    async def _handle_callback(self, query: Any) -> None:
        """One callback query: save a recipe, open the favorites list or open one favorite,
        always for the user its SENDER is tied to, and acknowledge the click in every case.

        The callback data is only a name -- (message id, recipe position), a page number, or
        (favorite id, page to return to). It is not trusted for identity: the account comes
        from the update, the user from the current Telegram link, and everything read from
        the database is found through that user. A click on somebody else's button, a recipe
        deleted since, an account with no link and a malformed payload are all answered with
        fixed Russian words and change nothing. None of these paths calls the assistant.

        Never raises (cancellation excepted). A callback is answered even when nothing was
        done, so a client's button never spins forever."""
        if not isinstance(query, dict):
            return
        callback_id = query.get("id")
        if not isinstance(callback_id, str) or not 0 < len(callback_id) <= 256:
            return  # there is nothing to answer with
        data = query.get("data")
        favorites_callback = self._favorites_callback(data)
        account, usable = self._callback_sender(query)
        if account is None or not usable:
            await self._answer_callback(
                callback_id, FAVORITES_UNAVAILABLE if favorites_callback else RECIPE_UNAVAILABLE
            )
            return
        try:
            bound = await self._bound_user(account)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("A Telegram callback could not be authorized: category=%s", type(exc).__name__)
            await self._answer_callback(
                callback_id, FAVORITES_UNAVAILABLE if favorites_callback else SAVE_FAILED
            )
            return
        if bound is None:
            await self._answer_callback(callback_id, CALLBACK_NOT_LINKED)
            return

        if isinstance(data, str) and data.startswith(FAVORITES_LIST_PREFIX + ":"):
            page = _favorites_page(data)
            if page is None:
                await self._answer_callback(callback_id, FAVORITES_UNAVAILABLE)
                return
            try:
                kept = await self._read_favorites(bound)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "The Telegram favorites list could not be read: category=%s",
                    type(exc).__name__,
                )
                await self._answer_callback(callback_id, FAVORITES_UNAVAILABLE)
                return
            text, markup = _favorites_view(kept, page)
            # Acknowledge before waiting for the chat's turn: an answer being prepared holds
            # that lock for the provider call, and the client's button must not spin meanwhile.
            await self._answer_callback(callback_id, "")
            try:
                await self._send_favorites(account, bound, text, markup)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "A Telegram favorites list could not be sent: category=%s",
                    type(exc).__name__,
                )
            return
        if isinstance(data, str) and data.startswith(FAVORITES_OPEN_PREFIX + ":"):
            target = _favorite_target(data)
            if target is None:
                await self._answer_callback(callback_id, FAVORITE_UNAVAILABLE)
                return
            favorite_id, page = target
            try:
                found = await self._read_favorite(bound, favorite_id)
            except favorites.FavoriteNotFound:
                await self._answer_callback(callback_id, FAVORITE_UNAVAILABLE)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "A Telegram favorite could not be read: category=%s", type(exc).__name__
                )
                await self._answer_callback(callback_id, FAVORITES_UNAVAILABLE)
                return
            if not found.text.strip():
                await self._answer_callback(callback_id, FAVORITE_UNAVAILABLE)
                return
            await self._answer_callback(callback_id, "")
            try:
                await self._send_favorite(account, bound, found.text, page)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "A Telegram favorite could not be sent: category=%s", type(exc).__name__
                )
            return
        await self._save_from_callback(callback_id, account, bound, data)

    async def _read_favorites(self, user_id: UUID) -> list[favorites.FavoriteSummary]:
        """The user's favorites through the website's own read service. A snapshot read;
        writes nothing and calls no provider."""
        return await self._database.run(
            lambda session: favorites.list_favorites(session, user_id)
        )

    async def _read_favorite(self, user_id: UUID, favorite_id: UUID) -> favorites.FavoriteDetail:
        """One favorite through the website's own read service. FavoriteNotFound when it is
        not this user's (unknown, foreign, removed)."""
        return await self._database.run(
            lambda session: favorites.get_favorite(session, user_id, favorite_id)
        )

    async def _send_favorites(
        self,
        account: int,
        user_id: UUID,
        text: str,
        markup: Optional[dict[str, Any]],
    ) -> None:
        """Send one page of the list to the user's chat, under the chat lock and the binding
        check all owned sends use. A tie that is gone is refused and said."""
        async with self._chat_lock(account):
            if await self._deliver(account, user_id, text, reply_markup=markup) is Delivery.REFUSED:
                await self._refuse_stale(account)

    async def _send_favorite(
        self, account: int, user_id: UUID, text: str, page: int
    ) -> None:
        """Send one favorite's complete saved text, with the way back to `page`."""
        async with self._chat_lock(account):
            if await self._deliver(
                account, user_id, text, reply_markup=_favorite_back(page)
            ) is Delivery.REFUSED:
                await self._refuse_stale(account)

    async def _save_from_callback(
        self, callback_id: str, account: int, user_id: UUID, data: Any
    ) -> None:
        """Save the recipe the callback names, then acknowledge it and, for a recipe that was
        not already a favorite, send the persistent confirmation.

        `user_id` is the owner this click was authorized for. The saving transaction resolves
        the tie again and must find that same owner; a tie that changed in between refuses the
        save (favorites.BindingChanged) instead of saving under the new owner and confirming
        under the old one. The owner the transaction validated comes back with the result and
        is the identity the confirmation is sent under. This method changes nothing itself."""
        target = _save_target(data)
        if target is None:
            await self._answer_callback(callback_id, RECIPE_UNAVAILABLE)
            return
        message_id, position = target
        now = self._clock()
        try:
            saved = await self._database.run(
                lambda session: favorites.save_recipe_for_telegram(
                    session,
                    account,
                    message_id,
                    expected_user_id=user_id,
                    recipe_index=position,
                    now=now,
                )
            )
        except favorites.NotBound:
            await self._answer_callback(callback_id, CALLBACK_NOT_LINKED)
            return
        except favorites.BindingChanged:
            # The click was authorized as one user and the tie inside the saving transaction
            # is another's. Nothing was saved or read; the fixed words carry nothing of either
            # account, and no confirmation follows.
            logger.info("A Telegram recipe save was refused: the tie changed under the click")
            await self._answer_callback(callback_id, RECIPE_UNAVAILABLE)
            return
        except (MessageNotFound, RecipeNotFound, ConversationNotFound):
            await self._answer_callback(callback_id, RECIPE_UNAVAILABLE)
            return
        except favorites.FavoritesFull:
            await self._answer_callback(callback_id, FAVORITES_FULL)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("A Telegram recipe could not be saved: category=%s", type(exc).__name__)
            await self._answer_callback(callback_id, SAVE_FAILED)
            return
        logger.info(
            "Telegram recipe save: outcome=%s", "created" if saved.created else "already"
        )
        await self._answer_callback(callback_id, SAVED if saved.created else ALREADY_SAVED)
        if saved.created:
            # saved.user_id is the owner the saving transaction resolved and validated against
            # the click's; the confirmation is sent on that identity, not on one looked up
            # before the save.
            await self._confirm_saved(account, saved.user_id, saved.favorite.title)

    async def _confirm_saved(self, account: int, user_id: UUID, title: str) -> None:
        """One short, persistent confirmation after a new favorite: the recipe's own title and
        a button that opens the list. `user_id` is the owner the save was made for. The answer's
        keyboard is deliberately not rewritten (see the module docstring); the tie is checked
        again by _deliver before the message goes out, so a title is never sent to whoever the
        account belongs to after a change."""
        text = f"Рецепт «{title}» сохранен в избранное."
        try:
            async with self._chat_lock(account):
                outcome = await self._deliver(
                    account, user_id, text, reply_markup=_open_favorites()
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The favorite is already saved and acknowledged: a confirmation that could not
            # be sent is a warning, not a failure of the click.
            logger.error(
                "A saved-recipe confirmation could not be sent: category=%s",
                type(exc).__name__,
            )
            return
        if outcome is Delivery.REFUSED:
            logger.info("A saved-recipe confirmation was not sent: the chat is no longer the user's")

    @staticmethod
    def _callback_sender(query: dict[str, Any]) -> tuple[Optional[int], bool]:
        """(account, usable) for a callback query. The account is the Bot API's sender id.
        `usable` is false for anything that is not a private chat of that same sender: the
        bot serves people only there, and a click that did not come from such a chat is
        answered without being used."""
        sender, message = query.get("from"), query.get("message")
        if not isinstance(sender, dict) or not isinstance(message, dict):
            return None, False
        account = _count(sender.get("id"))
        chat = message.get("chat")
        if account is None or sender.get("is_bot") is True:
            return account, False
        if not isinstance(chat, dict) or chat.get("type") != "private" or chat.get("id") != account:
            return account, False
        return account, True

    async def _answer_callback(self, callback_id: str, text: str) -> None:
        """Acknowledge one callback, showing `text` as its short notice. A failure to reach
        Telegram is logged by category and changes nothing else; it never raises."""
        try:
            await self._api.answer_callback_query(callback_id, text)
        except BotApiError as error:
            logger.warning("Telegram did not take a callback answer: category=%s", error.category)
        except Exception as exc:  # a bug; the click's outcome has already been decided
            logger.error("A callback answer could not be sent: category=%s", type(exc).__name__)

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
        elif name == "favorites":
            try:
                kept = await self._read_favorites(user_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "The Telegram favorites list could not be read: category=%s",
                    type(exc).__name__,
                )
                await self._say(account, FAVORITES_UNAVAILABLE)
                return
            text, markup = _favorites_view(kept, 0)
            await self._send_favorites(account, user_id, text, markup)
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
            if await self._deliver(
                account,
                user_id,
                result.completed.assistant_message.text,
                reply_markup=_save_keyboard(result.completed.recipes),
            ) is Delivery.REFUSED:
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
                    account,
                    completion.user_id,
                    FROM_WEBSITE + completion.user_text,
                    completion.reply_text,
                    reply_markup=_save_keyboard(completion.recipes),
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

    async def _deliver(
        self,
        account: int,
        user_id: UUID,
        *texts: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> Delivery:
        """Send `texts`, which belong to `user_id`, to the chat of `account`, but only while that
        account is still the enrolled sender of that user. The question and the sends are made
        under the binding lock, which unlinking takes too: the tie cannot be taken apart (and the
        account given to somebody else) between the check and the send. Nothing is sent, and
        REFUSED returned, when the tie is gone. `reply_markup` (an inline keyboard) goes under
        the last text, which is the answer the buttons belong to."""
        async with self._binding(account):
            if await self._bound_user(account) != user_id:
                return Delivery.REFUSED
            for number, text in enumerate(texts):
                markup = reply_markup if number == len(texts) - 1 else None
                if not await self._say(account, text, reply_markup=markup):
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

    async def _say(
        self, account: int, text: str, *, reply_markup: Optional[dict[str, Any]] = None
    ) -> bool:
        """Send `text` (in pieces if it is long). False, with the reason logged, when
        Telegram refused or could not be reached; never raises a Bot API error. `reply_markup`
        is attached to the last piece, so the controls sit under the end of the text."""
        pieces = split_text(text)
        for number, piece in enumerate(pieces):
            try:
                await self._api.send_message(
                    account,
                    piece,
                    reply_markup=reply_markup if number == len(pieces) - 1 else None,
                )
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
