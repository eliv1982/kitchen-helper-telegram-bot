"""The web application: a FastAPI process that is separate from the Telegram bot.

It is a transport, like bot.py: it may use house_helper.core (the chat core) and
house_helper.persistence, and neither may import it or FastAPI. It must not
import the Telegram transport or provider configuration (bot, config, api_client,
aiogram, openai) or read a .env file, so it starts, and is imported, without a bot
token or an API key. Both directions are pinned by tests/test_web_boundary.py.

Nothing here runs at import time except house_helper.web.asgi, whose only job is
to build the application object a server needs. The database engine is created
when the application starts and disposed when it stops (see app.py).

Sign-in with Telegram (OIDC) is optional and lives in auth.py (the routes, the
require_user dependency), telegram_oidc.py (the provider side) and logsafety.py;
settings.py says how it is configured and what happens when it is not. It is the
only way into the application: every route that serves a user's data must depend on
auth.require_user, which resolves the user from the server-side session and never
from anything the client sends.

The chat API (chat_api.py: the conversations and their messages under /api) is the
first such route set. turn_runner.py answers a message durably (the database, not the
process, holds the conversation; the AI is called with no transaction open and a
disconnecting client does not interrupt the turn), and diagnostics.py is the catalogue
of its errors (stable codes, Russian messages, server-made correlation ids). The AI
comes from the same settings as the Telegram bot's, minus its token (settings.py), and
is built when the application starts but connects only on the first message.
"""
