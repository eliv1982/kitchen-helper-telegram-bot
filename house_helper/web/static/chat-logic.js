/*
 * Kitchen Helper web chat: the logic, without a DOM.
 *
 * Everything here is plain data in, plain data out (or a promise of it), so it can be
 * run by Node against a scripted fetch (tests/test_web_ui_logic.py) and by the page
 * (app.js), which only draws what getView() describes and forwards clicks. No
 * element, no innerHTML, no eval, nothing remote.
 *
 * What the page may rely on, and why:
 *
 *  - Who the user is comes from the session cookie on the server; nothing here ever
 *    sends a user id, a token or a cookie. The id /auth/me names is kept in memory (the
 *    principal) and written next to each record of an unresolved message (below), only
 *    to tell whose it is.
 *  - One message is one idempotency key. A key is made when the person sends, and it
 *    stays with its exact text until the turn is resolved. Every retry of an unclear
 *    outcome (no answer, a proxy's 502/504, a dropped connection, a reload) uses the
 *    SAME key and the SAME text, which the server treats as the same message: answered
 *    from its record if it was answered, still being worked on (409, wait), or run
 *    again if it failed. A fresh key is made only for a new message, or when the server
 *    said for certain that this key can no longer be used (turn_superseded,
 *    idempotency_key_unusable, idempotency_key_reused) and the person chose to send the
 *    text again.
 *  - A reply is never assumed lost, and an error status is never taken for what became of
 *    the message: every 5xx (the application's own error answer included: a lost commit
 *    acknowledgement is a 503 for a turn that was completed and stored), a body that is
 *    not the application's JSON, a dropped connection and a timeout are "uncertain",
 *    never "failed". The history is read (read-only) to see what the server has;
 *    messages are merged by their id, so reading it and then getting the answer to a
 *    retry never shows a message twice. A message is known to have failed only when the
 *    history has it, under its own key, with the status "failed": that is the server's
 *    record, and it is then the history's (with its own same-key retry).
 *  - While a message is "uncertain" its key and text cannot be given up: the only way
 *    out is the same key sent again (the server replays, waits, or runs it), because the
 *    history cannot prove that a message did NOT run (a read can be stale, partial or
 *    fail) and exposes the key only of messages that are not finished, so it cannot
 *    prove that a finished one is this one either: a finished exchange with the same
 *    words is shown as a hint, never as the answer. Taking the text back, or sending it
 *    as a new message, is offered only after the server has refused the message for a
 *    reason of the request itself (a 4xx), never because of a 5xx.
 *  - A conversation that exists is written to only after its latest history page has
 *    been read successfully in this session: the baseline of a message (the newest id
 *    known when it was sent) is then the server's, never an empty conversation's.
 *  - The session's end is known from /auth/me (session_expires_at, absolute). When the
 *    deadline has passed, or the server says 401, everything of the conversations is
 *    dropped from memory and answers still on their way are ignored (epoch); the record
 *    of unresolved messages stays for the next sign-in.
 *  - Who is signed in is asked again whenever the page comes back (shown again, online
 *    again, resumed), and that is a privacy boundary: from that moment until the server has
 *    answered, nothing of the conversations is in the view (nor is anything asked of the
 *    server for them), and the answer decides what comes back. The same person (user_id):
 *    the conversations as they were, unresolved messages included. A different person:
 *    everything of the last one is dropped as if the page had just been opened (the epoch
 *    makes late answers meant for the last one change nothing). No answer: it stays held
 *    back, without claiming anything, until one comes. A 401: the end of the session.
 *  - Leaving is a boundary too: the moment "Выйти" is pressed the conversations are gone
 *    from memory and the view, the epoch moves on and the record of unresolved messages
 *    is removed, whatever the server answers. Only a 2xx says the session is revoked; any
 *    other outcome (a lost answer included) is said to be unconfirmed, with a way to leave
 *    again, and never brings the conversations back.
 *  - A conversation is created only when the first message is sent, and its creation
 *    (which has no idempotency) is never repeated blindly: after an unclear outcome
 *    the list is read again and the person decides.
 *  - A minimal record of each unresolved turn (whose it is, conversation id, key, exact
 *    text, the id of the newest message known when it was sent, the time) is kept in
 *    sessionStorage so a reload in the same tab can resolve it. It is removed when the
 *    turn resolves, on logout and when the person discards it (possible only for a message
 *    the server has refused); a record that is damaged, too old or another person's is
 *    removed when it is read, not just ignored, and is never shown to, or counted for, the
 *    person who is signed in. If the browser will not keep it, the page says so. Never a
 *    cookie, a token or a history.
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module !== null && module.exports) {
    module.exports = api;
  } else {
    root.HouseHelperLogic = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // --- constants ----------------------------------------------------------------------

  var MAX_MESSAGE_LENGTH = 2000; // Unicode code points; the server's MAX_INPUT_LENGTH
  var PAGE_SIZE = 50;
  var MESSAGE_TIMEOUT_MS = 90000; // a reply takes up to 60 s; the server's lease is 90 s
  var READ_TIMEOUT_MS = 20000;
  var STORAGE_KEY = "hh.pending.v1";
  var STORAGE_MAX_AGE_MS = 6 * 60 * 60 * 1000;
  var STORAGE_MAX_RECORDS = 8;
  var MAX_USER_ID_LENGTH = 64; // the server's are UUIDs (36); this only bounds what is stored
  var MAX_AUTO_ATTEMPTS = 24; // waiting for a busy conversation, at 5 s each: about two minutes
  var DEFAULT_RETRY_MS = 5000;
  var MIN_RETRY_MS = 1000;
  var MAX_RETRY_MS = 30000;
  var WATCH_DELAYS_MS = [2000, 3000, 4000, 5000];
  var WATCH_LIMIT_MS = 150000;

  var KEY_PATTERN = /^[!-~]{1,128}$/;
  var UUID_PATTERN = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
  var CORRELATION_PATTERN = /^hh-[0-9a-f]{16}$/;
  var CODE_PATTERN = /^[a-z][a-z_]{2,47}$/;

  // --- the words ----------------------------------------------------------------------

  var TEXT = {
    appName: "Кухонный помощник",
    authExpired: "Сессия закончилась или была прекращена. Войдите снова.",
    unavailable: "Сервис временно недоступен. Попробуйте ещё раз чуть позже.",
    network: "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.",
    timeout: "Сервер слишком долго не отвечает. Попробуйте ещё раз чуть позже.",
    unexpected: "Что-то пошло не так. Попробуйте ещё раз чуть позже.",
    loggedOut: "Вы вышли из аккаунта.",
    // Said when the page has left the conversations but the server's answer to "Выйти" is not
    // known (or is not a success): it does not claim the session is closed.
    logoutUnconfirmed:
      "Не удалось подтвердить выход из аккаунта: сессия, возможно, ещё открыта. " +
      "Разговоры скрыты. Нажмите «Выйти ещё раз», когда появится связь.",
    // Said when a sign-out (the first try, or the repeat of an unconfirmed one) finds another person's session in
    // the browser: the server ended nothing. It says neither who signed in nor that the first session is closed
    // (it is not known).
    logoutMismatch:
      "Выход не выполнен: в этом браузере уже вошёл другой пользователь, и его вход не затронут. " +
      "Прежние разговоры скрыты. Нажмите «Повторить», чтобы открыть страницу заново.",
    unverified:
      "Не удалось проверить, кто сейчас вошёл в аккаунт, поэтому разговоры скрыты, пока это не выяснится.",
    retry: "Повторить",
    logoutAgain: "Выйти ещё раз",
    loading: "Загружаем…",
    verifying: "Проверяем вход…",
    signingOut: "Выходим…",
    emptyMessage: "Напишите сообщение.",
    tooLong: "Сообщение слишком длинное (максимум {max} символов, сейчас {count}). Сократите его и отправьте снова.",
    badCharacters: "В сообщении есть недопустимые символы. Уберите их и отправьте снова.",
    noCrypto: "Браузер не смог подготовить безопасную отправку. Обновите страницу или откройте её в другом браузере.",
    invalidEmail: "Введите корректный адрес электронной почты.",
    emailSent:
      "Если этот адрес есть в списке разрешённых, мы отправили на него письмо со ссылкой для входа. " +
      "Если письма нет, проверьте папку «Спам» или подождите немного и запросите ссылку снова.",
    listFailed: "Не удалось загрузить список разговоров.",
    threadFailed: "Не удалось загрузить разговор.",
    olderFailed: "Не удалось загрузить более ранние сообщения.",
    conversationGone: "Этот разговор больше недоступен.",
    createRejected: "Не удалось начать разговор.",
    createUncertain:
      "Не удалось убедиться, что новый разговор создан. Ваше сообщение осталось в поле ввода, " +
      "список разговоров обновлён: проверьте, нет ли в нём нового разговора. " +
      "Если нет — нажмите «Отправить» ещё раз.",
    replied: "Помощник ответил.",
    preparing: "Помощник готовит ответ…",
    sending: "Отправляем…",
    slow: "Ответ готовится дольше обычного. Подождите ещё немного.",
    slower: "Всё ещё готовится. Ответ может занять до минуты.",
    waitingBusy: "В этом разговоре ещё готовится предыдущий ответ. Подождите — отправим сообщение сами.",
    waitingOwn: "Ответ на это сообщение ещё готовится. Подождите немного.",
    checking: "Проверяем, дошло ли сообщение…",
    stalled: "Ответ всё ещё не готов. Нажмите «Повторить» — сообщение не будет отправлено дважды.",
    uncertainNetwork:
      "Не удалось получить ответ: связь прервалась. Возможно, сообщение уже дошло. " +
      "Нажмите «Повторить» — оно не будет отправлено дважды.",
    uncertainTimeout:
      "Помощник отвечает дольше обычного, сообщение, возможно, ещё обрабатывается. " +
      "Нажмите «Повторить» — оно не будет отправлено дважды.",
    uncertainProxy:
      "Сервис ответил непонятно (так бывает при сбое на стороне сервиса). Сообщение, возможно, уже дошло. " +
      "Нажмите «Повторить» — оно не будет отправлено дважды.",
    uncertainServer:
      "Сервис сообщил об ошибке, но это не значит, что сообщение не дошло: оно могло быть уже обработано. " +
      "Нажмите «Повторить» — оно не будет отправлено дважды.",
    uncertainRestored:
      "Страница была перезагружена, пока шла отправка. Нажмите «Повторить» — если сообщение уже дошло, " +
      "оно не будет отправлено дважды.",
    uncertainSeen:
      "Похоже, сообщение уже дошло: ответ на него виден выше. " +
      "Нажмите «Повторить», чтобы убедиться в этом — оно не будет отправлено дважды.",
    hintUncertain:
      "Сначала нажмите «Повторить» у сообщения выше: пока сервер не подтвердил, что с ним, " +
      "новое сообщение в этом разговоре отправить нельзя.",
    notSaved:
      "Браузер не сохраняет это сообщение. Если закрыть или перезагрузить страницу, сначала посмотрите " +
      "историю разговора и только потом отправляйте его заново.",
    expiredRecords:
      "Неподтверждённое сообщение старше 6 часов удалено из памяти страницы. " +
      "Проверьте историю разговора: возможно, оно дошло.",
    historyLoading: "Загружаем историю разговора. Отправка станет доступна, когда она загрузится.",
    historyUnavailable: "Не удалось загрузить историю разговора, поэтому отправка пока недоступна. Загрузите её ещё раз.",
    failedGeneric: "Не удалось получить ответ. Попробуйте ещё раз.",
    correlation: "Номер обращения: {id}",
    emptyThread:
      "Спросите, что приготовить из имеющихся продуктов, или как приготовить блюдо.",
  };

  // What the server says for each of its error codes (house_helper/web/diagnostics.py).
  // The server's own `message` is preferred when the answer is the application's; this
  // table is for the answers that have no message (the /auth endpoints) and for a history
  // that names only the code of a failure. tests/test_web_ui_logic.py checks that every
  // code of the server's catalogue is here, with the same words.
  var CODE_MESSAGES = {
    invalid_request: "Запрос не удалось разобрать.",
    unsupported_media_type: "Запрос должен быть в формате JSON.",
    request_too_large: "Запрос слишком большой.",
    forbidden_origin: "Запрос отклонён: он пришёл не с нашего сайта.",
    idempotency_key_required:
      "Не удалось отправить сообщение: у запроса нет ключа. Обновите страницу и попробуйте ещё раз.",
    invalid_idempotency_key:
      "Не удалось отправить сообщение: ключ запроса некорректен. Обновите страницу и попробуйте ещё раз.",
    empty_message: "Сообщение не должно быть пустым.",
    invalid_message_text: "В сообщении есть недопустимые символы.",
    message_too_long: "Сообщение слишком длинное (максимум {max_length} символов). Сократите его и отправьте снова.",
    invalid_query: "Параметры запроса указаны неверно.",
    not_authenticated: "Войдите в аккаунт, чтобы продолжить.",
    conversation_not_found: "Разговор не найден.",
    idempotency_key_reused: "Этот ключ запроса уже использован для другого сообщения. Отправьте сообщение заново.",
    conversation_busy: "В этом разговоре ещё готовится предыдущий ответ. Подождите немного и повторите.",
    turn_in_progress: "Ответ на это сообщение ещё готовится. Подождите немного.",
    turn_superseded:
      "Это сообщение уже нельзя повторить: в разговоре появились новые. Отправьте его заново как новое.",
    idempotency_key_unusable: "Этот запрос больше нельзя повторить. Отправьте сообщение заново.",
    provider_rate_limited: "Сейчас слишком много запросов к помощнику. Подождите немного и попробуйте снова.",
    provider_unreachable: "Не удалось связаться с помощником. Попробуйте ещё раз чуть позже.",
    provider_timeout: "Помощник слишком долго готовил ответ. Попробуйте ещё раз.",
    provider_empty_response:
      "Помощник вернул пустой ответ. Попробуйте переформулировать вопрос или повторить позже.",
    provider_error: "Помощник сейчас не может ответить. Попробуйте ещё раз чуть позже.",
    interrupted: "Работа над ответом была прервана. Отправьте сообщение ещё раз.",
    chat_unavailable: "Помощник сейчас недоступен.",
    service_unavailable: "Сервис временно недоступен. Попробуйте ещё раз чуть позже.",
    internal_error: "Что-то пошло не так. Попробуйте ещё раз чуть позже.",
    // Not in the catalogue: what the endpoints under /auth answer (a code, no message), and the
    // code of a turn that failed while its reply was being stored (the history names it).
    temporarily_unavailable: "Сервис временно недоступен. Попробуйте ещё раз чуть позже.",
    login_unavailable: "Вход сейчас недоступен. Попробуйте ещё раз чуть позже.",
    invalid_email: "Введите корректный адрес электронной почты.",
    server_error: "Что-то пошло не так. Попробуйте ещё раз чуть позже.",
    storage_unavailable: "Сервис временно недоступен. Попробуйте ещё раз чуть позже.",
  };

  var MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
  ];

  function fill(template, values) {
    return template.replace(/\{(\w+)\}/g, function (whole, name) {
      return Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : whole;
    });
  }

  function messageForCode(code) {
    if (typeof code === "string" && Object.prototype.hasOwnProperty.call(CODE_MESSAGES, code)) {
      return fill(CODE_MESSAGES[code], { max_length: MAX_MESSAGE_LENGTH });
    }
    return null;
  }

  // --- text: what the server will make of what is typed ----------------------------------

  // The characters Python's str.strip() removes (str.isspace()): not JavaScript's trim(),
  // which also removes U+FEFF and keeps U+001C..U+001F and U+0085. The message is trimmed
  // here the way the server will trim it, so the text kept for a retry is the text stored.
  var SPACE_CLASS =
    "\\t\\n\\u000b\\u000c\\r\\u001c-\\u001f \\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
  var LEADING_SPACE = new RegExp("^[" + SPACE_CLASS + "]+");
  var TRAILING_SPACE = new RegExp("[" + SPACE_CLASS + "]+$");
  // A control character other than tab, newline and carriage return, and DEL..C1: refused
  // by the server (they cannot be stored everywhere).
  var CONTROL_CHARACTER = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/;

  function trimLikeServer(text) {
    return String(text).replace(LEADING_SPACE, "").replace(TRAILING_SPACE, "");
  }

  // Unicode code points, as Python's len() counts them: a character outside the basic plane
  // (an emoji) is one, though JavaScript's .length says two. A lone surrogate counts as one.
  function countCodePoints(text) {
    var count = 0;
    for (var i = 0; i < text.length; i++) {
      var unit = text.charCodeAt(i);
      if (unit >= 0xd800 && unit <= 0xdbff && i + 1 < text.length) {
        var next = text.charCodeAt(i + 1);
        if (next >= 0xdc00 && next <= 0xdfff) {
          i++;
        }
      }
      count++;
    }
    return count;
  }

  function hasLoneSurrogate(text) {
    for (var i = 0; i < text.length; i++) {
      var unit = text.charCodeAt(i);
      if (unit >= 0xd800 && unit <= 0xdbff) {
        var next = i + 1 < text.length ? text.charCodeAt(i + 1) : 0;
        if (next >= 0xdc00 && next <= 0xdfff) {
          i++;
          continue;
        }
        return true;
      }
      if (unit >= 0xdc00 && unit <= 0xdfff) {
        return true;
      }
    }
    return false;
  }

  function hasUnstorableCharacters(text) {
    return CONTROL_CHARACTER.test(text) || hasLoneSurrogate(text);
  }

  // The checks the server makes, in its order (chat_api._message_text): trimmed, not empty,
  // no unstorable character, not longer than the limit. The server stays the authority.
  function prepareMessage(raw, maxLength) {
    var limit = maxLength || MAX_MESSAGE_LENGTH;
    var text = trimLikeServer(typeof raw === "string" ? raw : "");
    var count = countCodePoints(text);
    if (!text) {
      return { ok: false, reason: "empty", message: TEXT.emptyMessage, count: 0, max: limit };
    }
    if (hasUnstorableCharacters(text)) {
      return { ok: false, reason: "characters", message: TEXT.badCharacters, count: count, max: limit };
    }
    if (count > limit) {
      return {
        ok: false,
        reason: "too_long",
        message: fill(TEXT.tooLong, { max: limit, count: count }),
        count: count,
        max: limit,
      };
    }
    return { ok: true, text: text, count: count, max: limit };
  }

  // --- idempotency keys -----------------------------------------------------------------------

  function isValidIdempotencyKey(key) {
    return typeof key === "string" && KEY_PATTERN.test(key);
  }

  // 128 random bits from the browser's cryptographic generator, as printable ASCII. There is
  // no fallback to Math.random: without crypto.getRandomValues nothing is sent.
  function newIdempotencyKey(cryptoApi) {
    if (!cryptoApi || typeof cryptoApi.getRandomValues !== "function") {
      throw new Error("no cryptographic random source");
    }
    var bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    var text = "web-";
    for (var i = 0; i < bytes.length; i++) {
      text += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16);
    }
    return text;
  }

  // --- time -----------------------------------------------------------------------------------

  // An ISO 8601 timestamp as milliseconds since the epoch, or null. One with no offset is UTC
  // (the server's times are), whichever way the browser's own parser would read it.
  function parseTimestamp(value) {
    if (typeof value !== "string") {
      return null;
    }
    var found = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}(?::?\d{2})?)?$/.exec(
      value
    );
    if (found === null) {
      return null;
    }
    var month = Number(found[2]);
    var day = Number(found[3]);
    var hour = Number(found[4]);
    var minute = Number(found[5]);
    var second = Number(found[6]);
    if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 60) {
      return null;
    }
    var millis = found[7] ? Math.floor(Number("0." + found[7]) * 1000) : 0;
    var time = Date.UTC(Number(found[1]), month - 1, day, hour, minute, second, millis);
    var offset = found[8];
    if (offset && offset !== "Z") {
      var sign = offset.charAt(0) === "-" ? -1 : 1;
      var digits = offset.slice(1).replace(":", "");
      var minutes = Number(digits.slice(0, 2)) * 60 + Number(digits.slice(2) || "0");
      time -= sign * minutes * 60000;
    }
    return isNaN(time) ? null : time;
  }

  function pad2(number) {
    return (number < 10 ? "0" : "") + number;
  }

  function clock(date) {
    return pad2(date.getHours()) + ":" + pad2(date.getMinutes());
  }

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }

  // "14:32" for today, "вчера, 14:32", "18 октября, 14:32", and with the year for another year.
  function formatMoment(time, nowTime) {
    if (typeof time !== "number") {
      return "";
    }
    var date = new Date(time);
    var now = new Date(nowTime);
    if (sameDay(date, now)) {
      return clock(date);
    }
    var yesterday = new Date(now.getTime());
    yesterday.setDate(yesterday.getDate() - 1);
    if (sameDay(date, yesterday)) {
      return "вчера, " + clock(date);
    }
    var label = date.getDate() + " " + MONTHS[date.getMonth()];
    if (date.getFullYear() !== now.getFullYear()) {
      label += " " + date.getFullYear();
    }
    return label + ", " + clock(date);
  }

  // The name of a conversation in the list. The server stores no title (and the page invents
  // none that it keeps): a conversation with none is named after when it began.
  function conversationLabel(conversation, nowTime) {
    if (typeof conversation.title === "string" && trimLikeServer(conversation.title)) {
      return conversation.title;
    }
    if (typeof conversation.createdAt !== "number") {
      return "Разговор";
    }
    var date = new Date(conversation.createdAt);
    var label = "Разговор от " + date.getDate() + " " + MONTHS[date.getMonth()];
    if (typeof nowTime === "number" && date.getFullYear() !== new Date(nowTime).getFullYear()) {
      label += " " + date.getFullYear();
    }
    return label + ", " + clock(date);
  }

  // --- reading what the server answered --------------------------------------------------------

  function isObject(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
  }

  function isCount(value) {
    return typeof value === "number" && isFinite(value) && Math.floor(value) === value && value >= 0;
  }

  function parseJson(text) {
    if (typeof text !== "string" || text === "") {
      return { ok: false, value: null };
    }
    try {
      return { ok: true, value: JSON.parse(text) };
    } catch (ignored) {
      return { ok: false, value: null };
    }
  }

  // The application's own error answer (chat_api / diagnostics.py): a code, a correlation id
  // the server made, and (usually) a message. Anything else -- a proxy's HTML, a gateway's JSON
  // -- is not this, and is never read as if it were.
  function readApiError(response) {
    if (!response.jsonOk || !isObject(response.json)) {
      return null;
    }
    var body = response.json;
    if (typeof body.detail !== "string" || !CODE_PATTERN.test(body.detail)) {
      return null;
    }
    if (typeof body.correlation_id !== "string" || !CORRELATION_PATTERN.test(body.correlation_id)) {
      return null;
    }
    var text = null;
    if (typeof body.message === "string" && body.message.length > 0 && body.message.length <= 400) {
      text = body.message;
    }
    return {
      code: body.detail,
      message: text || messageForCode(body.detail) || null,
      retryable: body.retryable === true,
      correlationId: body.correlation_id,
      retryAfterMs: retryDelay(response, body),
    };
  }

  // The code of an answer of the /auth endpoints: {"detail": code}, no correlation id there.
  function readAuthCode(response) {
    if (response.jsonOk && isObject(response.json) && typeof response.json.detail === "string") {
      return CODE_PATTERN.test(response.json.detail) ? response.json.detail : null;
    }
    return null;
  }

  function retryDelay(response, body) {
    var seconds = NaN;
    if (response.retryAfter && /^[0-9]{1,5}$/.test(response.retryAfter)) {
      seconds = Number(response.retryAfter);
    } else if (isObject(body) && typeof body.retry_after_seconds === "number") {
      seconds = body.retry_after_seconds;
    }
    if (!isFinite(seconds)) {
      return DEFAULT_RETRY_MS;
    }
    return Math.min(MAX_RETRY_MS, Math.max(MIN_RETRY_MS, Math.round(seconds * 1000)));
  }

  function unavailableText(response, error) {
    if (response.kind === "network") {
      return response.timedOut ? TEXT.timeout : TEXT.network;
    }
    if (error && error.message) {
      return error.message;
    }
    return response.status >= 500 ? TEXT.unavailable : TEXT.unexpected;
  }

  // GET /auth/me: 200 is a signed-in user, a definite 401 is not, and everything else is a
  // service that cannot say (network, timeout, 5xx, a proxy's page): never "signed out".
  // A 200 is a session only if it also says when it ends (session_expires_at, an absolute
  // time): without a usable deadline it is an answer that is not the application's, and
  // none is invented. A deadline that is already behind the clock is not a session either.
  // An "ok" names the person (userId): the page keeps it, and asks again whenever it comes back, to
  // know that the person at the other end is still the one whose conversations it shows.
  function classifyMe(response, nowTime) {
    if (response.kind === "network") {
      return { type: "unavailable", text: unavailableText(response, null), correlationId: null };
    }
    if (response.status === 401) {
      return { type: "auth" };
    }
    if (
      response.status === 200 &&
      response.jsonOk &&
      isObject(response.json) &&
      typeof response.json.user_id === "string" &&
      response.json.user_id.length > 0 &&
      response.json.user_id.length <= MAX_USER_ID_LENGTH
    ) {
      var expiresAt = parseTimestamp(response.json.session_expires_at);
      if (expiresAt === null) {
        return { type: "unavailable", text: TEXT.unexpected, correlationId: null };
      }
      if (expiresAt <= (typeof nowTime === "number" ? nowTime : Date.now())) {
        return { type: "auth", expired: true };
      }
      return { type: "ok", expiresAt: expiresAt, userId: response.json.user_id };
    }
    return { type: "unavailable", text: unavailableText(response, null), correlationId: null };
  }

  // The reads of the chat API (the list, the history).
  function classifyRead(response) {
    if (response.kind === "network") {
      return { type: "unavailable", text: unavailableText(response, null), correlationId: null };
    }
    if (response.status === 401) {
      return { type: "auth" };
    }
    var error = readApiError(response);
    if (response.status === 404 && error && error.code === "conversation_not_found") {
      return { type: "not_found" };
    }
    if (response.status === 200 && response.jsonOk && isObject(response.json)) {
      return { type: "ok", body: response.json };
    }
    return {
      type: "unavailable",
      text: unavailableText(response, error),
      correlationId: error ? error.correlationId : null,
    };
  }

  function classifyCreate(response) {
    if (response.kind === "network") {
      return { type: "uncertain", text: unavailableText(response, null) };
    }
    if (response.status === 401) {
      return { type: "auth" };
    }
    if (response.status === 201 && response.jsonOk && isObject(response.json)) {
      var conversation = normalizeConversation(response.json);
      if (conversation) {
        return { type: "created", conversation: conversation };
      }
      return { type: "uncertain", text: TEXT.unexpected };
    }
    var error = readApiError(response);
    // The application refused it for a reason of the request: nothing was created.
    if (error && response.status >= 400 && response.status < 500) {
      return {
        type: "rejected",
        text: error.message || TEXT.createRejected,
        correlationId: error.correlationId,
      };
    }
    // Anything else (a 5xx, a proxy's answer, a malformed body) cannot say whether the
    // conversation was made: it is not repeated blindly.
    return { type: "uncertain", text: unavailableText(response, error), correlationId: error ? error.correlationId : null };
  }

  function normalizeConversation(item) {
    if (!isObject(item) || typeof item.id !== "string" || !UUID_PATTERN.test(item.id)) {
      return null;
    }
    if (item.title !== null && item.title !== undefined && typeof item.title !== "string") {
      return null;
    }
    return {
      id: item.id.toLowerCase(),
      title: typeof item.title === "string" ? item.title : null,
      createdAt: parseTimestamp(item.created_at),
      updatedAt: parseTimestamp(item.updated_at),
    };
  }

  function normalizeConversationList(body) {
    if (!isObject(body) || !Array.isArray(body.conversations) || typeof body.has_more !== "boolean") {
      return null;
    }
    var items = [];
    for (var i = 0; i < body.conversations.length; i++) {
      var conversation = normalizeConversation(body.conversations[i]);
      if (conversation === null) {
        return null;
      }
      items.push(conversation);
    }
    return { items: items, hasMore: body.has_more };
  }

  var MESSAGE_STATUSES = { completed: true, in_progress: true, failed: true };

  function normalizeMessage(item) {
    if (!isObject(item) || !isCount(item.id) || item.id < 1) {
      return null;
    }
    if (item.role !== "user" && item.role !== "assistant") {
      return null;
    }
    if (typeof item.text !== "string") {
      return null;
    }
    var status = item.status === undefined ? "completed" : item.status;
    if (typeof status !== "string" || MESSAGE_STATUSES[status] !== true) {
      return null;
    }
    var key = typeof item.idempotency_key === "string" && KEY_PATTERN.test(item.idempotency_key)
      ? item.idempotency_key
      : null;
    return {
      id: item.id,
      role: item.role,
      text: item.text,
      createdAt: parseTimestamp(item.created_at),
      status: status,
      key: key,
      errorCode: typeof item.error_code === "string" && CODE_PATTERN.test(item.error_code) ? item.error_code : null,
    };
  }

  function normalizeMessagePage(body, conversationId) {
    if (!isObject(body) || !Array.isArray(body.messages) || typeof body.has_more !== "boolean") {
      return null;
    }
    if (typeof body.conversation_id !== "string" || body.conversation_id.toLowerCase() !== conversationId) {
      return null;
    }
    var messages = [];
    for (var i = 0; i < body.messages.length; i++) {
      var message = normalizeMessage(body.messages[i]);
      if (message === null) {
        return null;
      }
      messages.push(message);
    }
    var next = isCount(body.next_before) && body.next_before > 0 ? body.next_before : null;
    return { messages: messages, hasMore: body.has_more && next !== null, nextBefore: next };
  }

  // The 201/200 answer to a message: both messages of the exchange, completed.
  function normalizeTurn(body, conversationId) {
    if (!isObject(body) || typeof body.conversation_id !== "string" || body.conversation_id.toLowerCase() !== conversationId) {
      return null;
    }
    var user = normalizeMessage(isObject(body.user_message) ? Object.assign({ status: "completed" }, body.user_message) : null);
    var assistant = normalizeMessage(
      isObject(body.assistant_message) ? Object.assign({ status: "completed" }, body.assistant_message) : null
    );
    if (user === null || assistant === null || user.role !== "user" || assistant.role !== "assistant") {
      return null;
    }
    if (assistant.id <= user.id) {
      return null;
    }
    user.status = "completed";
    user.key = null;
    user.errorCode = null;
    assistant.status = "completed";
    return { user: user, assistant: assistant, truncated: body.truncated === true };
  }

  // The answer to POST .../messages, as what the page must do next:
  //   success    the exchange (200 replay or 201)
  //   auth       401: the session is gone
  //   busy       409 conversation_busy / turn_in_progress: nothing is wrong, wait and ask again
  //              with the same key
  //   rejected   the application refused this request for a certain reason (a 4xx); nothing ran.
  //              `asNew`: the key can never be used again, the text may be sent as a new message
  //   not_found  the conversation is gone
  //   uncertain  the outcome is not known: no answer, a body that is not the application's, and
  //              EVERY 5xx. A 5xx is the answer to this request, not the server's record of the
  //              turn: the completion may have been stored and only the answer to it failed (a
  //              lost commit acknowledgement is a 503). The application's own 5xx keeps its code
  //              and correlation id for diagnostics. Never "failed", never a reason for a fresh
  //              key: only the history (the message under its own key, with the status "failed")
  //              says a turn failed.
  function classifyMessageSend(response, conversationId) {
    if (response.kind === "network") {
      return { type: "uncertain", reason: response.timedOut ? "timeout" : "network" };
    }
    var status = response.status;
    if (status === 200 || status === 201) {
      var turn = response.jsonOk ? normalizeTurn(response.json, conversationId) : null;
      return turn ? { type: "success", turn: turn, replayed: status === 200 } : { type: "uncertain", reason: "proxy" };
    }
    if (status === 401) {
      return { type: "auth" };
    }
    var error = readApiError(response);
    if (error === null) {
      return { type: "uncertain", reason: "proxy" };
    }
    var common = { code: error.code, text: error.message || TEXT.unexpected, correlationId: error.correlationId };
    if (status === 409) {
      if (error.code === "conversation_busy" || error.code === "turn_in_progress") {
        return Object.assign({ type: "busy", retryAfterMs: error.retryAfterMs }, common);
      }
      if (
        error.code === "turn_superseded" ||
        error.code === "idempotency_key_unusable" ||
        error.code === "idempotency_key_reused"
      ) {
        return Object.assign({ type: "rejected", asNew: true }, common);
      }
      if (error.retryable) {
        return Object.assign({ type: "busy", retryAfterMs: error.retryAfterMs }, common);
      }
      return Object.assign({ type: "rejected", asNew: false }, common);
    }
    if (status === 404 && error.code === "conversation_not_found") {
      return { type: "not_found" };
    }
    if (status >= 500) {
      return Object.assign({ type: "uncertain", reason: "server" }, common);
    }
    return Object.assign({ type: "rejected", asNew: false }, common);
  }

  // --- email address ---------------------------------------------------------------------------

  // Trimmed, and nothing else: the local part keeps its case (the server lower-cases only the
  // domain). The server decides who may sign in; this only refuses what cannot be an address.
  function checkEmail(raw) {
    var address = typeof raw === "string" ? raw.replace(/^[ \t\r\n\f\v]+|[ \t\r\n\f\v]+$/g, "") : "";
    var at = address.indexOf("@");
    if (
      address.length < 3 ||
      address.length > 320 ||
      !/^[!-~]+$/.test(address) ||
      at < 1 ||
      at !== address.lastIndexOf("@") ||
      at === address.length - 1
    ) {
      return { ok: false, address: address };
    }
    return { ok: true, address: address };
  }

  // --- messages of a conversation ------------------------------------------------------------------

  function mergeMessages(existing, incoming) {
    var byId = {};
    var i;
    for (i = 0; i < existing.length; i++) {
      byId[existing[i].id] = existing[i];
    }
    for (i = 0; i < incoming.length; i++) {
      byId[incoming[i].id] = incoming[i];
    }
    var merged = [];
    for (var id in byId) {
      if (Object.prototype.hasOwnProperty.call(byId, id)) {
        merged.push(byId[id]);
      }
    }
    merged.sort(function (a, b) {
      return a.id - b.id;
    });
    return merged;
  }

  // --- the record of an unresolved turn -------------------------------------------------------------

  function createPendingStore(storage, nowFn) {
    var now = nowFn || Date.now;

    var expired = 0; // records the last load found older than they may be

    function isRecord(item) {
      return (
        isObject(item) &&
        typeof item.u === "string" && item.u.length > 0 && item.u.length <= MAX_USER_ID_LENGTH &&
        typeof item.c === "string" && UUID_PATTERN.test(item.c) &&
        typeof item.k === "string" && KEY_PATTERN.test(item.k) &&
        typeof item.t === "string" && item.t.length > 0 && item.t.length <= MAX_MESSAGE_LENGTH * 2 &&
        countCodePoints(item.t) <= MAX_MESSAGE_LENGTH &&
        isCount(item.b) &&
        typeof item.at === "number" && isFinite(item.at) &&
        item.at <= now() + 60000
      );
    }

    // What is stored for `owner` (the user_id /auth/me named), if it can be used. A value that is
    // not a record of this page, and every record that is damaged, too old or SOMEBODY ELSE'S, is
    // removed from the storage, not just left out of the answer: the text of a message is not kept
    // for longer than it can be of use, and never for the eyes of another person. (Another
    // person's record is judged before its age, so that the age of what is not yours is not told.)
    function read(owner) {
      expired = 0;
      var raw;
      try {
        raw = storage ? storage.getItem(STORAGE_KEY) : null;
      } catch (ignored) {
        return []; // the storage cannot be touched: there is nothing to tidy either
      }
      if (raw === null || raw === undefined) {
        return [];
      }
      var parsed = parseJson(raw);
      if (!parsed.ok || !isObject(parsed.value) || parsed.value.v !== 1 || !Array.isArray(parsed.value.turns)) {
        write([]);
        return [];
      }
      var records = [];
      for (var i = 0; i < parsed.value.turns.length; i++) {
        var item = parsed.value.turns[i];
        if (!isRecord(item) || typeof owner !== "string" || item.u !== owner) {
          continue;
        }
        if (now() - item.at > STORAGE_MAX_AGE_MS) {
          expired++;
          continue;
        }
        records.push({
          owner: item.u,
          conversationId: item.c.toLowerCase(),
          key: item.k,
          text: item.t,
          baseline: item.b,
          at: item.at,
        });
      }
      records = records.slice(-STORAGE_MAX_RECORDS); // the newest, as when writing
      if (records.length !== parsed.value.turns.length) {
        write(records);
      }
      return records;
    }

    // True if the storage now holds what it was given (nothing to hold is also fine).
    function write(records) {
      try {
        if (!storage) {
          return records.length === 0;
        }
        if (records.length === 0) {
          storage.removeItem(STORAGE_KEY);
          return true;
        }
        var turns = records.slice(-STORAGE_MAX_RECORDS).map(function (record) {
          return { u: record.owner, c: record.conversationId, k: record.key, t: record.text, b: record.baseline, at: record.at };
        });
        storage.setItem(STORAGE_KEY, JSON.stringify({ v: 1, turns: turns }));
        return true;
      } catch (ignored) {
        return false; // full, off or blocked: the page works without it, and says so
      }
    }

    return {
      load: read,
      save: write,
      expired: function () {
        return expired;
      },
      clear: function () {
        write([]);
      },
    };
  }

  // --- the controller -------------------------------------------------------------------------------

  function createController(deps) {
    deps = deps || {};
    var nowFn = deps.now || Date.now;
    var scheduleFn = deps.setTimeout || setTimeout;
    var cancelFn = deps.clearTimeout || clearTimeout;
    var AbortCtor = deps.AbortController !== undefined
      ? deps.AbortController
      : typeof AbortController === "function" ? AbortController : null;
    var cryptoApi = deps.crypto || null;
    var maxLength = deps.maxLength || MAX_MESSAGE_LENGTH;
    var store = createPendingStore(deps.storage || null, nowFn);

    var epoch = 0; // bumped when the session ends: answers that arrive after it are ignored
    var timers = {};
    var timerSeq = 0;
    var inflight = new Set();
    var sessionEndsAt = null; // when /auth/me said the session ends (ms since the epoch), while one is open
    var principal = null; // the user_id /auth/me named when this page entered the app: whose page it is
    // Whose sign-out has been asked for and not confirmed (the principal at the moment of asking), while
    // one is. Sent with every try of that sign-out as the server's guard, and never replaced by anybody
    // who signs in meanwhile. Memory only: not stored anywhere, and dropped when the sign-out is settled
    // or the page starts over.
    var logoutPrincipal = null;
    var generation = 0; // counts the beginnings (start) of a page: a draft the page holds back is only given back within one
    var verifyingEpoch = null; // the epoch of the /auth/me question that is out, if any
    var expiredRecords = 0; // stored records found too old when the page was opened
    var state = freshState("loading");

    function freshState(screen) {
      return {
        screen: screen,
        // While set, the signed-in page is held back: nothing of the conversations is in the view and
        // nothing is asked for them, until /auth/me has said who is signed in. { phase: "checking" } while
        // the question is out; { phase: "unavailable", failure } when it could not be answered.
        gate: null,
        notice: null,
        failure: null,
        list: { items: [], hasMore: false, status: "idle", error: null },
        currentId: null,
        threads: {},
        pending: {},
        creating: false,
        composerError: null,
        banner: null,
      };
    }

    function emit() {
      if (deps.onChange) {
        deps.onChange();
      }
    }

    function announce(text) {
      if (deps.announce) {
        deps.announce(text);
      }
    }

    function track(promise) {
      inflight.add(promise);
      var forget = function () {
        inflight.delete(promise);
      };
      promise.then(forget, forget);
      return promise;
    }

    // (A timer that comes due while the page is held back does nothing: the work it stood for is
    // picked up again by letThrough, once it is known that the same person is still signed in.)
    function later(ms, fn) {
      var mine = epoch;
      var id = ++timerSeq;
      timers[id] = scheduleFn(function () {
        delete timers[id];
        if (mine === epoch && !state.gate) {
          fn();
        }
      }, ms);
      return id;
    }

    function cancelTimer(id) {
      if (id && timers[id] !== undefined) {
        cancelFn(timers[id]);
        delete timers[id];
      }
    }

    function cancelAllTimers() {
      Object.keys(timers).forEach(function (id) {
        cancelFn(timers[id]);
        delete timers[id];
      });
    }

    // --- one HTTP call -----------------------------------------------------------------------

    async function http(method, path, options) {
      options = options || {};
      var headers = { Accept: "application/json" };
      var init = { method: method, headers: headers, credentials: "same-origin", cache: "no-store", redirect: "error" };
      if (options.headers) {
        Object.keys(options.headers).forEach(function (name) {
          headers[name] = options.headers[name];
        });
      }
      if (options.json !== undefined) {
        headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(options.json);
      }
      var timedOut = false;
      var timer = null;
      if (AbortCtor && options.timeoutMs) {
        var aborter = new AbortCtor();
        init.signal = aborter.signal;
        timer = scheduleFn(function () {
          timedOut = true;
          aborter.abort();
        }, options.timeoutMs);
      }
      function finish() {
        if (timer !== null) {
          cancelFn(timer);
          timer = null;
        }
      }
      var response;
      try {
        response = await deps.fetch(path, init);
      } catch (ignored) {
        finish();
        return { kind: "network", timedOut: timedOut };
      }
      var text;
      try {
        text = await response.text();
      } catch (ignored) {
        // The headers arrived and the body did not: the outcome of a POST is unknown.
        finish();
        return { kind: "network", timedOut: timedOut, partial: true };
      }
      finish();
      var parsed = parseJson(text);
      return {
        kind: "http",
        status: response.status,
        jsonOk: parsed.ok,
        json: parsed.value,
        retryAfter: response.headers && response.headers.get ? response.headers.get("Retry-After") : null,
      };
    }

    // --- data helpers -----------------------------------------------------------------------

    function ensureThread(id) {
      if (!state.threads[id]) {
        state.threads[id] = {
          id: id,
          messages: [],
          hasMore: false,
          nextBefore: null,
          status: "idle", // idle | loading | ready | error
          // The newest page of the history has been read successfully in this session (or the
          // conversation was just made, empty): only then is it written to. A failed or
          // missing read is not an empty conversation.
          synced: false,
          error: null,
          loadingOlder: false,
          olderError: null,
          latest: null,
          watch: null,
          stalled: false,
          correlations: {}, // key -> the correlation id of the 5xx that was the answer to it, for the message the history took over
        };
      }
      return state.threads[id];
    }

    function lastMessageId(thread) {
      return thread && thread.messages.length ? thread.messages[thread.messages.length - 1].id : 0;
    }

    function applyLatest(thread, page) {
      var existing = thread.messages;
      var incoming = page.messages;
      var newest = existing.length ? existing[existing.length - 1].id : 0;
      // More than a page of news since the cache: the cache and the news do not meet.
      var gap = existing.length > 0 && page.hasMore && incoming.length > 0 && incoming[0].id > newest;
      if (gap || existing.length === 0) {
        thread.messages = incoming.slice();
        thread.hasMore = page.hasMore;
        thread.nextBefore = page.nextBefore;
        return;
      }
      var earliest = existing[0].id;
      thread.messages = mergeMessages(existing, incoming);
      if (incoming.length > 0 && incoming[0].id < earliest) {
        thread.hasMore = page.hasMore;
        thread.nextBefore = page.nextBefore;
      }
    }

    function resetData() {
      var screen = state.screen;
      state = freshState(screen);
    }

    // --- the stored record of unresolved turns -----------------------------------------------

    function persist() {
      var records = [];
      Object.keys(state.pending).forEach(function (id) {
        var turn = state.pending[id];
        records.push({
          owner: turn.owner,
          conversationId: turn.conversationId,
          key: turn.key,
          text: turn.text,
          baseline: turn.baseline,
          at: turn.sentAt,
        });
      });
      records.sort(function (a, b) {
        return a.at - b.at;
      });
      var saved = store.save(records);
      Object.keys(state.pending).forEach(function (id) {
        state.pending[id].saved = saved;
      });
    }

    // Only this person's records come back (the store removes anybody else's as it reads).
    function restorePending() {
      store.load(principal).forEach(function (record) {
        state.pending[record.conversationId] = {
          owner: record.owner,
          conversationId: record.conversationId,
          key: record.key,
          text: record.text,
          baseline: record.baseline,
          phase: "uncertain",
          reason: "restored",
          sentAt: record.at,
          startedAt: record.at,
          attempts: 0,
          failure: null,
          checking: false,
          timer: null,
          waitCode: null,
          saved: true,
        };
      });
      expiredRecords = store.expired();
    }

    function removePending(id) {
      var turn = state.pending[id];
      if (turn) {
        cancelTimer(turn.timer);
        delete state.pending[id];
        persist();
      }
    }

    // --- screens -----------------------------------------------------------------------------

    function showLogin(notice) {
      cancelAllTimers();
      state.screen = "login";
      state.notice = notice;
      state.failure = null;
      emit();
    }

    // `retry` is what the button of this screen does: "start" (ask who is signed in, from the
    // beginning) or "logout" (leave again).
    function showUnavailable(found) {
      cancelAllTimers();
      state.screen = "unavailable";
      state.failure = { text: found.text, correlationId: found.correlationId || null, retry: found.retry || "start" };
      emit();
    }

    // The session ended (401 anywhere, or its deadline passed): everything in memory is
    // dropped, so nothing of the last person is on screen and no answer still on its way
    // can bring it back (the epoch); the record of unresolved turns stays, to be resolved
    // after signing in again -- with the same keys, so a message that may have run is not
    // sent as a new one.
    function handleAuthLost() {
      epoch++;
      principal = null;
      logoutPrincipal = null;
      sessionEndsAt = null;
      cancelAllTimers();
      resetData();
      showLogin({ kind: "error", text: TEXT.authExpired });
    }

    // Is the session still open by the clock? The deadline is absolute (/auth/me), so a page
    // that was suspended and woke up late finds out here at once, before it asks the server
    // anything. The page calls this every second and when it is shown again.
    function checkSession() {
      if (state.screen !== "app" || sessionEndsAt === null || nowFn() < sessionEndsAt) {
        return true;
      }
      handleAuthLost();
      return false;
    }

    // The page is coming back and the server is about to be asked who is signed in. From now
    // until it has answered, nothing of the conversations is in the view and nothing is asked
    // for them (the timers are stopped; letThrough picks the work up again): the browser's one
    // session may have been replaced by somebody else's while the page was away, and it is not
    // known who is looking until the server says. What is in memory stays, but out of sight.
    function holdBack() {
      cancelAllTimers();
      Object.keys(state.threads).forEach(function (id) {
        state.threads[id].watch = null; // (its timer is gone; a new look is made when it is let through)
      });
      state.gate = { phase: "checking", failure: null };
      emit();
    }

    // The server says the same person is still signed in: the conversations come back as they
    // were (unresolved messages included, with their keys), and what was waiting for the clock
    // or for a look at the history goes on.
    function letThrough() {
      state.gate = null;
      Object.keys(state.pending).forEach(function (id) {
        var turn = state.pending[id];
        if (turn.phase === "waiting") {
          track(attempt(id));
        } else if (turn.phase === "uncertain" && state.currentId === id) {
          track(reconcile(id));
        }
      });
      var open = state.currentId === null ? null : state.threads[state.currentId];
      if (open && !open.synced) {
        track(loadLatest(open.id)); // (opened, or being opened, while it was held back)
      } else if (open) {
        maybeWatch(open.id);
      }
      emit();
    }

    // Ask the server who is signed in. (The session can end before its deadline: signing out
    // in another tab, a revoked session; and the session of the browser can become somebody
    // else's.) A definite 401 ends the session. The same person (user_id) lets the page
    // through. Another person starts the page over, as if it had just been opened: nothing of the
    // last person is kept, and an answer still on its way for them changes nothing (epoch). An
    // answer that cannot say keeps the page held back and says so, without claiming anything.
    async function verifySession() {
      if (state.screen !== "app" || verifyingEpoch === epoch) {
        return;
      }
      var mine = epoch;
      verifyingEpoch = mine;
      try {
        var response = await http("GET", "/auth/me", { timeoutMs: READ_TIMEOUT_MS });
        if (mine !== epoch) {
          return;
        }
        var found = classifyMe(response, nowFn());
        if (found.type === "auth") {
          handleAuthLost();
        } else if (found.type === "ok") {
          if (principal === null || found.userId !== principal) {
            track(start());
          } else {
            sessionEndsAt = found.expiresAt;
            letThrough();
          }
        } else {
          state.gate = {
            phase: "unavailable",
            failure: { text: TEXT.unverified + " " + found.text, correlationId: found.correlationId || null, retry: "verify" },
          };
          emit();
        }
      } finally {
        if (verifyingEpoch === mine) {
          verifyingEpoch = null;
        }
      }
    }

    // --- start ---------------------------------------------------------------------------------

    async function start() {
      epoch++; // whatever an earlier start left in flight is not wanted any more
      generation++;
      principal = null;
      logoutPrincipal = null; // (a sign-out that was not confirmed is over: who is signed in is asked afresh)
      sessionEndsAt = null;
      cancelAllTimers();
      resetData(); // nothing of an earlier page (a restored one, perhaps somebody else's) is kept
      var mine = epoch;
      state.screen = "loading";
      emit();
      try {
        var response = await http("GET", "/auth/me", { timeoutMs: READ_TIMEOUT_MS });
        if (mine !== epoch) {
          return;
        }
        var found = classifyMe(response, nowFn());
        if (found.type === "ok") {
          principal = found.userId;
          sessionEndsAt = found.expiresAt;
          await enterApp();
        } else if (found.type === "auth") {
          showLogin(found.expired ? { kind: "error", text: TEXT.authExpired } : null);
        } else {
          showUnavailable(found);
        }
      } catch (ignored) {
        // A bug must not leave the page on "loading" for ever.
        if (mine === epoch) {
          showUnavailable({ text: TEXT.unexpected, correlationId: null });
        }
      }
    }

    async function enterApp() {
      var mine = epoch;
      resetData();
      state.screen = "app";
      restorePending();
      emit();
      await loadList();
      if (mine !== epoch) {
        return;
      }
      var first = chooseInitialConversation();
      if (first !== null) {
        await selectConversation(first);
      } else {
        emit();
      }
      // Records that were too old to keep were removed: say so, once, rather than let a message go
      // without a word (the history shows whether it arrived).
      if (mine === epoch && expiredRecords > 0 && state.banner === null) {
        state.banner = { kind: "info", text: TEXT.expiredRecords, correlationId: null };
        emit();
      }
    }

    function chooseInitialConversation() {
      var newest = null;
      Object.keys(state.pending).forEach(function (id) {
        if (newest === null || state.pending[id].sentAt > state.pending[newest].sentAt) {
          newest = id;
        }
      });
      if (newest !== null) {
        return newest;
      }
      return state.list.items.length ? state.list.items[0].id : null;
    }

    // --- the list ------------------------------------------------------------------------------

    async function loadList() {
      if (state.gate) {
        return false; // held back: nothing is asked for the conversations until it is known who is signed in
      }
      var mine = epoch;
      state.list.status = "loading";
      state.list.error = null;
      emit();
      var response = await http("GET", "/api/conversations?limit=" + PAGE_SIZE, { timeoutMs: READ_TIMEOUT_MS });
      if (mine !== epoch) {
        return false;
      }
      var found = classifyRead(response);
      if (found.type === "ok") {
        var list = normalizeConversationList(found.body);
        if (list !== null) {
          state.list.items = list.items;
          state.list.hasMore = list.hasMore;
          state.list.status = "ready";
          emit();
          return true;
        }
        found = { type: "unavailable", text: TEXT.unexpected, correlationId: null };
      }
      if (found.type === "auth") {
        handleAuthLost();
        return false;
      }
      state.list.status = "error";
      state.list.error = { text: found.text || TEXT.listFailed, correlationId: found.correlationId || null };
      emit();
      return false;
    }

    function touchListItem(id, time) {
      var items = state.list.items;
      for (var i = 0; i < items.length; i++) {
        if (items[i].id === id) {
          var item = items.splice(i, 1)[0];
          if (typeof time === "number") {
            item.updatedAt = time;
          }
          items.unshift(item);
          return;
        }
      }
    }

    // --- opening a conversation -------------------------------------------------------------------

    async function selectConversation(id) {
      if (state.screen !== "app" || state.creating) {
        return;
      }
      state.currentId = id;
      state.banner = null;
      state.composerError = null;
      if (id === null) {
        emit();
        return;
      }
      ensureThread(id);
      emit();
      await loadLatest(id);
    }

    function loadLatest(id) {
      var thread = ensureThread(id);
      if (thread.latest) {
        return thread.latest;
      }
      if (state.gate) {
        return Promise.resolve(false); // held back (see loadList); letThrough reads it when it is known
      }
      var run = fetchLatest(id).then(
        function (value) {
          thread.latest = null;
          return value;
        },
        function (problem) {
          thread.latest = null;
          throw problem;
        }
      );
      thread.latest = track(run);
      return thread.latest;
    }

    async function fetchLatest(id) {
      var mine = epoch;
      var thread = ensureThread(id);
      if (thread.messages.length === 0) {
        thread.status = "loading";
      }
      thread.error = null;
      emit();
      var response = await http(
        "GET",
        "/api/conversations/" + id + "/messages?limit=" + PAGE_SIZE,
        { timeoutMs: READ_TIMEOUT_MS }
      );
      if (mine !== epoch) {
        return false;
      }
      var found = classifyRead(response);
      if (found.type === "ok") {
        var page = normalizeMessagePage(found.body, id);
        if (page !== null) {
          applyLatest(thread, page);
          thread.status = "ready";
          thread.synced = true;
          thread.error = null;
          adoptPending(id);
          maybeWatch(id);
          emit();
          return true;
        }
        found = { type: "unavailable", text: TEXT.unexpected, correlationId: null };
      }
      if (found.type === "auth") {
        handleAuthLost();
        return false;
      }
      if (found.type === "not_found") {
        conversationGone(id);
        return false;
      }
      thread.error = { text: found.text || TEXT.threadFailed, correlationId: found.correlationId || null };
      if (thread.messages.length === 0) {
        thread.status = "error";
      }
      emit();
      return false;
    }

    function conversationGone(id) {
      var turn = state.pending[id];
      if (turn) {
        removePending(id);
      }
      delete state.threads[id];
      state.list.items = state.list.items.filter(function (item) {
        return item.id !== id;
      });
      if (state.currentId === id) {
        state.currentId = null;
        state.banner = { kind: "error", text: TEXT.conversationGone, correlationId: null };
      }
      emit();
    }

    async function loadOlder() {
      var mine = epoch;
      var id = state.currentId;
      var thread = id === null ? null : state.threads[id];
      if (state.gate || !thread || !thread.hasMore || thread.nextBefore === null || thread.loadingOlder) {
        return;
      }
      thread.loadingOlder = true;
      thread.olderError = null;
      emit();
      var response = await http(
        "GET",
        "/api/conversations/" + id + "/messages?limit=" + PAGE_SIZE + "&before=" + thread.nextBefore,
        { timeoutMs: READ_TIMEOUT_MS }
      );
      if (mine !== epoch) {
        return;
      }
      thread.loadingOlder = false;
      var found = classifyRead(response);
      if (found.type === "ok") {
        var page = normalizeMessagePage(found.body, id);
        if (page !== null) {
          thread.messages = mergeMessages(thread.messages, page.messages);
          thread.hasMore = page.hasMore;
          thread.nextBefore = page.nextBefore;
          emit();
          return;
        }
        found = { type: "unavailable", text: TEXT.unexpected, correlationId: null };
      }
      if (found.type === "auth") {
        handleAuthLost();
        return;
      }
      if (found.type === "not_found") {
        conversationGone(id);
        return;
      }
      thread.olderError = { text: found.text || TEXT.olderFailed, correlationId: found.correlationId || null };
      emit();
    }

    // --- watching a reply that is being prepared (read-only) ------------------------------------------

    // The last message is the person's own and the server is still working on it (a reload,
    // another tab): look again now and then, for a couple of minutes. Only reads; a message
    // that failed in the meantime is the person's to send again.
    function serverIsWorking(thread, turn) {
      if (turn) {
        return false; // the pending turn is the one in charge
      }
      var last = thread.messages[thread.messages.length - 1];
      return Boolean(last) && last.role === "user" && last.status === "in_progress";
    }

    function maybeWatch(id) {
      var thread = state.threads[id];
      if (!thread || thread.watch || state.currentId !== id || state.gate) {
        return;
      }
      thread.stalled = false;
      if (!serverIsWorking(thread, state.pending[id])) {
        return;
      }
      thread.watch = { tries: 0, since: nowFn(), timer: null };
      scheduleWatch(id);
    }

    function scheduleWatch(id) {
      var thread = state.threads[id];
      if (!thread || !thread.watch) {
        return;
      }
      var delay = WATCH_DELAYS_MS[Math.min(thread.watch.tries, WATCH_DELAYS_MS.length - 1)];
      thread.watch.tries++;
      thread.watch.timer = later(delay, function () {
        track(watchTick(id));
      });
    }

    async function watchTick(id) {
      var thread = state.threads[id];
      var watch = thread ? thread.watch : null;
      if (!watch) {
        return;
      }
      watch.timer = null;
      if (state.currentId !== id) {
        thread.watch = null; // looked at again when the person comes back to it
        return;
      }
      if (nowFn() - watch.since > WATCH_LIMIT_MS) {
        thread.watch = null;
        thread.stalled = true;
        emit();
        return;
      }
      await loadLatest(id); // (maybeWatch inside it sees the watch and leaves it alone)
      var after = state.threads[id];
      if (!after || after.watch !== watch) {
        return; // the session ended or the conversation went away meanwhile
      }
      if (serverIsWorking(after, state.pending[id])) {
        scheduleWatch(id);
      } else {
        after.watch = null;
        emit();
      }
    }

    // --- sending ------------------------------------------------------------------------------------

    function startTurn(id, text, key) {
      var thread = ensureThread(id);
      var now = nowFn();
      state.pending[id] = {
        owner: principal,
        conversationId: id,
        key: key,
        text: text,
        baseline: lastMessageId(thread),
        phase: "sending",
        reason: null,
        sentAt: now,
        startedAt: now,
        attempts: 0,
        failure: null,
        checking: false,
        timer: null,
        waitCode: null,
      };
      persist();
      emit();
      return track(attempt(id));
    }

    async function attempt(id) {
      var mine = epoch;
      var turn = state.pending[id];
      if (!turn) {
        return;
      }
      cancelTimer(turn.timer);
      turn.timer = null;
      turn.attempts++;
      turn.phase = "sending";
      turn.failure = null;
      turn.checking = false;
      turn.startedAt = nowFn();
      emit();
      var response = await http("POST", "/api/conversations/" + id + "/messages", {
        headers: { "Idempotency-Key": turn.key },
        json: { text: turn.text },
        timeoutMs: MESSAGE_TIMEOUT_MS,
      });
      if (mine !== epoch || state.pending[id] !== turn) {
        return;
      }
      settle(id, turn, classifyMessageSend(response, id));
    }

    function settle(id, turn, outcome) {
      switch (outcome.type) {
        case "success":
          applyTurn(id, outcome.turn);
          removePending(id);
          announce(TEXT.replied);
          emit();
          return;
        case "auth":
          handleAuthLost();
          return;
        case "not_found":
          conversationGone(id);
          return;
        case "busy":
          turn.waitCode = outcome.code;
          if (turn.attempts >= MAX_AUTO_ATTEMPTS) {
            turn.phase = "uncertain";
            turn.reason = "stalled";
          } else {
            turn.phase = "waiting";
            turn.timer = later(outcome.retryAfterMs, function () {
              track(attempt(id));
            });
          }
          emit();
          return;
        case "rejected":
          turn.phase = "rejected";
          turn.failure = { text: outcome.text, correlationId: outcome.correlationId, asNew: outcome.asNew, code: outcome.code };
          emit();
          return;
        default: // uncertain
          turn.phase = "uncertain";
          turn.reason = outcome.reason;
          // What the application said, if it was the application (a 5xx): kept for diagnostics and
          // shown as the number of the request. It is not a verdict on the turn.
          turn.failure = outcome.code
            ? { text: outcome.text, correlationId: outcome.correlationId, code: outcome.code }
            : null;
          emit();
          track(reconcile(id));
      }
    }

    // The exchange is in (a reply, or a replay of one): merge it by id, so that a history
    // read before it, or a replay after it, never shows a message twice.
    function applyTurn(id, turn) {
      var thread = ensureThread(id);
      thread.messages = mergeMessages(thread.messages, [turn.user, turn.assistant]);
      thread.status = "ready";
      thread.error = null;
      touchListItem(id, turn.assistant.createdAt);
      var known = state.list.items.some(function (item) {
        return item.id === id;
      });
      if (!known) {
        state.list.items.unshift({ id: id, title: null, createdAt: turn.user.createdAt, updatedAt: turn.assistant.createdAt });
      }
    }

    // Read the history to see what the server has of an unresolved turn. Read-only: it
    // never sends the message again.
    async function reconcile(id) {
      var turn = state.pending[id];
      if (!turn || turn.phase !== "uncertain" || state.gate) {
        return;
      }
      turn.checking = true;
      emit();
      await loadLatest(id);
      var again = state.pending[id];
      if (again) {
        again.checking = false;
        emit();
      }
    }

    // After a read of the history: an uncertain turn the server has a record of, under the
    // turn's own key, is the history's from here on (its own status and, if it failed, its
    // retry). That is the only thing a read can settle. A message that is not there proves
    // nothing (the read may be stale or partial, the request may still arrive), and a finished
    // exchange with the same words is not proof either (the history gives the key only of
    // messages that are not finished; another tab may have sent the same words): both leave
    // the turn as it is, to be settled by sending the same key again.
    function adoptPending(id) {
      var turn = state.pending[id];
      var thread = state.threads[id];
      if (!turn || !thread || turn.phase !== "uncertain") {
        return;
      }
      var messages = thread.messages;
      for (var i = 0; i < messages.length; i++) {
        if (messages[i].role === "user" && messages[i].key === turn.key) {
          if (turn.failure && turn.failure.correlationId) {
            thread.correlations[turn.key] = turn.failure.correlationId; // the number of the request that failed stays on the message
          }
          removePending(id);
          return;
        }
      }
    }

    // Has the server a finished exchange that looks like this turn's: the same words, newer than
    // the newest message known when it was sent, and answered? Only ever a hint to the person.
    function looksFinished(thread, turn) {
      if (!thread) {
        return false;
      }
      var messages = thread.messages;
      for (var i = 0; i < messages.length; i++) {
        var message = messages[i];
        if (message.role === "user" && message.status === "completed" && message.id > turn.baseline && message.text === turn.text) {
          for (var j = i + 1; j < messages.length; j++) {
            if (messages[j].role === "assistant") {
              return true;
            }
          }
        }
      }
      return false;
    }

    async function send(raw) {
      if (state.screen !== "app" || state.gate) {
        return { ok: false, reason: "not_ready" };
      }
      var prepared = prepareMessage(raw, maxLength);
      if (!prepared.ok) {
        state.composerError = prepared.message;
        emit();
        return { ok: false, reason: prepared.reason };
      }
      if (state.creating) {
        return { ok: false, reason: "busy" };
      }
      var id = state.currentId;
      if (id !== null && state.pending[id]) {
        return { ok: false, reason: "busy" };
      }
      // A conversation that exists is written to only once its newest page has been read: what
      // the server had when the message was sent is the baseline, and an empty page that never
      // loaded is not an empty conversation.
      if (id !== null && !(state.threads[id] && state.threads[id].synced)) {
        return { ok: false, reason: "history" };
      }
      var key;
      try {
        key = newIdempotencyKey(cryptoApi);
      } catch (ignored) {
        state.composerError = TEXT.noCrypto;
        emit();
        return { ok: false, reason: "no_crypto" };
      }
      var mine = epoch;
      state.composerError = null;
      state.banner = null;
      if (id === null) {
        state.creating = true;
        emit();
        var response = await http("POST", "/api/conversations", { json: {}, timeoutMs: READ_TIMEOUT_MS });
        if (mine !== epoch) {
          return { ok: false, reason: "stale" };
        }
        state.creating = false;
        var made = classifyCreate(response);
        if (made.type === "auth") {
          handleAuthLost();
          return { ok: false, reason: "auth" };
        }
        if (made.type !== "created") {
          // The text stays in the composer. An unclear outcome is not repeated: the list is
          // read again and the person decides.
          state.composerError = made.text || TEXT.createRejected;
          emit();
          if (made.type === "uncertain") {
            await loadList();
            if (mine === epoch) {
              state.banner = { kind: "info", text: TEXT.createUncertain, correlationId: made.correlationId || null };
              emit();
            }
          }
          return { ok: false, reason: made.type };
        }
        id = made.conversation.id;
        state.list.items = [made.conversation].concat(
          state.list.items.filter(function (item) {
            return item.id !== id;
          })
        );
        var created = ensureThread(id);
        created.status = "ready";
        created.synced = true; // made empty by the server a moment ago: its baseline is 0 for certain
        state.currentId = id;
        if (state.gate) {
          // The page was held back while the conversation was being made: the message is not sent
          // until it is known who is signed in. It stays in the box; the empty conversation is there.
          emit();
          return { ok: false, reason: "held" };
        }
      }
      return { ok: true, conversationId: id, done: startTurn(id, prepared.text, key) };
    }

    // The same message again, with the same key and the same text.
    function retry(id) {
      var turn = state.pending[id];
      if (state.gate || !turn || turn.phase === "sending" || turn.phase === "waiting" || turn.phase === "rejected") {
        return Promise.resolve();
      }
      turn.attempts = 0;
      return track(attempt(id));
    }

    // The message the server marked failed (it is the last one of the conversation, and the
    // history gives its key and its exact text): send it again, same key, same text.
    function retryMessage(id) {
      var thread = state.threads[id];
      if (state.gate || !thread || state.pending[id]) {
        return Promise.resolve();
      }
      var last = thread.messages[thread.messages.length - 1];
      if (!last || last.role !== "user" || last.status !== "failed" || last.key === null) {
        return Promise.resolve();
      }
      var now = nowFn();
      state.pending[id] = {
        owner: principal,
        conversationId: id,
        key: last.key,
        text: last.text,
        baseline: Math.max(0, last.id - 1),
        phase: "sending",
        reason: null,
        sentAt: now,
        startedAt: now,
        attempts: 0,
        failure: null,
        checking: false,
        timer: null,
        waitCode: null,
      };
      persist();
      emit();
      return track(attempt(id));
    }

    // The server said for certain that this key will not run again: the same text as a new
    // message, with a fresh key. Only the person's click gets here.
    function sendAsNew(id) {
      var turn = state.pending[id];
      if (state.gate || !turn || turn.phase !== "rejected" || !(turn.failure && turn.failure.asNew)) {
        return Promise.resolve();
      }
      var key;
      try {
        key = newIdempotencyKey(cryptoApi);
      } catch (ignored) {
        state.composerError = TEXT.noCrypto;
        emit();
        return Promise.resolve();
      }
      var thread = ensureThread(id);
      turn.key = key;
      turn.baseline = lastMessageId(thread);
      turn.sentAt = nowFn();
      turn.attempts = 0;
      persist();
      return track(attempt(id));
    }

    // Take the unresolved message back into the composer (the explicit discard): returns its
    // text, or null. Only for a message the server has refused for a reason of the request (a
    // 4xx): the key is given up there, and the text may be sent as a new message. While the
    // outcome is unknown (sending, waiting, uncertain -- a 5xx included) the key is the only
    // way to learn it and nothing may forget it.
    function discard(id) {
      var turn = state.pending[id];
      if (state.gate || !turn || turn.phase !== "rejected") {
        return null;
      }
      var text = turn.text;
      removePending(id);
      emit();
      return text;
    }

    // --- everything else ------------------------------------------------------------------------------

    // Leaving. The moment the person asks, they have left as far as this page is concerned: the
    // epoch moves on (answers still on their way change nothing), the conversations are gone from
    // memory and the view, the timers stop, and the record of unresolved messages is removed (as
    // it always was on an explicit logout). That does not wait for the server: its answer can be
    // lost after it revoked the session, or never come, and the page must not go back to the
    // conversations because it is not known what happened.
    //   2xx (or 401: there is no session)  the session is closed: the sign-in screen, saying so
    //   409 session_mismatch  the browser's session is not the one this page was signed in with
    //     (somebody else signed in, in another tab, after the answer was lost): the server ended
    //     nothing, so this sign-out is over -- it is not tried again and the other person is not
    //     signed out -- and the page says so and offers to begin again from "who is signed in?"
    //   anything else, a lost answer included  NOT known to be closed: a screen that says so and
    //     offers to leave again (the request is idempotent), and still no conversations.
    // Every try carries the person the page was signed in as when the sign-out was asked for
    // (X-Expected-User), so that the server ends that person's session or none: the cookie of a
    // retry may belong to somebody else by then.
    async function logout() {
      var again = state.screen === "unavailable" && state.failure !== null && state.failure.retry === "logout";
      if (!again && (state.screen !== "app" || state.gate)) {
        return false;
      }
      var expected = again ? logoutPrincipal : principal;
      if (typeof expected !== "string" || expected === "") {
        // (Not reachable: the page in the app always has a principal, and a retry has the one it kept.)
        // Never a sign-out without a guard: begin again from who is signed in.
        track(start());
        return false;
      }
      logoutPrincipal = expected;
      epoch++;
      generation++;
      principal = null;
      sessionEndsAt = null;
      cancelAllTimers();
      resetData();
      store.clear();
      var mine = epoch;
      state.screen = "signing_out";
      emit();
      var response = await http("POST", "/auth/logout", {
        timeoutMs: READ_TIMEOUT_MS,
        headers: { "X-Expected-User": expected },
      });
      if (mine !== epoch) {
        return false;
      }
      if (response.kind === "http" && ((response.status >= 200 && response.status < 300) || response.status === 401)) {
        logoutPrincipal = null;
        showLogin({ kind: "info", text: TEXT.loggedOut });
        return true;
      }
      if (response.kind === "http" && response.status === 409 && readAuthCode(response) === "session_mismatch") {
        logoutPrincipal = null;
        showUnavailable({ text: TEXT.logoutMismatch, correlationId: null, retry: "start" });
        return false;
      }
      showUnavailable({ text: TEXT.logoutUnconfirmed, correlationId: null, retry: "logout" });
      return false;
    }

    // Ask for a sign-in link. The answer is the same neutral one whoever the address belongs
    // to (the server makes sure of that); nothing is said here about the allowlist.
    async function requestEmailLink(raw) {
      var checked = checkEmail(raw);
      if (!checked.ok) {
        return { ok: false, kind: "invalid", text: TEXT.invalidEmail };
      }
      var response = await http("POST", "/auth/email/request", {
        json: { email: checked.address },
        timeoutMs: READ_TIMEOUT_MS,
      });
      if (response.kind === "network") {
        return { ok: false, kind: response.timedOut ? "timeout" : "network", text: unavailableText(response, null) };
      }
      var body = response.jsonOk && isObject(response.json) ? response.json : {};
      var shown = typeof body.message === "string" && body.message.length > 0 && body.message.length <= 600 ? body.message : null;
      if (response.status === 202 && body.detail === "link_requested") {
        return { ok: true, kind: "sent", text: shown || TEXT.emailSent };
      }
      var code = readAuthCode(response);
      if (response.status === 400 && code === "invalid_email") {
        return { ok: false, kind: "invalid", text: shown || TEXT.invalidEmail };
      }
      if (response.status === 403 && code === "forbidden_origin") {
        return { ok: false, kind: "forbidden", text: shown || messageForCode("forbidden_origin") };
      }
      if (response.status >= 500 || response.status === 503) {
        return { ok: false, kind: "unavailable", text: shown && code ? shown : TEXT.unavailable };
      }
      return { ok: false, kind: "error", text: code && shown ? shown : TEXT.unexpected };
    }

    function dismissBanner() {
      state.banner = null;
      emit();
    }

    // The person is typing again: the complaint about what they typed before is out of date.
    function clearComposerError() {
      if (state.composerError !== null) {
        state.composerError = null;
        emit();
      }
    }

    function reloadList() {
      return track(loadList());
    }

    function reloadThread() {
      var id = state.currentId;
      return id === null ? Promise.resolve() : loadLatest(id);
    }

    // The network is back, or the tab is visible again (a suspended tab included): first the
    // clock -- a deadline that passed while the page slept ends the session before anything is
    // asked -- then the page is held back and the server is asked who is signed in. The answer
    // decides (verifySession): the same person gets the conversations back as they were and the
    // work goes on (letThrough: the turns left unclear are read again, a reply being prepared is
    // watched again); another person gets a page started over; a 401 is the end of the session;
    // no answer leaves the page held back, with a button to ask again.
    function recheck() {
      if (!checkSession() || state.screen !== "app") {
        return;
      }
      if (!state.gate || state.gate.phase !== "checking") {
        holdBack();
      }
      track(verifySession());
    }

    // The button of the screens that say something could not be done: ask who is signed in
    // again (a held-back page), leave again (an unconfirmed logout), or begin from the start.
    function recover() {
      if (state.screen === "app" && state.gate) {
        recheck();
        return Promise.resolve();
      }
      if (state.screen === "unavailable" && state.failure !== null && state.failure.retry === "logout") {
        return logout();
      }
      return start();
    }

    function idle() {
      return (async function () {
        for (var rounds = 0; rounds < 1000; rounds++) {
          await Promise.resolve();
          if (inflight.size === 0) {
            return;
          }
          await Promise.all(Array.from(inflight).map(function (p) { return p.then(null, function () {}); }));
        }
      })();
    }

    // --- what the page draws ---------------------------------------------------------------------------

    function sendingNote(elapsedMs) {
      if (elapsedMs < 3000) {
        return TEXT.sending;
      }
      if (elapsedMs < 15000) {
        return TEXT.preparing;
      }
      if (elapsedMs < 45000) {
        return TEXT.slow;
      }
      return TEXT.slower;
    }

    function uncertainNote(turn, seen) {
      if (turn.checking) {
        return TEXT.checking;
      }
      if (seen) {
        return TEXT.uncertainSeen;
      }
      switch (turn.reason) {
        case "network":
          return TEXT.uncertainNetwork;
        case "timeout":
          return TEXT.uncertainTimeout;
        case "server":
          return TEXT.uncertainServer;
        case "stalled":
          return TEXT.stalled;
        case "restored":
          return TEXT.uncertainRestored;
        default:
          return TEXT.uncertainProxy;
      }
    }

    function describePending(turn, now, thread) {
      var item = {
        type: "pending",
        role: "user",
        text: turn.text,
        time: formatMoment(turn.sentAt, now),
        iso: isoTime(turn.sentAt),
        phase: turn.phase,
        note: "",
        warning: turn.saved === false ? TEXT.notSaved : "",
        correlationId: null,
        actions: [],
        busy: false,
      };
      if (turn.phase === "sending") {
        item.note = sendingNote(Math.max(0, now - turn.startedAt));
        item.busy = true;
      } else if (turn.phase === "waiting") {
        item.note = turn.waitCode === "turn_in_progress" ? TEXT.waitingOwn : TEXT.waitingBusy;
        item.busy = true;
      } else if (turn.phase === "uncertain") {
        item.note = uncertainNote(turn, looksFinished(thread, turn));
        item.busy = turn.checking;
        item.correlationId = turn.failure ? turn.failure.correlationId : null; // (the application's own 5xx)
        item.actions = ["retry"]; // never "edit": the key is all there is to find out what became of it
      } else if (turn.phase === "rejected") {
        item.note = (turn.failure && turn.failure.text) || TEXT.unexpected;
        item.correlationId = turn.failure ? turn.failure.correlationId : null;
        item.actions = (turn.failure && turn.failure.asNew ? ["asnew"] : []).concat(["edit"]);
      }
      return item;
    }

    function isoTime(time) {
      return typeof time === "number" ? new Date(time).toISOString() : "";
    }

    function buildTimeline(thread, turn, now) {
      var items = [];
      var messages = thread ? thread.messages : [];
      var last = messages.length - 1;
      // A turn restored from the storage is shown only once the history of its conversation has
      // been read for this session: that read is what shows the conversation is this person's
      // (a stranger's 404 removes the turn), and what the baseline is measured against.
      if (turn && !(thread && thread.synced)) {
        turn = null;
      }
      for (var i = 0; i < messages.length; i++) {
        var message = messages[i];
        if (turn && message.role === "user" && message.key === turn.key) {
          continue; // the pending turn is this message, and says more about it
        }
        var item = {
          type: "message",
          id: message.id,
          role: message.role,
          text: message.text,
          time: formatMoment(message.createdAt, now),
          iso: isoTime(message.createdAt),
          status: message.status,
          note: "",
          correlationId: null,
          canRetry: false,
        };
        if (message.role === "user" && message.status === "in_progress") {
          item.note = TEXT.preparing;
        } else if (message.role === "user" && message.status === "failed") {
          item.note = messageForCode(message.errorCode) || TEXT.failedGeneric;
          item.correlationId = (message.key !== null && thread.correlations[message.key]) || null;
          item.canRetry = i === last && message.key !== null && !turn;
        }
        items.push(item);
      }
      if (turn) {
        items.push(describePending(turn, now, thread));
      }
      return items;
    }

    // The view of a page that is held back: its own screen ("verifying" while the server is being
    // asked, "unverified" when it could not answer), and the rest in the same shape as ever so that
    // the page can read it, with nothing of the conversations in it -- not the list, not the
    // title, not a message, not a note, not the state of the composer.
    function heldView() {
      var checking = state.gate.phase === "checking";
      return {
        screen: checking ? "verifying" : "unverified",
        generation: generation,
        notice: null,
        failure: checking ? null : state.gate.failure,
        banner: null,
        currentId: null,
        busy: false,
        maxLength: maxLength,
        chatTitle: "",
        list: { status: "idle", error: null, hasMore: false, draftCurrent: false, items: [] },
        thread: {
          id: null,
          status: "ready",
          error: null,
          hasMore: false,
          loadingOlder: false,
          olderError: null,
          stalled: false,
          watching: false,
          empty: false,
          items: [],
        },
        composer: { locked: true, creating: false, hint: "", error: null, max: maxLength },
      };
    }

    function getView() {
      if (state.screen === "app" && state.gate) {
        return heldView();
      }
      var now = nowFn();
      var view = {
        screen: state.screen,
        generation: generation,
        notice: state.notice,
        failure: state.failure,
        banner: state.banner,
        currentId: state.currentId,
        busy: false,
        maxLength: maxLength,
      };
      var current = state.currentId;
      view.list = {
        status: state.list.status,
        error: state.list.error,
        hasMore: state.list.hasMore,
        draftCurrent: current === null,
        items: state.list.items.map(function (item) {
          var turn = state.pending[item.id];
          var meta = item.updatedAt === null ? "" : formatMoment(item.updatedAt, now);
          if (turn) {
            meta = turn.phase === "sending" || turn.phase === "waiting" ? "готовится ответ…" : "ждёт решения";
          }
          return {
            id: item.id,
            label: conversationLabel(item, now),
            meta: meta,
            current: item.id === current,
          };
        }),
      };
      view.chatTitle = "Новый разговор";
      view.list.items.forEach(function (item) {
        if (item.current) {
          view.chatTitle = item.label;
        }
      });
      if (current !== null && view.chatTitle === "Новый разговор") {
        view.chatTitle = "Разговор"; // not in the list (older than the list shows)
      }
      var thread = current === null ? null : state.threads[current] || null;
      var turn = current === null ? null : state.pending[current] || null;
      var timeline = buildTimeline(thread, turn, now);
      view.thread = {
        id: current,
        status: thread ? thread.status : "ready",
        error: thread ? thread.error : null,
        hasMore: Boolean(thread && thread.hasMore),
        loadingOlder: Boolean(thread && thread.loadingOlder),
        olderError: thread ? thread.olderError : null,
        stalled: Boolean(thread && thread.stalled),
        watching: Boolean(thread && thread.watch),
        empty: timeline.length === 0 && (!thread || thread.status === "ready"),
        items: timeline,
      };
      Object.keys(state.pending).forEach(function (id) {
        var phase = state.pending[id].phase;
        if (phase === "sending" || phase === "waiting") {
          view.busy = true;
        }
      });
      if (view.thread.watching) {
        view.busy = true;
      }
      var working = thread !== null && serverIsWorking(thread, turn);
      // Writing to a conversation that exists waits for the first successful read of its history.
      var readable = current === null || Boolean(thread && thread.synced);
      var hint = "";
      if (!readable) {
        hint = thread && thread.error ? TEXT.historyUnavailable : TEXT.historyLoading;
      } else if (turn) {
        if (turn.phase === "sending" || turn.phase === "waiting") {
          hint = "Дождитесь ответа помощника.";
        } else if (turn.phase === "uncertain") {
          hint = TEXT.hintUncertain;
        } else {
          hint = "Сначала решите, что делать с сообщением выше.";
        }
      } else if (working) {
        hint = "Дождитесь ответа помощника.";
      }
      view.composer = {
        locked: state.creating || Boolean(turn) || working || !readable,
        creating: state.creating,
        hint: hint,
        error: state.composerError,
        max: maxLength,
      };
      return view;
    }

    return {
      start: start,
      selectConversation: selectConversation,
      startDraft: function () {
        return selectConversation(null);
      },
      loadOlder: loadOlder,
      send: send,
      retry: retry,
      retryMessage: retryMessage,
      sendAsNew: sendAsNew,
      discard: discard,
      logout: logout,
      requestEmailLink: requestEmailLink,
      dismissBanner: dismissBanner,
      clearComposerError: clearComposerError,
      reloadList: reloadList,
      reloadThread: reloadThread,
      recheck: recheck,
      recover: recover,
      checkSession: checkSession,
      idle: idle,
      getView: getView,
      // for tests
      _state: function () {
        return state;
      },
      _logoutPrincipal: function () {
        return logoutPrincipal;
      },
    };
  }

  return {
    MAX_MESSAGE_LENGTH: MAX_MESSAGE_LENGTH,
    PAGE_SIZE: PAGE_SIZE,
    STORAGE_KEY: STORAGE_KEY,
    TEXT: TEXT,
    CODE_MESSAGES: CODE_MESSAGES,
    countCodePoints: countCodePoints,
    trimLikeServer: trimLikeServer,
    hasUnstorableCharacters: hasUnstorableCharacters,
    prepareMessage: prepareMessage,
    isValidIdempotencyKey: isValidIdempotencyKey,
    newIdempotencyKey: newIdempotencyKey,
    parseTimestamp: parseTimestamp,
    formatMoment: formatMoment,
    conversationLabel: conversationLabel,
    normalizeMessage: normalizeMessage,
    normalizeMessagePage: normalizeMessagePage,
    normalizeConversationList: normalizeConversationList,
    mergeMessages: mergeMessages,
    checkEmail: checkEmail,
    classifyMe: classifyMe,
    classifyRead: classifyRead,
    classifyCreate: classifyCreate,
    classifyMessageSend: classifyMessageSend,
    createPendingStore: createPendingStore,
    createController: createController,
  };
});
