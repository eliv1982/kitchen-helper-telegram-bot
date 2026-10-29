"""The few pages of email sign-in, as plain strings: no templates, no assets.

The sign-in link opens a page that must work without anything a security reviewer
would have to trust: no third-party script, font, stylesheet or image, and no
network request from the page. So the page is one self-contained document with one
inline script and one inline style, allowed by a Content-Security-Policy that names
each by the SHA-256 of its text (not "unsafe-inline"), and that forbids everything
else: the script cannot fetch, load, frame or submit anywhere but this origin.

What the script does, and why there is one at all. The secret in the link is in the
URL fragment (the part after "#"), which a browser never sends to a server: not in
the request line, not in an access log, not in a Referer header, not through a
redirect. Only script can read it. The script takes it out of the address bar (so it
does not stay on screen or in the history) and puts it into a hidden field of a form;
nothing is sent until the person presses the button and the browser POSTs the form
back to this origin. Opening the page therefore never uses the link up (a mail scanner
that fetches it is harmless), and the POST is an explicit act.

Every page is sent with Cache-Control: no-store and X-Content-Type-Options: nosniff,
may not be framed, and carries Referrer-Policy: same-origin, not no-referrer. The
reason is not secrecy but the form: for a form POST a browser sends "Origin: null"
when the referrer policy is no-referrer (Fetch's rule), which the sign-in endpoint
would have to refuse, and cannot tell from a forged request. With same-origin the
real Origin is sent to this site and no referrer at all to any other.
"""

import base64
import hashlib
import html
from dataclasses import dataclass

CONFIRM_ACTION = "/auth/email/confirm"

# The text of the script and the style, exactly as served: the CSP hash is a hash of
# these bytes.
_SCRIPT = """
(function () {
  "use strict";
  var form = document.getElementById("login");
  var button = document.getElementById("go");
  var problem = document.getElementById("problem");
  var token = window.location.hash.slice(1);
  try {
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  } catch (ignored) {}
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) {
    problem.hidden = false;
    return;
  }
  form.elements["token"].value = token;
  button.disabled = false;
  form.addEventListener("submit", function () {
    button.disabled = true;
  });
})();
"""

_STYLE = """
:root { color-scheme: light dark; }
body { font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 2rem 1rem; }
main { max-width: 28rem; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 1rem; }
button { font: inherit; padding: 0.6rem 1.6rem; border-radius: 0.5rem; cursor: pointer; }
button[disabled] { cursor: not-allowed; opacity: 0.6; }
.note { opacity: 0.75; font-size: 0.9rem; }
"""


def _sha256_source(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def _policy(*, script: bool) -> str:
    directives = [
        "default-src 'none'",
        f"style-src {_sha256_source(_STYLE)}",
        "form-action 'self'" if script else "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
    if script:
        directives.insert(1, f"script-src {_sha256_source(_SCRIPT)}")
    return "; ".join(directives)


PRIVATE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@dataclass(frozen=True)
class Page:
    """A rendered page and the headers it must be sent with."""

    status_code: int
    html: str
    headers: dict[str, str]


def _document(title: str, body: str, *, script: bool, status_code: int) -> Page:
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
        "<main>\n"
        f"{body}\n"
        "</main>\n"
        + (f"<script>{_SCRIPT}</script>\n" if script else "")
        + "</body>\n"
        "</html>\n"
    )
    headers = {**PRIVATE_HEADERS, "Content-Security-Policy": _policy(script=script)}
    return Page(status_code=status_code, html=text, headers=headers)


def confirm_page(challenge_id: str, masked_address: str) -> Page:
    """The page the emailed link opens: which address this is for, and the button.
    `challenge_id` must already be a validated challenge id (it is put into the
    page); the masked address is escaped regardless."""
    body = (
        "<h1>Вход в House Helper</h1>\n"
        f"<p>Вход для адреса <strong>{html.escape(masked_address)}</strong>.</p>\n"
        f'<form id="login" method="post" action="{CONFIRM_ACTION}" autocomplete="off">\n'
        f'<input type="hidden" name="c" value="{html.escape(challenge_id, quote=True)}">\n'
        '<input type="hidden" name="token" value="">\n'
        '<button id="go" type="submit" disabled>Войти</button>\n'
        "</form>\n"
        '<p id="problem" hidden>Ссылка неполная или повреждена. Откройте ее из письма целиком '
        "или запросите новую.</p>\n"
        '<p class="note">Нажимая кнопку, вы входите на этом устройстве. Если вы не запрашивали '
        "вход, просто закройте страницу.</p>\n"
        "<noscript><p>Для входа нужен включенный JavaScript — ссылка передает секрет браузеру "
        "так, чтобы он не попадал на сервер до вашего подтверждения.</p></noscript>"
    )
    return _document("Вход в House Helper", body, script=True, status_code=200)


def _message_page(title: str, message: str, status_code: int) -> Page:
    body = f"<h1>{html.escape(title)}</h1>\n<p>{html.escape(message)}</p>"
    return _document(title, body, script=False, status_code=status_code)


def invalid_link_page() -> Page:
    return _message_page(
        "Ссылка недействительна",
        "Эта ссылка для входа недействительна или устарела. Запросите новую ссылку и "
        "откройте ее из нового письма.",
        400,
    )


def other_user_page() -> Page:
    return _message_page(
        "Вы уже вошли под другой учетной записью",
        "В этом браузере уже выполнен вход в другую учетную запись. Выйдите из нее и "
        "откройте ссылку из письма снова: она еще действует.",
        409,
    )


def forbidden_page() -> Page:
    return _message_page("Запрос отклонен", "Этот запрос пришел не с нашего сайта.", 403)


def unavailable_page() -> Page:
    return _message_page(
        "Сервис временно недоступен",
        "Войти сейчас не получилось. Попробуйте еще раз чуть позже: ссылка пока действует.",
        503,
    )


def error_page() -> Page:
    return _message_page(
        "Что-то пошло не так",
        "Войти не получилось. Запросите новую ссылку и попробуйте еще раз.",
        500,
    )
