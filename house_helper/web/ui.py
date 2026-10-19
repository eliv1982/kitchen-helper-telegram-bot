"""The page: the HTML shell of the kitchen web interface, and its static files.

Two routes, both public (the page holds nothing of anyone's: who the visitor is, and
what they may see, is decided by the /auth and /api routes the page calls):

  GET /                  the shell, made by the server for this request
  GET /static/<name>     one of a fixed few files: the script, the logic, the style, the icon

The shell is the one dynamic thing. It carries, as data attributes of <body>, the
non-sensitive facts the page cannot learn any other way before it has signed anyone in:
which ways of signing in are configured here (Telegram, email -- a login choice is drawn
only if it exists, so no button leads to a 503), and the limit on the length of a
message. Nothing else comes from the configuration: no secret, no URL, no client id, no
provider name, no address of a server. The same markup is drawn for everybody.

No inline script or style. The page may load script, style and the icon from this
origin, may call this origin and nothing else, may not be framed, may not submit a form
(it has no form that is submitted natively: the script handles them, and without it
nothing may leave the page), and may not set a <base>:

  default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self';
  connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'

Its referrer policy is same-origin, as the email pages' (see email_pages.py): for a
fetch() made with the default mode the browser sends the real Origin on a POST whatever
the policy, and the policy keeps a form-like request from sending "Origin: null". The
page never sets a fetch mode of its own for that reason.

The static files are served from memory-sized reads of the files next to this module,
by a whitelist of names (there is no path to traverse), with an ETag and
`Cache-Control: no-cache`, so a changed file is picked up on the next load and an
unchanged one costs a 304.
"""

import hashlib
import html
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Response

from house_helper.core.limits import MAX_INPUT_LENGTH

STATIC_DIR = Path(__file__).parent / "static"

# The published name and the media type of every file under /static. Nothing else is served.
ASSETS: dict[str, str] = {
    "app.css": "text/css; charset=utf-8",
    "chat-logic.js": "text/javascript; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
}

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
)

PAGE_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=(), payment=()",
    "Content-Language": "ru",
}

ASSET_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}

router = APIRouter()


# --- the shell ----------------------------------------------------------------------------

_TELEGRAM = """\
    <a id="login-telegram" class="btn btn-primary btn-block" href="/auth/telegram/login">Войти через Telegram</a>
"""

_DIVIDER = """\
    <p class="divider"><span>или</span></p>
"""

_EMAIL = """\
    <form id="email-form" class="stack" novalidate>
      <label for="email-input">Электронная почта</label>
      <input id="email-input" name="email" type="email" inputmode="email" autocomplete="email"
             autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="320"
             aria-describedby="email-hint email-result">
      <p id="email-hint" class="hint">Мы отправим на этот адрес ссылку для входа. Она действует недолго и открывается один раз.</p>
      <button id="email-submit" class="btn btn-secondary btn-block" type="submit">Получить ссылку для входа</button>
      <p id="email-result" class="notice" role="status" hidden></p>
    </form>
"""

_NO_LOGIN = """\
    <p id="login-none" class="notice notice-error">Вход пока не настроен. Обратитесь к тому, кто установил помощника.</p>
"""

_SHELL = """\
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<meta name="color-scheme" content="light">
<meta name="theme-color" content="#1c3f94">
<title>Кухонный помощник</title>
<link rel="icon" type="image/svg+xml" href="/static/icon.svg">
<link rel="stylesheet" href="/static/app.css">
</head>
<body data-telegram="@TELEGRAM_FLAG@" data-email="@EMAIL_FLAG@" data-max-length="@MAX_LENGTH@">
<div id="announcer" class="visually-hidden" role="status" aria-live="polite" aria-atomic="true"></div>
<div id="alerter" class="visually-hidden" role="alert" aria-atomic="true"></div>
<noscript>
  <div class="card card-center"><h1>Кухонный помощник</h1>
  <p>Для работы помощника нужен включённый JavaScript. Включите его в настройках браузера и обновите страницу.</p></div>
</noscript>

<main id="gate" class="gate">
  <section id="screen-loading" class="card card-center" aria-label="Загрузка">
    <p class="loading"><span class="spinner" aria-hidden="true"></span><span id="loading-text">Загружаем…</span></p>
  </section>

  <section id="screen-unavailable" class="card card-center" aria-labelledby="unavailable-title" hidden>
    <h1 id="unavailable-title" tabindex="-1">Кухонный помощник</h1>
    <p id="unavailable-text" class="notice notice-error" role="alert"></p>
    <p id="unavailable-id" class="hint" hidden></p>
    <button id="unavailable-retry" class="btn btn-primary" type="button">Повторить</button>
  </section>

  <section id="screen-login" class="card card-center" aria-labelledby="login-title" hidden>
    <h1 id="login-title" tabindex="-1">Кухонный помощник</h1>
    <p class="lead">Помощник по домашней кухне: подскажет, что приготовить из того, что есть дома, и расскажет, как приготовить блюдо.</p>
    <div id="login-notice" class="notice" role="status" hidden></div>
@LOGIN@  </section>
</main>

<div id="screen-app" class="app" data-drawer="closed" hidden>
  <header class="topbar">
    <button id="drawer-toggle" class="btn btn-onblue drawer-toggle" type="button" aria-controls="sidebar" aria-expanded="false"><span class="burger" aria-hidden="true"></span>Разговоры</button>
    <p class="brand">Кухонный помощник</p>
    <button id="logout" class="btn btn-onblue" type="button">Выйти</button>
  </header>
  <div class="workspace">
    <nav id="sidebar" class="sidebar" aria-label="Разговоры">
      <button id="new-conversation" class="btn btn-secondary btn-block" type="button">Новый разговор</button>
      <p id="list-status" class="hint" role="status"></p>
      <button id="list-retry" class="btn btn-quiet btn-block" type="button" hidden>Обновить список</button>
      <ul id="conversation-list" class="conv-list"></ul>
    </nav>
    <div id="backdrop" class="backdrop" hidden></div>
    <main id="chat" class="chat" aria-labelledby="chat-title" hidden>
      <h1 id="chat-title" class="chat-title" tabindex="-1"></h1>
      <div id="banner" class="banner" hidden>
        <p id="banner-text"></p>
        <button id="banner-close" class="btn btn-quiet" type="button">Закрыть</button>
      </div>
      <div id="messages" class="messages" role="log" aria-live="off" aria-label="Сообщения разговора" tabindex="0"></div>
      <form id="composer" class="composer" novalidate>
        <label class="visually-hidden" for="composer-input">Ваше сообщение</label>
        <div class="composer-row">
          <textarea id="composer-input" name="message" rows="1" lang="ru" autocomplete="off"
                    placeholder="Спросите о блюде…"
                    aria-describedby="composer-hint composer-error"></textarea>
          <button id="send" class="btn btn-primary btn-send" type="submit">Отправить</button>
        </div>
        <p id="composer-error" class="field-error" role="alert" hidden></p>
        <p class="composer-meta"><span><span id="composer-hint" class="hint"></span> <span class="hint hint-keys">Ctrl+Enter — отправить</span></span><span id="counter" class="hint counter" aria-hidden="true"></span></p>
      </form>
    </main>
  </div>
</div>

<script src="/static/chat-logic.js"></script>
<script src="/static/app.js"></script>
</body>
</html>
"""


def render_shell(*, telegram: bool, email: bool, max_length: int = MAX_INPUT_LENGTH) -> str:
    """The shell for a deployment with these ways of signing in. The flags are the only
    thing that varies; each draws its own markup, or nothing."""
    if telegram and email:
        login = _TELEGRAM + _DIVIDER + _EMAIL
    elif telegram:
        login = _TELEGRAM
    elif email:
        login = _EMAIL
    else:
        login = _NO_LOGIN
    return (
        _SHELL.replace("@TELEGRAM_FLAG@", "1" if telegram else "0")
        .replace("@EMAIL_FLAG@", "1" if email else "0")
        .replace("@MAX_LENGTH@", html.escape(str(int(max_length)), quote=True))
        .replace("@LOGIN@", login)
    )


def _login_methods(request: Request) -> tuple[bool, bool]:
    """(Telegram, email) as they are running now: the services the lifespan built, not
    the settings (a method whose service is not there would answer 503)."""
    auth = getattr(request.app.state, "auth", None)
    email = getattr(request.app.state, "email", None)
    return (auth is not None and auth.telegram_enabled), email is not None


@router.get("/")
def page(request: Request) -> Response:
    telegram, email = _login_methods(request)
    return Response(
        render_shell(telegram=telegram, email=email),
        media_type="text/html",
        headers=PAGE_HEADERS,
    )


# --- the static files ---------------------------------------------------------------------


def _read_asset(name: str) -> Optional[bytes]:
    if name not in ASSETS:
        return None
    try:
        return (STATIC_DIR / name).read_bytes()
    except OSError:
        return None


def _etag(content: bytes) -> str:
    return '"' + hashlib.sha256(content).hexdigest()[:32] + '"'


def _matches(header: Optional[str], etag: str) -> bool:
    """True if an If-None-Match header names `etag` (weak or strong) or says "*"."""
    if header is None:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == etag:
            return True
    return False


@router.get("/static/{name}")
def static_file(name: str, request: Request) -> Response:
    content = _read_asset(name)
    if content is None:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    etag = _etag(content)
    headers = {**ASSET_HEADERS, "ETag": etag}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content, media_type=ASSETS[name], headers=headers)
