"""The Telegram Bot API, as much of it as the bot needs, over httpx.

Five calls: getUpdates (long polling), sendMessage, sendChatAction, getFile and the download
of a file, and getMe. Nothing else, and no library of Telegram's own: the web package does not
import the Telegram transport (aiogram) and this module is not an exception to that. It is the
same kind of thing as telegram_oidc.py: a small client over an injected httpx client, so a test
puts a MockTransport in the place of the network.

The token is in the URL (that is how the Bot API is addressed), so this module takes care that
it goes nowhere else:

  * a failure is a BotApiError, which carries only a fixed word for the kind of failure, the
    method's name and the numbers Telegram answered with (HTTP status, error_code,
    retry_after). It never carries Telegram's description, the URL, the response body or the
    exception that was caught (httpx's messages can quote the URL);
  * logsafety.install_bot_token_redaction() puts a filter on the loggers of the HTTP libraries,
    which write the URL of every request at INFO when their level is lowered.

What comes back is plain JSON, and what is used from it is validated by the caller (the chat
layer reads ids as integers and text as strings and drops anything else).
"""

import re
from typing import Any, Optional

import httpx

API_ROOT = "https://api.telegram.org"

# The longest text Telegram accepts in one message.
MAX_MESSAGE_LENGTH = 4096

# A bot token is "<bot id>:<35 characters>"; the second part is made of letters, digits, "_" and "-".
TOKEN_PATTERN = re.compile(r"[0-9]{5,16}:[A-Za-z0-9_-]{30,64}")

# What Telegram puts in file_path: directories and a file name, nothing that could leave the
# file endpoint (the value is the server's, but it is put into a URL, so it is checked).
_FILE_PATH = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*")


class BotApiError(Exception):
    """A call to the Bot API did not succeed. `kind` is one of "network", "timeout",
    "malformed" (an answer that is not the API's), "api" (Telegram said ok=false), "http" (a
    file endpoint answered with a status other than 200) and "too_large" (a download over its
    limit). The numbers are what Telegram sent; none of them is user data."""

    def __init__(
        self,
        method: str,
        kind: str,
        *,
        status: Optional[int] = None,
        error_code: Optional[int] = None,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(f"{method}: {kind}")
        self.method = method
        self.kind = kind
        self.status = status
        self.error_code = error_code
        self.retry_after = retry_after

    @property
    def category(self) -> str:
        """A word for the log line: the kind, and the code when there is one."""
        code = self.error_code if self.error_code is not None else self.status
        return f"{self.kind}:{code}" if code is not None else self.kind

    @property
    def permanent(self) -> bool:
        """True when trying again cannot help: the token is refused (401) or the bot is gone
        (404). 403 and 400 are about one chat, not about the bot."""
        return self.kind == "api" and self.error_code in (401, 404)

    @property
    def conflict(self) -> bool:
        """True for 409: another consumer is polling with this token, or a webhook is set."""
        return self.kind == "api" and self.error_code == 409


def _integer(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class BotApi:
    """The calls. `http` is an httpx.AsyncClient that the caller owns and closes."""

    def __init__(self, token: str, http: httpx.AsyncClient) -> None:
        if TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError("not a bot token")
        self._base = f"{API_ROOT}/bot{token}/"
        self._file_base = f"{API_ROOT}/file/bot{token}/"
        self._http = http

    async def _call(
        self, method: str, payload: Optional[dict[str, Any]] = None, *, timeout: Optional[float] = None
    ) -> Any:
        try:
            response = await self._http.post(
                self._base + method,
                json=payload if payload is not None else {},
                **({} if timeout is None else {"timeout": timeout}),
            )
        except httpx.TimeoutException:
            raise BotApiError(method, "timeout") from None
        except httpx.HTTPError:
            raise BotApiError(method, "network") from None
        try:
            body = response.json()
        except ValueError:
            raise BotApiError(method, "malformed", status=response.status_code) from None
        if not isinstance(body, dict):
            raise BotApiError(method, "malformed", status=response.status_code)
        if body.get("ok") is not True:
            parameters = body.get("parameters")
            retry_after = _integer(parameters.get("retry_after")) if isinstance(parameters, dict) else None
            raise BotApiError(
                method,
                "api",
                status=response.status_code,
                error_code=_integer(body.get("error_code")),
                retry_after=retry_after,
            )
        return body.get("result")

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", timeout=10.0)
        if not isinstance(result, dict):
            raise BotApiError("getMe", "malformed")
        return result

    async def get_updates(self, offset: Optional[int], poll_seconds: int) -> list[Any]:
        """Updates after `offset` (None: all that are waiting). Waits up to `poll_seconds`
        for one; the HTTP call is allowed ten more."""
        payload: dict[str, Any] = {"timeout": poll_seconds, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload, timeout=poll_seconds + 10.0)
        if not isinstance(result, list):
            raise BotApiError("getUpdates", "malformed")
        return result

    async def send_message(self, chat_id: int, text: str) -> None:
        """Plain text, no parse mode (the assistant writes plain text, and a parse mode would
        make a stray "<" or "_" a delivery failure). Text over MAX_MESSAGE_LENGTH is the
        caller's to split."""
        await self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}},
            timeout=15.0,
        )

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10.0)

    async def get_file(self, file_id: str) -> tuple[str, Optional[int]]:
        """(file_path, size) of a file the bot can download. `size` is None when Telegram
        does not say."""
        result = await self._call("getFile", {"file_id": file_id}, timeout=10.0)
        path = result.get("file_path") if isinstance(result, dict) else None
        if not isinstance(path, str) or _FILE_PATH.fullmatch(path) is None or ".." in path.split("/"):
            raise BotApiError("getFile", "malformed")
        return path, _integer(result.get("file_size"))

    async def download(self, file_path: str, max_bytes: int) -> bytes:
        """The bytes of a file, at most `max_bytes` of them: a longer file is not read to its
        end (too_large)."""
        if _FILE_PATH.fullmatch(file_path) is None or ".." in file_path.split("/"):
            raise BotApiError("download", "malformed")
        try:
            async with self._http.stream("GET", self._file_base + file_path, timeout=30.0) as response:
                if response.status_code != 200:
                    raise BotApiError("download", "http", status=response.status_code)
                declared = response.headers.get("content-length", "")
                if declared.isascii() and declared.isdigit() and int(declared) > max_bytes:
                    raise BotApiError("download", "too_large")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise BotApiError("download", "too_large")
                    chunks.append(chunk)
        except httpx.TimeoutException:
            raise BotApiError("download", "timeout") from None
        except httpx.HTTPError:
            raise BotApiError("download", "network") from None
        return b"".join(chunks)


def split_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """`text` as pieces of at most `limit` characters, cut at a paragraph, a line or a space
    where there is one in the second half of the piece, and anywhere otherwise. Pieces are
    never empty; whitespace at the cuts is dropped."""
    pieces: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for separator in ("\n\n", "\n", " "):
            found = window.rfind(separator)
            if found >= limit // 2:
                cut = found
                break
        if cut == -1:
            cut = limit
        piece = rest[:cut].rstrip()
        if piece:
            pieces.append(piece)
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces
