"""The web application: a FastAPI process that is separate from the Telegram bot.

It is a transport, like bot.py: it may use house_helper.core (the chat core) and
house_helper.persistence, and neither may import it or FastAPI. It must not
import the Telegram transport or provider configuration (bot, config, api_client,
aiogram, openai) or read a .env file, so it starts, and is imported, without a bot
token or an API key. Both directions are pinned by tests/test_web_boundary.py.

Nothing here runs at import time except house_helper.web.asgi, whose only job is
to build the application object a server needs. The database engine is created
when the application starts and disposed when it stops (see app.py).
"""
