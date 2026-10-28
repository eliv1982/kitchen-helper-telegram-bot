"""Connecting a Telegram account to the signed-in person: three routes under /api/telegram.

  GET    /api/telegram        whether the bot exists here, whether it is reading Telegram, and
                              whether this person has linked an account: {"enabled", "linked",
                              "available"?, "bot_username"?, "account"?: {"name", "linked_at"}}
  POST   /api/telegram/link   a pairing code for this person (body: {}):
                              {"code", "link", "expires_at", "expires_in_seconds"}. The link is
                              https://t.me/<bot>?start=<code>; opening it in Telegram and pressing
                              Start is the second half of the pairing (telegram_chat.py). 409
                              telegram_already_linked if an account is already linked; 503
                              telegram_unavailable while the bot is not reading Telegram.
  DELETE /api/telegram/link   unlink (idempotent): {"linked": false}. 503 service_unavailable
                              (retryable) only if the link changed hands again and again while
                              it was being removed (TelegramChat.unlink); nothing is removed then.

Who. The user is whoever the session cookie resolves to (the chat API's rule, and the same
errors, correlation ids and headers: these routes are ChatRoute routes). The code is made for
that user and for nobody else; the Telegram account that presents it never says whose it is.
A mutation must come from this site (the Origin rule of the chat API), so a page on another
site cannot make a code for a visitor or unlink their account.

The code is a secret for ten minutes: it is shown to the person who asked, once, and stored only
as a digest. It is never put in a log line, and the responses are `no-store`.

Without the bot configured the status answers {"enabled": false, "linked": false} and the other
two routes answer 503 telegram_unavailable.

"available" is false while the bot's polling is failing (a refused token, another consumer or a
webhook holding the token, no route to Telegram: TelegramChat.failure). The page then says the bot
is not answering instead of showing it as connected, and no code is issued for a bot that would
not read the message that presents it. Unlinking needs only the database and still works; it goes
through the bot (TelegramChat.unlink) so that it is serialised with the bot's sends to that account.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from house_helper.persistence import telegram as store
from house_helper.persistence.authn import AuthenticatedSession
from house_helper.web.chat_api import ChatRoute, _read_json_object, authenticated, authenticated_mutation
from house_helper.web.database import WebDatabase
from house_helper.web.dependencies import get_database
from house_helper.web.diagnostics import ApiError
from house_helper.web.settings import BotSettings

router = APIRouter(prefix="/api/telegram", route_class=ChatRoute)


def _bot(request: Request) -> Optional[BotSettings]:
    """The bot's settings, if the bot is running in this process."""
    if getattr(request.app.state, "telegram", None) is None:
        return None
    return request.app.state.settings.telegram_bot


@router.get("")
async def status(
    request: Request,
    current: AuthenticatedSession = Depends(authenticated),
    database: WebDatabase = Depends(get_database),
) -> Response:
    bot = _bot(request)
    if bot is None:
        return JSONResponse({"enabled": False, "linked": False})
    link = await database.run(lambda session: store.get_link(session, current.user_id))
    body: dict = {
        "enabled": True,
        "available": request.app.state.telegram.failure is None,
        "linked": link is not None,
        "bot_username": bot.username,
    }
    if link is not None:
        body["account"] = {"name": link.display_name, "linked_at": link.created_at.isoformat()}
    return JSONResponse(body)


@router.post("/link")
async def create_link(
    request: Request,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    bot = _bot(request)
    if bot is None:
        raise ApiError("telegram_unavailable", category="NotConfigured")
    if request.app.state.telegram.failure is not None:
        raise ApiError("telegram_unavailable", category="PollingFailed")
    if await _read_json_object(request):
        raise ApiError("invalid_request", category="UnexpectedFields")
    now = request.app.state.clock()
    try:
        issued = await database.run(
            lambda session: store.issue_link_code(session, current.user_id, now=now)
        )
    except store.AlreadyLinked:
        raise ApiError("telegram_already_linked") from None
    except store.UserUnavailable:
        raise ApiError("not_authenticated", category="UserUnavailable") from None
    return JSONResponse(
        {
            "code": issued.code,
            "link": f"{bot.link_base}?start={issued.code}",
            "expires_at": issued.expires_at.isoformat(),
            "expires_in_seconds": int(store.CODE_LIFETIME.total_seconds()),
        },
        status_code=201,
    )


@router.delete("/link")
async def delete_link(
    request: Request,
    current: AuthenticatedSession = Depends(authenticated_mutation),
) -> Response:
    if _bot(request) is None:
        raise ApiError("telegram_unavailable", category="NotConfigured")
    # Through the bot, not straight to the database: the link is taken apart under the lock
    # that the bot holds while it checks that an account is still this person's and sends
    # them something (telegram_chat.py, "Who is served, at the moment of serving").
    await request.app.state.telegram.unlink(current.user_id)
    return JSONResponse({"linked": False})
