"""The pages of the Telegram invitation sign-in, as plain strings: no templates, no assets.

An invitation reaches a person as a link, https://<public url>/auth/invite#<id>.<secret>.
The secret is in the URL fragment, which a browser never sends to a server: not in the
request line, not in an access log, not in a Referer header, not through a redirect. Only
script can read it, so the page has one inline script, and like the emailed sign-in link's
page (email_pages.py) it must work without anything a security reviewer would have to trust:
no third-party script, font, stylesheet or image, one inline script and one inline style,
each allowed by a Content-Security-Policy that names it by the SHA-256 of its text (not
"unsafe-inline"), and that lets the script call this origin and nothing else.

What the script does. It takes the code out of the fragment and out of the address bar at
once (history.replaceState), keeps it in a variable of its own (never in localStorage,
sessionStorage, a cookie or the page), and enables a button. Nothing is sent until the
person presses it: the code then goes to this origin's POST /auth/invite/start in the body
of a JSON request, and what comes back is the address at Telegram to continue at, which the
script opens. Opening the page therefore uses nothing up (a link preview that fetches it
is harmless, and a fetch cannot even see the fragment), and the invitation stays unused
until a verified Telegram identity has come back and been linked (web/auth.py).

The other pages here are what the callback shows when a sign-in that began with an
invitation ends badly, and the page for the one that ends well, plus the one page of an
ordinary Telegram sign-in (login_other_user_page: the browser is signed in as somebody
else). They are fixed texts: the reason is told in words that are the same whatever the
cause behind them, and nothing about an invitation, a user or an identity is put into them.

Every page is sent with Cache-Control: no-store and X-Content-Type-Options: nosniff, may not
be framed, and carries Referrer-Policy: same-origin, not no-referrer, for the reason given
in email_pages.py (a browser sends "Origin: null" with a request made under no-referrer in
some cases, which the start endpoint would have to refuse).
"""

import base64
import hashlib
import html
from typing import Optional

from house_helper.web.email_pages import PRIVATE_HEADERS, Page

START_PATH = "/auth/invite/start"
APPLICATION_PATH = "/"

# The text of the script and the style, exactly as served: the CSP hash is a hash of these
# bytes. The pattern is invitations.parse_invitation_code's, in JavaScript.
_SCRIPT = """
(function () {
  "use strict";
  var PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\.[A-Za-z0-9_-]{43}$/;
  var TELEGRAM = "https://oauth.telegram.org/auth?";
  var button = document.getElementById("go");
  var status = document.getElementById("status");
  var code = window.location.hash.slice(1);
  var removed = false;
  try {
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    removed = window.location.hash === "";
  } catch (ignored) {}
  function say(text, problem) {
    status.textContent = text;
    status.className = problem ? "problem" : "note";
    status.hidden = false;
  }
  if (!removed) {
    code = "";
    button.disabled = true;
    say("Эта страница не может безопасно обработать приглашение: не удалось убрать код из адресной строки. Закройте вкладку и откройте ссылку в другом браузере или попросите выдать новое приглашение.", true);
    return;
  }
  if (!PATTERN.test(code)) {
    code = "";
    say("Ссылка-приглашение неполная или повреждена. Откройте её из сообщения целиком или попросите новую.", true);
    return;
  }
  var busy = false;
  var leaving = false;
  function again(text) {
    busy = false;
    button.disabled = false;
    say(text, true);
  }
  function finished(text) {
    code = "";
    say(text, true);
  }
  function answered(result) {
    var data = result.data;
    if (result.status === 200 && data && typeof data.authorization_url === "string" &&
        data.authorization_url.indexOf(TELEGRAM) === 0) {
      leaving = true;
      window.location.assign(data.authorization_url);
      return;
    }
    var text = data && typeof data.message === "string" && data.message.length <= 500
      ? data.message
      : "Не получилось начать вход. Попробуйте ещё раз чуть позже.";
    if (result.status === 400 || result.status === 403) {
      finished(text);
    } else {
      again(text);
    }
  }
  button.disabled = false;
  button.addEventListener("click", function () {
    if (busy) {
      return;
    }
    busy = true;
    button.disabled = true;
    say("Открываем Telegram…", false);
    window.fetch("/auth/invite/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code }),
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error"
    }).then(function (response) {
      return response.json().then(
        function (data) { return { status: response.status, data: data }; },
        function () { return { status: response.status, data: null }; }
      );
    }).then(answered, function () {
      again("Нет связи с сервером. Проверьте интернет и нажмите кнопку ещё раз.");
    });
  });
  window.addEventListener("pageshow", function (event) {
    if (event.persisted && leaving && code) {
      leaving = false;
      again("Вход через Telegram не завершён. Чтобы повторить, нажмите кнопку ещё раз.");
    }
  });
})();
"""

# The look of the application's own login card (static/app.css: the same colours, type, card and
# main button), so that a person who goes from the invitation to the application does not seem to
# have changed site. Light only, as the application is. Inline, because the page loads no file.
_STYLE = """
:root { color-scheme: light; }
*, *::before, *::after { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; padding: 1.5rem 1rem; background: #f4f7fc; color: #162033; font: 1.0625rem/1.55 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
main { max-width: 28rem; margin: 0 auto; padding: 1.5rem; background: #ffffff; border: 1px solid #b9c9e4; border-radius: 1rem; box-shadow: 0 1px 3px rgba(22, 32, 51, 0.12); }
h1 { margin: 0 0 0.75rem; font-size: 1.5rem; line-height: 1.25; }
p { margin: 0 0 0.75rem; }
button, a.button { display: flex; align-items: center; justify-content: center; width: 100%; min-height: 2.75rem; padding: 0.55rem 1.1rem; border: 2px solid transparent; border-radius: 0.75rem; background: #1c3f94; color: #ffffff; font: inherit; font-weight: 600; line-height: 1.2; text-align: center; text-decoration: none; cursor: pointer; }
button:hover, a.button:hover { background: #142f73; }
button[disabled] { cursor: not-allowed; opacity: 0.6; }
:focus-visible { outline: 3px solid #1c3f94; outline-offset: 2px; }
.note { color: #44546a; font-size: 0.9rem; }
.problem { padding: 0.7rem 0.9rem; border: 1px solid #b3261e; border-radius: 0.75rem; background: #fdeceb; color: #8f1d17; }
"""


def _sha256_source(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def _policy(*, script: bool) -> str:
    directives = [
        "default-src 'none'",
        f"style-src {_sha256_source(_STYLE)}",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
    if script:
        directives.insert(1, f"script-src {_sha256_source(_SCRIPT)}")
        directives.insert(2, "connect-src 'self'")
    return "; ".join(directives)


def _document(
    title: str, body: str, *, script: bool, status_code: int, code: Optional[str] = None
) -> Page:
    text = (
        "<!doctype html>\n"
        '<html lang="ru">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n"
        "<body>\n"
        + (f'<main data-code="{html.escape(code, quote=True)}">\n' if code else "<main>\n")
        + f"{body}\n"
        "</main>\n"
        + (f"<script>{_SCRIPT}</script>\n" if script else "")
        + "</body>\n"
        "</html>\n"
    )
    headers = {**PRIVATE_HEADERS, "Content-Security-Policy": _policy(script=script)}
    return Page(status_code=status_code, html=text, headers=headers)


def invitation_page() -> Page:
    """The page the invitation link opens: what this is, and the button that starts the
    sign-in. The same page for everybody and every link: it does not know the invitation."""
    body = (
        "<h1>Приглашение в House Helper</h1>\n"
        "<p>Вас пригласили пользоваться House Helper. Чтобы получить доступ, "
        "подтвердите свой аккаунт в Telegram.</p>\n"
        '<p><button id="go" type="button" disabled>Продолжить через Telegram</button></p>\n'
        '<p id="status" role="status" hidden></p>\n'
        '<p class="note">Приглашение действует два часа и подходит один раз. Оно будет '
        "использовано только после того, как вы подтвердите аккаунт в Telegram. Если вы "
        "не ждали приглашения, просто закройте страницу.</p>\n"
        "<noscript><p>Для входа по приглашению нужен включённый JavaScript: код приглашения "
        "передаётся браузеру так, чтобы он не попадал на сервер до вашего подтверждения.</p>"
        "</noscript>"
    )
    return _document("Приглашение в House Helper", body, script=True, status_code=200)


def _message_page(
    title: str,
    message: str,
    status_code: int,
    *,
    application_link: Optional[str] = None,
    code: Optional[str] = None,
) -> Page:
    body = f"<h1>{html.escape(title)}</h1>\n<p>{html.escape(message)}</p>"
    if application_link is not None:
        body += (
            f'\n<p><a class="button" href="{APPLICATION_PATH}">{html.escape(application_link)}</a></p>'
        )
    return _document(title, body, script=False, status_code=status_code, code=code)


def done_page() -> Page:
    """What a person sees once their Telegram account is linked and they are signed in."""
    return _message_page(
        "Готово: Telegram привязан",
        "Вы вошли в House Helper. В следующий раз входите кнопкой «Войти через "
        "Telegram»: приглашение больше не понадобится.",
        200,
        application_link="Открыть помощника",
    )


def not_configured_page() -> Page:
    return _message_page(
        "Вход через Telegram недоступен",
        "Вход через Telegram сейчас не настроен. Обратитесь к тому, кто прислал приглашение.",
        503,
    )


def unavailable_page() -> Page:
    return _message_page(
        "Сервис временно недоступен",
        "Войти сейчас не получилось. Попробуйте ещё раз чуть позже: приглашение не использовано "
        "и пока действует.",
        503,
    )


def provider_unavailable_page() -> Page:
    return _message_page(
        "Telegram не отвечает",
        "Не получилось связаться с Telegram. Приглашение не использовано. Откройте ссылку-"
        "приглашение и попробуйте ещё раз чуть позже.",
        502,
    )


def invalid_invitation_page() -> Page:
    return _message_page(
        "Приглашение недействительно",
        "Приглашение уже использовано, отозвано или его срок истёк. Аккаунт Telegram не "
        "привязан. Попросите выдать новое приглашение.",
        400,
    )


def identity_page() -> Page:
    return _message_page(
        "Этот аккаунт Telegram нельзя привязать",
        "Этот аккаунт Telegram не подходит для приглашения. Приглашение не использовано: "
        "откройте ссылку снова и войдите другим аккаунтом Telegram или обратитесь к тому, "
        "кто его выдал.",
        409,
    )


def other_user_page() -> Page:
    return _message_page(
        "Вы уже вошли под другой учётной записью",
        "В этом браузере уже выполнен вход в другую учётную запись. Выйдите из неё и откройте "
        "ссылку-приглашение снова: приглашение не использовано.",
        409,
        application_link="Открыть помощника",
    )


def login_other_user_page() -> Page:
    """What an ORDINARY Telegram sign-in (no invitation) answers when the browser is signed in
    as somebody else: 409, the stable code in the markup (data-code), and the words that tell
    the person nothing was changed. The session cookie is not touched by this answer."""
    return _message_page(
        "Вы уже вошли под другой учётной записью",
        "В этом браузере уже выполнен вход в другую учётную запись. Вход через Telegram не "
        "выполнен, текущий вход не изменён. Чтобы войти под другой записью, выйдите из текущей "
        "в помощнике и войдите через Telegram снова.",
        409,
        application_link="Открыть помощника",
        code="signed_in_as_another_user",
    )


def not_completed_page() -> Page:
    return _message_page(
        "Вход через Telegram не завершён",
        "Не удалось подтвердить аккаунт Telegram: вход был отменён или не прошёл проверку. "
        "Приглашение не использовано. Откройте ссылку-приглашение заново и повторите вход.",
        400,
    )


def stale_page() -> Page:
    return _message_page(
        "Ссылка входа устарела",
        "Эта ссылка входа устарела или открыта не в том браузере, где вы начали. Если вы уже "
        "вошли, откройте помощника. Иначе откройте ссылку-приглашение заново и повторите вход "
        "в том же браузере.",
        400,
        application_link="Открыть помощника",
    )


def error_page() -> Page:
    return _message_page(
        "Что-то пошло не так",
        "Войти не получилось. Откройте ссылку-приглашение и попробуйте ещё раз; если не выйдет, "
        "попросите выдать новое приглашение.",
        500,
    )


# The codes of web.auth.LoginFailure that can end a sign-in begun with an invitation, and the
# page for each. Anything not listed is an unexpected failure.
_FAILURES = {
    "invalid_login_request": stale_page,
    "login_failed": not_completed_page,
    "provider_unavailable": provider_unavailable_page,
    "temporarily_unavailable": unavailable_page,
    "invitation_not_usable": invalid_invitation_page,
    "identity_not_linkable": identity_page,
    "signed_in_as_another_user": other_user_page,
}


def failure_page(code: str) -> Page:
    """The page for a sign-in that began with an invitation and stopped with `code`."""
    return _FAILURES.get(code, error_page)()
