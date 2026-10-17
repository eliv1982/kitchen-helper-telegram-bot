"""The ASGI entry point:

    uvicorn house_helper.web.asgi:app

The one module here that does something when imported, and only this: it builds
the application object. It reads HOUSE_HELPER_DATABASE_URL and the optional
Telegram login variables (and fails to import, so the server does not start, if
either is unusable, a half-configured login included; see settings.py) but opens
no connection and creates no file; see app.py. Anything that wants an application of its own, a
test for one, imports create_app instead.
"""

from house_helper.web.app import create_app

app = create_app()
