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
 *  - Deleting a conversation is asked first, and only the second answer sends DELETE. A 404
 *    (already gone, or never this person's) is the outcome that was wanted: everything the page
 *    held of it is dropped and, when it was open, the page returns to a new empty draft. A
 *    favorite is the server's copy and is not touched. No bulk deletion, no trash, no undo.
 *  - A minimal record of each unresolved turn (whose it is, conversation id, key, exact
 *    text, the id of the newest message known when it was sent, the time) is kept in
 *    sessionStorage so a reload in the same tab can resolve it. It is removed when the
 *    turn resolves, on logout and when the person discards it (possible only for a message
 *    the server has refused); a record that is damaged, too old or another person's is
 *    removed when it is read, not just ignored, and is never shown to, or counted for, the
 *    person who is signed in. If the browser will not keep it, the page says so. Never a
 *    cookie, a token or a history.
 *  - Favorites (answers of the assistant that the person kept) are the server's: the page holds
 *    the list in memory, opens a recipe by asking for it, and keeps nothing of them in the
 *    browser. Keeping an answer names a conversation and a message and nothing else (the server
 *    copies the text); the server keeps one favorite per answer, so a save whose outcome is not
 *    known may simply be asked again, and so may a removal (a second one finds nothing, which is
 *    the outcome that was wanted). Everything of the favorites is dropped with the rest when the
 *    session ends, at "Выйти" and for another person, and is held back with the rest while it is
 *    asked who is signed in.
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
  var MAX_RECIPES_PER_MESSAGE = 5; // the server's MAX_RECIPES_PER_REPLY (protocol position limit)
  var MAX_EDIT_INGREDIENTS = 100; // the protocol's MAX_INGREDIENTS
  var MAX_EDIT_STEPS = 50; // the protocol's MAX_STEPS
  var MAX_EDIT_TITLE = 200; // the protocol's MAX_TITLE_LENGTH
  var MAX_EDIT_SERVINGS = 100; // the protocol's MAX_SERVINGS_LENGTH
  var MAX_EDIT_INGREDIENT_NAME = 200; // the protocol's MAX_INGREDIENT_NAME_LENGTH
  var MAX_EDIT_AMOUNT = 100; // the protocol's MAX_AMOUNT_LENGTH
  var MAX_EDIT_STEP = 1000; // the protocol's MAX_STEP_LENGTH
  var MAX_EDIT_TEXT = 100000; // the server's MAX_FAVORITE_TEXT_LENGTH
  var MAX_EDIT_NOTES = 2000; // the server's MAX_FAVORITE_NOTES_LENGTH
  var MAX_PROFILE_NAME = 200; // the server's users.display_name width, in code points
  var TEXT_SIZE_VALUES = { normal: true, large: true }; // the server's TextSize values
  // The approved personal household-measure ranges and system defaults (D18/D19): the
  // same numbers core.measures holds, repeated in persistence.profile. The profile read is
  // checked against them and the form validates against them before any PATCH.
  var MEASURE_LIMITS = {
    glass_ml: { min: 50, max: 1000, def: 250 },
    teaspoon_ml: { min: 1, max: 20, def: 5 },
    tablespoon_ml: { min: 5, max: 50, def: 15 },
  };
  var MEASURE_KEYS = ["glass_ml", "teaspoon_ml", "tablespoon_ml"];
  // Denominators whose exact decimal expansion ends within two places, with the factor
  // that turns the fraction into hundredths (4/5 -> 80 -> "0,8").
  var MEASURE_HUNDREDTHS_FACTOR = { 5: 20, 10: 10, 20: 5, 25: 4, 50: 2, 100: 1 };
  var MAX_SHOPPING_ITEMS = 200; // the server's shopping_lists.ITEMS_LIMIT
  var MAX_SHOPPING_NAME = 200; // the server's shopping_lists.MAX_NAME_LENGTH
  var MAX_SHOPPING_AMOUNT = 100; // the server's shopping_lists.MAX_AMOUNT_LENGTH
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

  // The one explanation of the ideas -> chosen dish -> full recipe -> favorites workflow.
  // It is the empty-conversation hint, and the help panel reuses it word for word so the
  // two cannot drift apart.
  var HELP_EXPLANATION =
    "Спросите, что приготовить из имеющихся продуктов, или как приготовить блюдо. " +
    "Помощник может предложить идеи блюд: выберите понравившееся — он даст полный рецепт, " +
    "который можно сохранить в избранное кнопкой рядом с рецептом.";

  var TEXT = {
    authExpired: "Сессия закончилась или была прекращена. Войдите снова.",
    unavailable: "Сервис временно недоступен. Попробуйте еще раз чуть позже.",
    network: "Нет связи с сервером. Проверьте интернет и попробуйте еще раз.",
    timeout: "Сервер слишком долго не отвечает. Попробуйте еще раз чуть позже.",
    unexpected: "Что-то пошло не так. Попробуйте еще раз чуть позже.",
    loggedOut: "Вы вышли из аккаунта.",
    // Said when the page has left the conversations but the server's answer to "Выйти" is not
    // known (or is not a success): it does not claim the session is closed.
    logoutUnconfirmed:
      "Не удалось подтвердить выход из аккаунта: сессия, возможно, еще открыта. " +
      "Разговоры скрыты. Нажмите «Выйти еще раз», когда появится связь.",
    // Said when a sign-out (the first try, or the repeat of an unconfirmed one) finds another person's session in
    // the browser: the server ended nothing. It says neither who signed in nor that the first session is closed
    // (it is not known).
    logoutMismatch:
      "Выход не выполнен: в этом браузере уже вошел другой пользователь, и его вход не затронут. " +
      "Прежние разговоры скрыты. Нажмите «Повторить», чтобы открыть страницу заново.",
    unverified:
      "Не удалось проверить, кто сейчас вошел в аккаунт, поэтому разговоры скрыты, пока это не выяснится.",
    retry: "Повторить",
    logoutAgain: "Выйти еще раз",
    loading: "Загружаем…",
    verifying: "Проверяем вход…",
    signingOut: "Выходим…",
    emptyMessage: "Напишите сообщение.",
    tooLong: "Сообщение слишком длинное (максимум {max} символов, сейчас {count}). Сократите его и отправьте снова.",
    badCharacters: "В сообщении есть недопустимые символы. Уберите их и отправьте снова.",
    noCrypto: "Браузер не смог подготовить безопасную отправку. Обновите страницу или откройте ее в другом браузере.",
    invalidEmail: "Введите корректный адрес электронной почты.",
    emailSent:
      "Если этот адрес есть в списке разрешенных, мы отправили на него письмо со ссылкой для входа. " +
      "Если письма нет, проверьте папку «Спам» или подождите немного и запросите ссылку снова.",
    listFailed: "Не удалось загрузить список разговоров.",
    threadFailed: "Не удалось загрузить разговор.",
    olderFailed: "Не удалось загрузить более ранние сообщения.",
    conversationGone: "Этот разговор больше недоступен.",
    createRejected: "Не удалось начать разговор.",
    deleteNamed: "Удалить разговор",
    deleteAsk:
      "Удалить этот разговор? Все его сообщения будут удалены без возможности восстановления. " +
      "Сохраненные рецепты в избранном останутся.",
    deleteConfirm: "Удалить",
    deleteCancel: "Отмена",
    deleting: "Удаляем…",
    deleteFailed: "Не удалось удалить разговор. Попробуйте еще раз.",
    deleted: "Разговор удален.",
    createUncertain:
      "Не удалось убедиться, что новый разговор создан. Ваше сообщение осталось в поле ввода, " +
      "список разговоров обновлен: проверьте, нет ли в нем нового разговора. " +
      "Если нет — нажмите «Отправить» еще раз.",
    replied: "Помощник ответил.",
    preparing: "Помощник готовит ответ…",
    sending: "Отправляем…",
    slow: "Ответ готовится дольше обычного. Подождите еще немного.",
    slower: "Все еще готовится. Ответ может занять до минуты.",
    waitingBusy: "В этом разговоре еще готовится предыдущий ответ. Подождите — отправим сообщение сами.",
    waitingOwn: "Ответ на это сообщение еще готовится. Подождите немного.",
    checking: "Проверяем, дошло ли сообщение…",
    stalled: "Ответ все еще не готов. Нажмите «Повторить» — сообщение не будет отправлено дважды.",
    uncertainNetwork:
      "Не удалось получить ответ: связь прервалась. Возможно, сообщение уже дошло. " +
      "Нажмите «Повторить» — оно не будет отправлено дважды.",
    uncertainTimeout:
      "Помощник отвечает дольше обычного, сообщение, возможно, еще обрабатывается. " +
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
      "Неподтвержденное сообщение старше 6 часов удалено из памяти страницы. " +
      "Проверьте историю разговора: возможно, оно дошло.",
    historyLoading: "Загружаем историю разговора. Отправка станет доступна, когда она загрузится.",
    historyUnavailable: "Не удалось загрузить историю разговора, поэтому отправка пока недоступна. Загрузите ее еще раз.",
    failedGeneric: "Не удалось получить ответ. Попробуйте еще раз.",
    correlation: "Номер обращения: {id}",
    emptyThread: HELP_EXPLANATION,
    // The help panel: the same explanation, reachable while a conversation is active.
    helpTitle: "Как пользоваться помощником",
    help: HELP_EXPLANATION,
    // The favorites: the recipes the person kept.
    chatsTab: "Разговоры",
    favoritesTab: "Избранное",
    favoritesLoading: "Загружаем избранное…",
    favoritesFailed: "Не удалось загрузить избранное.",
    favoritesEmpty:
      "В избранном пока ничего нет. Под ответом помощника нажмите «В избранное», чтобы сохранить рецепт.",
    // With structured recipes on, an answer is kept one recipe at a time, by a control named
    // after the recipe, so the hint must not promise one "В избранное" for the whole answer.
    favoritesEmptyStructured:
      "В избранном пока ничего нет. Под ответом помощника нажмите «Сохранить» рядом с рецептом, чтобы добавить его в избранное.",
    favoritesPick: "Выберите рецепт в списке.",
    favoriteKeep: "В избранное",
    favoriteKeeping: "Сохраняем…",
    favoriteKept: "Сохранено в избранном",
    favoriteOpen: "Открыть",
    favoriteKeptAnnounce: "Рецепт сохранен в избранное.",
    favoriteKeepFailed: "Не удалось сохранить в избранное. Попробуйте еще раз.",
    recipeLoading: "Загружаем рецепт…",
    recipeFailed: "Не удалось открыть рецепт.",
    recipeGone: "Этого рецепта больше нет в избранном.",
    removeAction: "Убрать из избранного",
    removeAsk: "Убрать этот рецепт из избранного?",
    removeConfirm: "Убрать",
    removeKeep: "Оставить",
    removing: "Убираем…",
    removeFailed: "Не удалось убрать рецепт. Попробуйте еще раз.",
    removed: "Рецепт убран из избранного.",
    savedOn: "Сохранено: {when}",
    // One answer may hold several recipes; each is kept separately.
    saveRecipeNamed: "Сохранить «{title}»",
    recipeSaving: "Сохраняем…",
    recipeSaved: "В избранном",
    recipeSaveFailed: "Не удалось сохранить рецепт. Попробуйте еще раз.",
    openRecipe: "Открыть",
    // The shopping lists: a draft of a recipe's ingredients, and the saved checklists.
    shoppingTab: "Покупки",
    shoppingTitle: "Мои покупки",
    shoppingDraftTitle: "Что купить?",
    shoppingPick: "Выберите список покупок в списке.",
    shoppingEmpty:
      "Пока нет списков покупок. Откройте рецепт и нажмите „Что купить?“",
    shoppingLoading: "Загружаем списки покупок…",
    shoppingFailed: "Не удалось загрузить списки покупок.",
    shoppingRetry: "Обновить покупки",
    shoppingListLoading: "Загружаем список…",
    shoppingListFailed: "Не удалось загрузить список покупок.",
    shoppingListGone: "Этот список покупок больше недоступен.",
    shoppingDraftLoading: "Загружаем рецепт…",
    shoppingDraftFailed: "Не удалось открыть рецепт для списка покупок.",
    shoppingDraftInstruction: "Отметьте продукты, которые уже есть дома",
    shoppingDraftUnsaved: "Изменения в списке не сохранены. Выйти без сохранения?",
    shoppingRemaining: "Проверить / купить: {count}",
    shoppingUncheckedNote: "Неотмеченные продукты нужно проверить перед покупками.",
    shoppingSave: "Сохранить список",
    shoppingSaving: "Сохраняем список…",
    shoppingSaved: "Список покупок сохранен",
    shoppingRetrySave: "Повторить",
    shoppingSaveNew: "Сохранить новый список",
    shoppingSaveAgain: "Сохранить снова",
    shoppingCancel: "Отмена",
    shoppingUnderstand: "Понятно",
    shoppingUncertain:
      "Не удалось получить подтверждение сохранения: связь прервалась. Возможно, список уже сохранен. " +
      "Нажмите «Повторить» — список не будет создан дважды.",
    shoppingTimeout:
      "Сервер слишком долго не отвечает на сохранение. Возможно, список уже сохранен. " +
      "Нажмите «Повторить» — список не будет создан дважды.",
    shoppingUncertainServer:
      "Сервис сообщил об ошибке, но список мог быть сохранен. Нажмите «Повторить» — он не будет создан дважды.",
    shoppingKeyReused:
      "Этот ключ запроса уже использован для другого списка покупок. Проверьте отмеченные продукты " +
      "и сохраните список заново.",
    shoppingDeletedElsewhere:
      "Этот список покупок уже удален. Сохраните новый список из рецепта.",
    shoppingFavoriteConflict:
      "Этот рецепт изменился. Вернитесь в избранное, откройте его заново и повторите.",
    shoppingDeletedNotice: "Список покупок удален.",
    atHome: "Есть дома",
    toBuy: "Проверить / купить",
    bought: "Куплено",
    listActive: "В работе",
    listCompleted: "Завершен",
    progress: "Куплено {purchased} из {total}",
    countToBuy: "Проверить / купить: {count}",
    countBought: "Куплено: {count}",
    countAtHome: "Есть дома: {count}",
    listUpdated: "Обновлено: {when}",
    completeList: "Завершить список",
    reopenList: "Вернуть в работу",
    deleteList: "Удалить список",
    deleteListAsk:
      "Удалить список «{title}»? Все его пункты будут удалены без возможности восстановления.",
    deleteListConfirm: "Удалить",
    deleteListKeep: "Оставить",
    deletingList: "Удаляем…",
    listDeleted: "Список покупок удален.",
    deleteListFailed: "Не удалось удалить список. Попробуйте еще раз.",
    listCompletedNote: "Список завершен: пункты нельзя изменить, пока список не возвращен в работу.",
    listCompletedNow: "Список завершен.",
    listReopened: "Список возвращен в работу.",
    completeListFailed: "Не удалось завершить список. Попробуйте еще раз.",
    reopenListFailed: "Не удалось вернуть список в работу. Попробуйте еще раз.",
    listChanged: "Список покупок изменился. Мы обновили его.",
    listStatusFailed: "Не удалось изменить список. Попробуйте еще раз.",
    addItem: "Добавить продукт",
    addItemName: "Название продукта",
    addItemAmount: "Количество (необязательно)",
    addItemSubmit: "Добавить",
    addItemCancel: "Отмена",
    addItemFailed: "Не удалось добавить продукт. Попробуйте еще раз.",
    addItemUncertain:
      "Не удалось подтвердить добавление: связь прервалась. Продукт мог быть добавлен. " +
      "Проверьте список; если его там нет, нажмите «Добавить» еще раз.",
    itemAdded: "Продукт добавлен.",
    itemRequired: "Введите название продукта.",
    itemNameTooLong: "Название слишком длинное (максимум 200 символов).",
    itemAmountTooLong: "Количество слишком длинное (максимум 100 символов).",
    itemBadCharacters: "В названии или количестве есть недопустимые символы.",
    itemEdit: "Изменить",
    itemSave: "Сохранить",
    itemEditCancel: "Отмена",
    itemEditFailed: "Не удалось сохранить изменения. Попробуйте еще раз.",
    itemSaved: "Изменения сохранены.",
    itemUpdated: "Пункт обновлен.",
    itemRemove: "Удалить",
    itemRemoveAsk: "Удалить этот пункт?",
    itemRemoveConfirm: "Удалить",
    itemRemoveKeep: "Оставить",
    itemRemoving: "Удаляем…",
    itemRemoved: "Пункт удален.",
    itemRemoveFailed: "Не удалось удалить пункт. Попробуйте еще раз.",
    itemStateFailed: "Не удалось изменить пункт. Попробуйте еще раз.",
    itemNameLabel: "Название",
    itemAmountLabel: "Количество",
    itemStateLabel: "Состояние",
    itemAddLabel: "Добавить продукт",
    itemsEmpty: "В списке пока нет пунктов.",
    // The card and its editing.
    servings: "Порции",
    ingredients: "Ингредиенты",
    steps: "Приготовление",
    notes: "Мои заметки",
    // KH-F01-B1: the person's own household measures under the ingredient list.
    measuresTitle: "В ваших мерах",
    measureExact: "Точно: {value}",
    measureApprox: "≈ {value}",
    measureUnitGlass: {
      one: "ваш стакан",
      few: "ваших стакана",
      many: "ваших стаканов",
      fraction: "вашего стакана",
    },
    measureUnitTeaspoon: {
      one: "ваша чайная ложка",
      few: "ваших чайных ложки",
      many: "ваших чайных ложек",
      fraction: "вашей чайной ложки",
    },
    measureUnitTablespoon: {
      one: "ваша столовая ложка",
      few: "ваших столовых ложки",
      many: "ваших столовых ложек",
      fraction: "вашей столовой ложки",
    },
    // KH-F01-B2: an ingredient-specific mass estimate names the unit itself, not "your"
    // measure: it is the unit the server selected from the person's capacities.
    massUnitGlass: {
      one: "стакан",
      few: "стакана",
      many: "стаканов",
      fraction: "стакана",
    },
    massUnitTeaspoon: {
      one: "чайная ложка",
      few: "чайные ложки",
      many: "чайных ложек",
      fraction: "чайной ложки",
    },
    massUnitTablespoon: {
      one: "столовая ложка",
      few: "столовые ложки",
      many: "столовых ложек",
      fraction: "столовой ложки",
    },
    massCountUnit: "шт.",
    sourceGlassQuestion: "Какой стакан имеется в виду в рецепте?",
    // KH-F01-B1.1 (15.7.4): what is missing, said once per recipe -- never a demand that
    // the person supply a fact the recipe omitted.
    sourceGlassExplanation:
      "В рецепте не указан объем стакана, поэтому мы не можем точно пересчитать эти количества.",
    sourceGlass200: "200 мл",
    sourceGlass250: "250 мл",
    sourceGlassOther: "Другой объем",
    // The safe option: no capacity is guessed, nothing is converted from an assumption.
    sourceGlassUnknown: "Не знаю — оставить исходное количество без пересчета",
    // A derived-domain failure is said by the component it belongs to (15.8.2/15.8.4): a
    // provenance failure never claims that step amounts were not calculated, and a step
    // failure never claims anything about the ingredients. Neither says the recipe was
    // checked, validated or consistent.
    provenanceNotChecked: "Не удалось определить, откуда взяты количества в ингредиентах.",
    stepsNotChecked: "Не удалось рассчитать количества в шагах. Текст шагов показан как есть.",
    // The whole-block summary failed and the client cannot say which component did it: a
    // neutral statement, without a claim about either component.
    analysisPartlyFailed:
      "Не удалось выполнить полный расчет количеств. Подсказки могут быть неполными.",
    sourceGlassOtherLabel: "Объем, мл",
    sourceGlassApply: "Применить",
    sourceGlassInvalid: "Укажите целое число от 50 до 1000 мл.",
    sourceGlassNote: "Выбор действует, пока открыт рецепт, и не сохраняется в профиле.",
    // A typed capacity is only a draft until its own read succeeds: it is never drawn as
    // the confirmed choice, and this says so beside the field.
    sourceGlassDraftHint: "Черновик: объем применится после нажатия «Применить».",
    sourceGlassConfirmed: "Выбрано: {value} мл",
    sourceGlassPending: "Применяем {value} мл…",
    sourceGlassFailed: "Не удалось применить объем. Попробуйте еще раз.",
    measuresRefreshPending: "Обновляем пересчет в ваших мерах…",
    measuresRefreshFailed: "Рецепт сохранен, но пересчет в ваших мерах не удалось обновить. Попробуйте еще раз.",
    editAction: "Редактировать",
    editTitleLabel: "Название",
    editServingsLabel: "Порции",
    editTextLabel: "Текст рецепта",
    editNotesLabel: "Мои заметки",
    editIngredientName: "Название ингредиента",
    editIngredientAmount: "Количество",
    editAddIngredient: "Добавить ингредиент",
    editRemoveIngredient: "Удалить ингредиент",
    editStep: "Шаг",
    editAddStep: "Добавить шаг",
    editRemoveStep: "Удалить шаг",
    editMoveUp: "Выше",
    editMoveDown: "Ниже",
    editSave: "Сохранить",
    editCancel: "Отмена",
    editSaving: "Сохраняем изменения…",
    editSaved: "Изменения сохранены.",
    editFailed: "Не удалось сохранить изменения. Попробуйте еще раз.",
    editTitleRequired: "Введите название рецепта.",
    editTitleTooLong: "Название слишком длинное (максимум 200 символов).",
    editTextRequired: "Введите текст рецепта.",
    editTextTooLong: "Текст рецепта слишком длинный (максимум 100000 символов).",
    editIngredientsRequired: "Добавьте хотя бы один ингредиент.",
    editTooManyIngredients: "Слишком много ингредиентов (максимум 100).",
    editIngredientIncomplete: "У каждого ингредиента должны быть название и количество.",
    editIngredientNameTooLong: "Название ингредиента слишком длинное (максимум 200 символов).",
    editIngredientAmountTooLong: "Количество слишком длинное (максимум 100 символов).",
    editServingsTooLong: "Порции указаны слишком длинно (максимум 100 символов).",
    editStepsRequired: "Добавьте хотя бы один шаг приготовления.",
    editTooManySteps: "Слишком много шагов (максимум 50).",
    editStepEmpty: "Шаг не должен быть пустым.",
    editStepTooLong: "Шаг слишком длинный (максимум 1000 символов).",
    editNotesTooLong: "Заметки слишком длинные (максимум 2000 символов).",
    editTooLong: "Одно из полей слишком длинное.",
    editConflict:
      "Рецепт изменился в другом месте. Загрузите новую версию или сохраните свои изменения поверх нее.",
    editReload: "Загрузить заново",
    editOverwrite: "Сохранить поверх",
    editUnsaved: "В рецепте есть несохраненные изменения. Уйти без сохранения?",
    // The profile: the name, the text size, the ways in, and the explanation of the data.
    profileTab: "Профиль",
    profileLoading: "Загружаем профиль…",
    profileFailed: "Не удалось загрузить профиль.",
    profileOthersWork: "Остальные разделы работают: разговоры, избранное и покупки доступны.",
    profileNameLabel: "Отображаемое имя",
    profileNameNotSet: "Имя не задано",
    profileNameHint: "От 1 до 200 символов. Невидимые и управляющие символы не допускаются.",
    profileNameEmpty: "Введите имя.",
    profileNameTooLong: "Имя слишком длинное (максимум 200 символов).",
    profileNameBadChars: "В имени есть невидимые или управляющие символы.",
    profileTextSizeLabel: "Размер текста",
    profileSizeNormal: "Обычный",
    profileSizeLarge: "Крупный (120%)",
    profileMeasuresTitle: "Мои кухонные меры",
    profileMeasureGlass: "Объем моего стакана, мл",
    profileMeasureTeaspoon: "Объем моей чайной ложки, мл",
    profileMeasureTablespoon: "Объем моей столовой ложки, мл",
    profileMeasureDefault: "по умолчанию",
    profileMeasurePersonal: "Указано вами",
    profileMeasureHint: "От {min} до {max} мл. Пустое поле — по умолчанию ({def} мл).",
    profileMeasureReset: "По умолчанию",
    profileMeasureResetLabel: "Вернуть значение по умолчанию: {name}",
    profileMeasureInvalid: "Укажите целое число от {min} до {max} мл или оставьте поле пустым.",
    profileEdit: "Изменить",
    profileSave: "Сохранить",
    profileSaving: "Сохраняем…",
    profileSaved: "Профиль сохранен.",
    profileSaveFailed: "Не удалось сохранить профиль. Попробуйте еще раз.",
    profileLoginTitle: "Способы входа",
    profileLoginHint: "Здесь показаны способы входа, которые уже использовались в этом аккаунте.",
    profileLoginEmpty: "Способы входа появятся здесь после первого входа.",
    profileLoginTelegram: "Telegram",
    profileLoginEmail: "Электронная почта",
    profileLoginOther: "Другой способ",
    profileStatusActive: "Активен",
    profileStatusRevoked: "Отозван",
    profileDataTitle: "Ваши данные",
    profileDataMessages:
      "Сообщения и фотографии к ним хранятся 365 дней с момента создания, после чего " +
      "автоматическая очистка удаляет их. Разговор не удаляется только из-за возраста: " +
      "старый разговор, в котором есть более новые сообщения, остается. Очистка удаляет " +
      "разговор, только когда в нем не осталось сообщений и им не пользовались 365 дней.",
    profileDataRecipes:
      "Ответы помощника с рецептами — это сообщения: каждый хранится 365 дней с момента " +
      "создания и удаляется автоматически, даже если сам разговор остается. " +
      "Рецепты, сохраненные в избранное, хранятся, пока вы сами их не удалите.",
    profileDataShopping:
      "Списки покупок автоматически не удаляются: они хранятся, пока вы сами их не удалите.",
    profileDataOpenAI:
      "Чтобы ответить, помощник отправляет в OpenAI сообщения текущего разговора и фотографии к ним. " +
      "Профиль, избранное и списки покупок в OpenAI не отправляются.",
    profileDataDelete:
      "Разговор можно удалить в меню рядом с его названием, рецепт — в избранном, " +
      "список покупок — в разделе «Покупки». Удаленное восстановить нельзя.",
    profileDataProfile:
      "Имя и размер текста хранятся на сервере в вашем профиле.",
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
    forbidden_origin: "Запрос отклонен: он пришел не с нашего сайта.",
    idempotency_key_required:
      "Не удалось отправить сообщение: у запроса нет ключа. Обновите страницу и попробуйте еще раз.",
    invalid_idempotency_key:
      "Не удалось отправить сообщение: ключ запроса некорректен. Обновите страницу и попробуйте еще раз.",
    empty_message: "Сообщение не должно быть пустым.",
    invalid_message_text: "В сообщении есть недопустимые символы.",
    message_too_long: "Сообщение слишком длинное (максимум {max_length} символов). Сократите его и отправьте снова.",
    invalid_query: "Параметры запроса указаны неверно.",
    invalid_display_name:
      "Имя должно быть непустым, не длиннее 200 символов и без невидимых или управляющих символов.",
    invalid_text_size: "Не удалось сохранить размер текста. Выберите «Обычный» или «Крупный».",
    invalid_measure:
      "Укажите объем целым числом в миллилитрах: стакан — от 50 до 1000, " +
      "чайная ложка — от 1 до 20, столовая ложка — от 5 до 50.",
    not_authenticated: "Войдите в аккаунт, чтобы продолжить.",
    conversation_not_found: "Разговор не найден.",
    message_not_found: "Этот ответ не найден: возможно, он уже удален.",
    favorite_not_found: "Этого рецепта нет в избранном.",
    recipe_not_found: "В этом ответе нет такого рецепта.",
    favorite_conflict: "Этот рецепт уже изменен. Обновите страницу и повторите.",
    favorites_full: "В избранном набралось максимальное число рецептов. Уберите ненужные, чтобы добавить новый.",
    shopping_list_not_found: "Список покупок не найден.",
    shopping_list_item_not_found: "Этого пункта нет в списке покупок.",
    shopping_list_completed: "Список покупок завершен. Откройте его, чтобы изменить.",
    shopping_lists_full:
      "Сохранено максимальное число списков покупок. Удалите ненужный, чтобы сохранить новый.",
    shopping_list_items_full:
      "В списке покупок максимальное число пунктов. Удалите ненужный, чтобы добавить новый.",
    shopping_list_key_reused:
      "Этот ключ запроса уже использован для другого списка покупок. Сохраните список заново.",
    shopping_list_deleted: "Этот список покупок уже удален. Сохраните новый список.",
    idempotency_key_reused: "Этот ключ запроса уже использован для другого сообщения. Отправьте сообщение заново.",
    conversation_busy: "В этом разговоре еще готовится предыдущий ответ. Подождите немного и повторите.",
    turn_in_progress: "Ответ на это сообщение еще готовится. Подождите немного.",
    turn_superseded:
      "Это сообщение уже нельзя повторить: в разговоре появились новые. Отправьте его заново как новое.",
    idempotency_key_unusable: "Этот запрос больше нельзя повторить. Отправьте сообщение заново.",
    provider_rate_limited: "Сейчас слишком много запросов к помощнику. Подождите немного и попробуйте снова.",
    provider_unreachable: "Не удалось связаться с помощником. Попробуйте еще раз чуть позже.",
    provider_timeout: "Помощник слишком долго готовил ответ. Попробуйте еще раз.",
    provider_empty_response:
      "Помощник вернул пустой ответ. Попробуйте переформулировать вопрос или повторить позже.",
    provider_error: "Помощник сейчас не может ответить. Попробуйте еще раз чуть позже.",
    interrupted: "Работа над ответом была прервана. Отправьте сообщение еще раз.",
    chat_unavailable: "Помощник сейчас недоступен.",
    attachment_not_found: "Фото не найдено: возможно, оно уже удалено.",
    attachment_unusable:
      "Это фото нельзя прикрепить: оно уже использовано или устарело. Отправьте его заново.",
    telegram_unavailable: "Подключение Telegram сейчас недоступно.",
    telegram_already_linked:
      "Telegram уже подключен. Сначала отключите его, чтобы подключить другой аккаунт.",
    service_unavailable: "Сервис временно недоступен. Попробуйте еще раз чуть позже.",
    internal_error: "Что-то пошло не так. Попробуйте еще раз чуть позже.",
    // Not in the catalogue: what the endpoints under /auth answer (a code, no message), and the
    // code of a turn that failed while its reply was being stored (the history names it).
    temporarily_unavailable: "Сервис временно недоступен. Попробуйте еще раз чуть позже.",
    login_unavailable: "Вход сейчас недоступен. Попробуйте еще раз чуть позже.",
    invalid_email: "Введите корректный адрес электронной почты.",
    server_error: "Что-то пошло не так. Попробуйте еще раз чуть позже.",
    storage_unavailable: "Сервис временно недоступен. Попробуйте еще раз чуть позже.",
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
  // A display name may hold no character of Unicode category C at all: not tab or
  // newline (a name is one line), not a zero-width or bidirectional mark, not a
  // surrogate, a private-use or an unassigned character (persistence.profile).
  var PROFILE_CONTROL = /\p{C}/u;

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

  // A code-point index as an index into the string's UTF-16 code units, or -1 when it is
  // outside [0, countCodePoints(text)]. The server counts its step spans in code points
  // (15.8.4); JavaScript's slicing counts code units, and an emoji is two of them. This is
  // the only conversion, so no raw server offset ever reaches .slice().
  function codePointToUtf16(text, index) {
    var codePoints = 0;
    var units = 0;
    for (var i = 0; i < text.length; i++) {
      if (codePoints === index) {
        return units;
      }
      var unit = text.charCodeAt(i);
      if (unit >= 0xd800 && unit <= 0xdbff && i + 1 < text.length) {
        var next = text.charCodeAt(i + 1);
        if (next >= 0xdc00 && next <= 0xdfff) {
          i++;
        }
      }
      codePoints++;
      units = i + 1;
    }
    return codePoints === index ? units : -1;
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

  // The name of a conversation, from its first message: the same rule as the server's
  // (persistence.conversations.conversation_title), which names every conversation the list
  // brings; this is for a conversation made in this page, until the list is read again. The words
  // on one line (every run of whitespace is one space), cut at 60 characters (code points) with
  // an ellipsis when longer. tests/test_web_ui_logic.py compares it with the server's over a
  // fixed set of texts.
  var TITLE_LENGTH = 60;
  var SPACE_RUN = new RegExp("[" + SPACE_CLASS + "]+");

  function deriveTitle(text) {
    if (typeof text !== "string") {
      return null;
    }
    var line = text.split(SPACE_RUN).filter(function (word) {
      return word !== "";
    }).join(" ");
    if (line === "") {
      return null;
    }
    var points = Array.from(line);
    if (points.length <= TITLE_LENGTH) {
      return line;
    }
    return points.slice(0, TITLE_LENGTH).join("").replace(/ +$/, "") + "…";
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

  // The display name's checks, as the server makes them (persistence.profile): trimmed,
  // not empty, at most 200 code points, and no Unicode category C character. The server
  // stays the authority; this only saves a round trip and words the refusal at once.
  function prepareProfileName(raw) {
    var text = trimLikeServer(typeof raw === "string" ? raw : "");
    var count = countCodePoints(text);
    if (!text) {
      return { ok: false, reason: "empty", message: TEXT.profileNameEmpty, count: count };
    }
    if (count > MAX_PROFILE_NAME) {
      return { ok: false, reason: "too_long", message: TEXT.profileNameTooLong, count: count };
    }
    if (PROFILE_CONTROL.test(text)) {
      return { ok: false, reason: "characters", message: TEXT.profileNameBadChars, count: count };
    }
    return { ok: true, text: text, count: count };
  }

  // --- household measures: the form's values and how a fraction is called -----------------------

  function greatestCommonDivisor(left, right) {
    var a = Math.abs(left);
    var b = Math.abs(right);
    while (b) {
      var next = a % b;
      a = b;
      b = next;
    }
    return a;
  }

  function reducedFraction(numerator, denominator) {
    if (!Number.isSafeInteger(numerator) || !Number.isSafeInteger(denominator)) {
      return null;
    }
    if (numerator < 0 || denominator < 1) {
      return null;
    }
    var divisor = greatestCommonDivisor(numerator, denominator) || 1;
    return { numerator: numerator / divisor, denominator: denominator / divisor };
  }

  // The common kitchen fractions, as the glyphs Russian recipe text uses. A denominator
  // that is not here and terminates within two decimal places is written as a decimal
  // with a comma (4/5 -> "0,8"); anything else stays a plain "num/den" fraction, so an
  // exact value is never silently replaced by a rounded decimal.
  var VULGAR_FRACTIONS = {
    2: { 1: "½" },
    3: { 1: "⅓", 2: "⅔" },
    4: { 1: "¼", 2: "½", 3: "¾" },
    6: { 1: "⅙", 5: "⅚" },
    8: { 1: "⅛", 3: "⅜", 5: "⅝", 7: "⅞" },
  };

  function formatFraction(numerator, denominator) {
    var value = reducedFraction(numerator, denominator);
    if (value === null) {
      return null;
    }
    var n = value.numerator;
    var d = value.denominator;
    if (d === 1) {
      return String(n);
    }
    var whole = Math.floor(n / d);
    var rest = n % d;
    var vulgar = VULGAR_FRACTIONS[d] && VULGAR_FRACTIONS[d][rest];
    if (vulgar) {
      return (whole ? String(whole) : "") + vulgar;
    }
    var factor = MEASURE_HUNDREDTHS_FACTOR[d];
    if (factor) {
      var hundredths = n * factor;
      var integer = Math.floor(hundredths / 100);
      var tail = hundredths % 100;
      if (tail === 0) {
        return String(integer);
      }
      var digits = (tail < 10 ? "0" : "") + String(tail);
      return integer + "," + digits.replace(/0+$/, "");
    }
    return n + "/" + d;
  }

  // Which Russian form of the unit a count takes: 1 -> "ваш стакан", 2..4 -> "ваших
  // стакана", 0 and 5+ -> "ваших стаканов" (11..14 included), and a fraction always takes
  // the genitive singular ("0,8 вашего стакана").
  function measureGrammar(numerator, denominator) {
    var value = reducedFraction(numerator, denominator);
    if (value === null) {
      return null;
    }
    if (value.denominator !== 1) {
      return "fraction";
    }
    var count = value.numerator;
    var lastTwo = count % 100;
    if (lastTwo >= 11 && lastTwo <= 14) {
      return "many";
    }
    var last = count % 10;
    if (last === 1) {
      return "one";
    }
    if (last >= 2 && last <= 4) {
      return "few";
    }
    return "many";
  }

  var MEASURE_UNIT_TEXT = {
    glass: function () { return TEXT.measureUnitGlass; },
    teaspoon: function () { return TEXT.measureUnitTeaspoon; },
    tablespoon: function () { return TEXT.measureUnitTablespoon; },
  };

  function formatMeasure(numerator, denominator, unit) {
    var amount = formatFraction(numerator, denominator);
    var grammar = measureGrammar(numerator, denominator);
    var forms = MEASURE_UNIT_TEXT[unit] ? MEASURE_UNIT_TEXT[unit]() : null;
    if (amount === null || grammar === null || !forms) {
      return null;
    }
    return amount + " " + forms[grammar];
  }

  // KH-F01-B2: a mass estimate's practical value ("≈ 1 стакан", "≈ 3½ столовой ложки").
  // The unit is the one the engine selected; the grammar is the same Russian rule the
  // released B1.1 hint uses, with the plain unit name instead of "your measure".
  var MASS_MEASURE_UNIT_TEXT = {
    glass: function () { return TEXT.massUnitGlass; },
    teaspoon: function () { return TEXT.massUnitTeaspoon; },
    tablespoon: function () { return TEXT.massUnitTablespoon; },
  };

  function formatMassMeasure(numerator, denominator, unit) {
    var amount = formatFraction(numerator, denominator);
    var grammar = measureGrammar(numerator, denominator);
    var forms = MASS_MEASURE_UNIT_TEXT[unit] ? MASS_MEASURE_UNIT_TEXT[unit]() : null;
    if (amount === null || grammar === null || !forms) {
      return null;
    }
    return amount + " " + forms[grammar];
  }

  // One measure as the form holds it: empty means "the system default" (null), anything
  // else must be a whole number of millilitres within the approved range. Nothing is
  // clamped or rounded; the server checks the same rule again and is the authority.
  function prepareMeasure(name, raw) {
    var limit = MEASURE_LIMITS[name];
    if (!limit) {
      return { ok: false, message: TEXT.profileMeasureInvalid };
    }
    var message = fill(TEXT.profileMeasureInvalid, { min: limit.min, max: limit.max });
    var text = trimLikeServer(typeof raw === "string" ? raw : "");
    if (text === "") {
      return { ok: true, value: null };
    }
    if (!/^[0-9]+$/.test(text)) {
      return { ok: false, message: message };
    }
    var number = Number(text);
    if (!Number.isSafeInteger(number) || number < limit.min || number > limit.max) {
      return { ok: false, message: message };
    }
    return { ok: true, value: number };
  }

  function prepareMeasures(values) {
    var prepared = {};
    for (var index = 0; index < MEASURE_KEYS.length; index++) {
      var name = MEASURE_KEYS[index];
      var result = prepareMeasure(name, values ? values[name] : "");
      if (!result.ok) {
        return { ok: false, field: name, message: result.message };
      }
      prepared[name] = result.value;
    }
    return { ok: true, values: prepared };
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

  // Membership in a closed set of string members. The type check comes first: an object or
  // an array must never be coerced into a key (a plain object with no usable toString
  // throws, and `["temporary_clarification"]` would otherwise read as its joined text).
  function isEnum(value, members) {
    return typeof value === "string" && members.has(value);
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
  // The one 5xx that is an answer is the application's own {"detail": "login_unavailable"} (a 503):
  // it says no way of signing in is connected on this deployment, which is how it was set up, not
  // a failure ("setup"). It names nobody and lets nobody in; any other 503, or the same words
  // from anything but the application's JSON, stay failures.
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
    if (response.status === 503 && readAuthCode(response) === "login_unavailable") {
      // (text: what a page that is already signed in says, should this ever be the answer to its question)
      return { type: "setup", text: TEXT.unavailable, correlationId: null };
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

  // The reads of the chat API (the list, the history) and of the favorites. `missing` is the code of the
  // application's 404 that means "that one is not there" ("conversation_not_found" unless said otherwise).
  function classifyRead(response, missing) {
    if (response.kind === "network") {
      return { type: "unavailable", text: unavailableText(response, null), correlationId: null };
    }
    if (response.status === 401) {
      return { type: "auth" };
    }
    var error = readApiError(response);
    if (response.status === 404 && error && error.code === (missing || "conversation_not_found")) {
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

  // --- the profile: the account's settings and the ways it has signed in --------------------------------

  // One sign-in method as the server names it. The address is the person's own email
  // address (the server sends it for an email identity and for no other); the Telegram
  // subject is deliberately not part of the answer, so there is nothing here to hide.
  function normalizeLoginMethod(item) {
    if (
      !isObject(item) ||
      (item.provider !== "telegram" && item.provider !== "email") ||
      (item.status !== "active" && item.status !== "revoked")
    ) {
      return null;
    }
    if (item.address !== null && item.address !== undefined && typeof item.address !== "string") {
      return null;
    }
    return {
      provider: item.provider,
      address: item.provider === "email" && typeof item.address === "string" ? item.address : null,
      status: item.status,
    };
  }

  // One of the three measure settings as the profile read carries it: the stored
  // override (or null), its effective value, and the approved bounds. The bounds and the
  // default must be the ones this page knows, and the effective value must follow from
  // the override; anything else is an answer this page cannot use.
  function normalizeMeasureSetting(item, name) {
    var limit = MEASURE_LIMITS[name];
    if (!isObject(item)) {
      return null;
    }
    var override = item.override;
    if (override !== null && !Number.isSafeInteger(override)) {
      return null;
    }
    if (
      !Number.isSafeInteger(item.effective) ||
      !Number.isSafeInteger(item.default) ||
      !Number.isSafeInteger(item.min) ||
      !Number.isSafeInteger(item.max)
    ) {
      return null;
    }
    if (item.default !== limit.def || item.min !== limit.min || item.max !== limit.max) {
      return null;
    }
    if (override !== null && (override < limit.min || override > limit.max)) {
      return null;
    }
    var effective = override === null ? item.default : override;
    if (item.effective !== effective) {
      return null;
    }
    return {
      override: override,
      effective: item.effective,
      defaultMl: item.default,
      min: item.min,
      max: item.max,
    };
  }

  function normalizeProfileMeasures(value) {
    if (!isObject(value)) {
      return null;
    }
    var measures = {};
    for (var index = 0; index < MEASURE_KEYS.length; index++) {
      var name = MEASURE_KEYS[index];
      var setting = normalizeMeasureSetting(value[name], name);
      if (setting === null) {
        return null;
      }
      measures[name] = setting;
    }
    return measures;
  }

  function normalizeProfile(body) {
    if (!isObject(body)) {
      return null;
    }
    var name = body.display_name === undefined ? null : body.display_name;
    if (name !== null && (typeof name !== "string" || name.length > 4096)) {
      return null;
    }
    if (!Object.prototype.hasOwnProperty.call(TEXT_SIZE_VALUES, body.text_size)) {
      return null;
    }
    var measures = normalizeProfileMeasures(body.measures);
    if (measures === null) {
      return null;
    }
    if (!Array.isArray(body.login_methods)) {
      return null;
    }
    var methods = [];
    for (var i = 0; i < body.login_methods.length; i++) {
      var method = normalizeLoginMethod(body.login_methods[i]);
      if (method === null) {
        return null;
      }
      methods.push(method);
    }
    return {
      displayName: name,
      textSize: body.text_size,
      measures: measures,
      loginMethods: methods,
    };
  }

  // --- favorites: what the server says about the recipes a person kept ---------------------------------

  // {id, title, message_id, created_at, kind, recipe_index, version} ->
  // {id, title, messageId, createdAt, kind, recipeIndex, version}.
  // A body from an older server (no kind) is a text favorite, as it always was.
  function normalizeFavorite(item) {
    if (
      !isObject(item) ||
      typeof item.id !== "string" ||
      !UUID_PATTERN.test(item.id) ||
      typeof item.title !== "string" ||
      item.title === "" ||
      !isCount(item.message_id) ||
      item.message_id < 1
    ) {
      return null;
    }
    var kind = item.kind === undefined ? "text" : item.kind;
    if (kind !== "text" && kind !== "structured") {
      return null;
    }
    var recipeIndex = -1;
    if (kind === "structured") {
      if (!isCount(item.recipe_index) || item.recipe_index < 0 || item.recipe_index >= MAX_RECIPES_PER_MESSAGE) {
        return null;
      }
      recipeIndex = item.recipe_index;
    }
    var version = isCount(item.version) && item.version >= 1 ? item.version : 1;
    return {
      id: item.id.toLowerCase(),
      title: item.title,
      messageId: item.message_id,
      createdAt: parseTimestamp(item.created_at),
      kind: kind,
      recipeIndex: recipeIndex,
      version: version,
    };
  }

  function normalizeFavoriteList(body) {
    if (!isObject(body) || !Array.isArray(body.favorites)) {
      return null;
    }
    var items = [];
    for (var i = 0; i < body.favorites.length; i++) {
      var item = normalizeFavorite(body.favorites[i]);
      if (item === null) {
        return null;
      }
      items.push(item);
    }
    return items;
  }

  function normalizeIngredients(value) {
    if (!Array.isArray(value) || value.length < 1 || value.length > MAX_EDIT_INGREDIENTS) {
      return null;
    }
    var items = [];
    for (var i = 0; i < value.length; i++) {
      var item = value[i];
      if (
        !isObject(item) ||
        typeof item.name !== "string" ||
        item.name === "" ||
        typeof item.amount !== "string" ||
        item.amount === ""
      ) {
        return null;
      }
      items.push({ name: item.name, amount: item.amount });
    }
    return items;
  }

  function normalizeSteps(value) {
    if (!Array.isArray(value) || value.length < 1 || value.length > MAX_EDIT_STEPS) {
      return null;
    }
    var items = [];
    for (var i = 0; i < value.length; i++) {
      if (typeof value[i] !== "string" || value[i] === "") {
        return null;
      }
      items.push(value[i]);
    }
    return items;
  }

  // --- the favorite's household-measure block (KH-F01-B1) --------------------------------------

  var CONVERSION_STATUSES = new Set([
    "exact",
    "nominal_household",
    "clarification_required",
    "unsupported",
    "ambiguous",
    "unresolved",
  ]);
  var CONVERSION_TARGET_UNITS = new Set(["glass", "teaspoon", "tablespoon"]);
  var CONVERSION_PRESENTATIONS = new Set(["rounded", "exact", "none"]);
  // The closed reason set of 15.8.2. It drives no drawing of its own, but a value outside
  // it is not one the page understands.
  var CONVERSION_REASONS = new Set([
    "converted",
    "source_not_quantified",
    "glass_size_required",
    "glass_size_invalid",
    "glass_size_conflict",
    "mass_volume_coefficient_required",
  ]);
  // Only a genuinely converted amount may be drawn as a number: every other status says
  // why the amount could not be converted, and a numeric presentation beside one of them
  // contradicts the status. A block that does so is not used at all (nothing is repaired).
  var CONVERSION_SUCCEEDED = new Set(["exact", "nominal_household"]);

  // An exact rational as the server writes it: two integers, never a float. Anything
  // outside the safe-integer range is refused rather than rounded by the browser.
  function normalizeRational(value) {
    if (!isObject(value)) {
      return null;
    }
    if (!Number.isSafeInteger(value.numerator) || !Number.isSafeInteger(value.denominator)) {
      return null;
    }
    if (value.numerator < 0 || value.denominator < 1) {
      return null;
    }
    return { numerator: value.numerator, denominator: value.denominator };
  }

  // The typed conversion of an ingredient amount or of a step span (15.8.2), as the page
  // reads it. The released B1 encoding is unchanged; a value that contradicts its own status
  // is refused rather than repaired.
  function normalizeConversionValues(item) {
    if (!isObject(item)) {
      return null;
    }
    if (!isEnum(item.target_unit, CONVERSION_TARGET_UNITS)) {
      return null;
    }
    if (!isEnum(item.status, CONVERSION_STATUSES)) {
      return null;
    }
    if (!isEnum(item.reason, CONVERSION_REASONS)) {
      return null;
    }
    if (typeof item.identical !== "boolean") {
      return null;
    }
    if (!isEnum(item.presentation, CONVERSION_PRESENTATIONS)) {
      return null;
    }
    var exact = null;
    if (item.exact !== null && item.exact !== undefined) {
      exact = normalizeRational(item.exact);
      if (exact === null) {
        return null;
      }
    }
    var rounded = null;
    if (item.rounded !== null && item.rounded !== undefined) {
      var amount = normalizeRational(item.rounded);
      var error = normalizeRational(item.rounded.relative_error);
      if (amount === null || error === null) {
        return null;
      }
      rounded = {
        numerator: amount.numerator,
        denominator: amount.denominator,
        relativeError: error,
      };
    }
    if (item.presentation === "rounded" && rounded === null) {
      return null;
    }
    if (item.presentation === "exact" && exact === null) {
      return null;
    }
    if (!CONVERSION_SUCCEEDED.has(item.status)) {
      // A failed, unsupported, ambiguous or clarification-required amount carries no
      // converted value: an exact or rounded presentation (or a value to fall back on)
      // beside it is a contradiction, and no part of it may reach the page.
      if (item.presentation !== "none" || item.identical !== false) {
        return null;
      }
      if (exact !== null || rounded !== null) {
        return null;
      }
    }
    return {
      targetUnit: item.target_unit,
      status: item.status,
      reason: item.reason,
      exact: exact,
      rounded: rounded,
      identical: item.identical,
      presentation: item.presentation,
    };
  }

  function normalizeIngredientConversion(item, ingredientCount) {
    if (!isObject(item) || !isCount(item.position) || item.position >= ingredientCount) {
      return null;
    }
    if (typeof item.source !== "string" || item.source === "") {
      return null;
    }
    if (item.source_kind !== "household" && item.source_kind !== "metric_volume") {
      return null;
    }
    var values = normalizeConversionValues(item);
    if (values === null) {
      return null;
    }
    return {
      position: item.position,
      source: item.source,
      sourceKind: item.source_kind,
      targetUnit: values.targetUnit,
      status: values.status,
      reason: values.reason,
      exact: values.exact,
      rounded: values.rounded,
      identical: values.identical,
      presentation: values.presentation,
    };
  }

  // Whether an ingredient conversion may rest on a capacity that only the temporary
  // clarification supplied. Fail closed on purpose: absent, null, empty or structurally
  // invalid source terms never prove independence, so the conversion is treated as
  // possibly dependent (OD-03 has no inheritance, and nothing is drawn from a dependency
  // nobody verified). A legacy B1 block carries no source terms at all, so its conversions
  // take the same conservative path once the clarification has been dropped.
  function conversionNeedsClarification(item) {
    var terms = item.source_terms;
    if (!Array.isArray(terms) || terms.length === 0) {
      return true;
    }
    var dependent = false;
    for (var index = 0; index < terms.length; index++) {
      var term = terms[index];
      if (!isObject(term)) {
        return true;
      }
      if (!isEnum(term.unit, SOURCE_TERM_UNITS)) {
        return true;
      }
      if (normalizeRational(term.amount) === null) {
        return true;
      }
      if (!isEnum(term.capacity_origin, CAPACITY_ORIGINS)) {
        return true;
      }
      // The per-term shapes of 15.8.2: a glass term names the capacity it was resolved
      // with (or has none because it is unresolved); a spoon or metric term has
      // `not_applicable` and no capacity. Anything else establishes nothing.
      if (term.unit === "glass") {
        if (term.capacity_origin === "not_applicable") {
          return true;
        }
        if (term.capacity_origin === "unresolved") {
          if (term.capacity_ml !== null && term.capacity_ml !== undefined) {
            return true;
          }
        } else if (normalizeRational(term.capacity_ml) === null) {
          return true;
        }
      } else {
        if (term.capacity_origin !== "not_applicable") {
          return true;
        }
        if (term.capacity_ml !== null && term.capacity_ml !== undefined) {
          return true;
        }
      }
      if (term.capacity_origin === "temporary_clarification") {
        dependent = true;
      }
    }
    return dependent;
  }

  // Whether a successful conversion claims a number beside a glass term whose capacity was
  // never established. The typed contract of 15.8.2 allows `unresolved` only where the
  // capacity is still required, and Stage C's own conversion then reports
  // `clarification_required` (MeasureVolumes never fills a source capacity); a success
  // beside such a term is therefore contradictory, and the number is refused during
  // normalization instead of being shown and withdrawn later. The origin alone decides:
  // `unresolved` never establishes a capacity, so no `capacity_ml` beside it -- null,
  // absent, a valid rational, zero, or any other shape -- makes the success compatible.
  // A failed conversion needs no such rejection: it draws no number at all. Unreadable
  // source terms are not decided here: they take the conservative clarification-dependent
  // path above.
  function conversionContradictsSourceTerms(item) {
    if (!CONVERSION_SUCCEEDED.has(item.status)) {
      return false;
    }
    var terms = item.source_terms;
    if (!Array.isArray(terms)) {
      return false;
    }
    for (var index = 0; index < terms.length; index++) {
      var term = terms[index];
      if (isObject(term) && term.unit === "glass" && term.capacity_origin === "unresolved") {
        return true;
      }
    }
    return false;
  }

  // --- KH-F01-B1.1: the step annotations of a `schema_version: 2` block ------------------------

  var LINK_STATUSES = new Set(["linked", "ambiguous", "shared", "unresolved", "unsupported"]);
  var LINK_REASONS = new Set([
    "name_after",
    "name_before_separator",
    "name_before_copula",
    "shared_amount",
    "multiple_candidates",
    "anaphora",
    "no_local_binding",
    "no_mention",
    "range",
    "us_cup",
    "package",
    "countable",
    "heaped",
    "unsupported_construct",
    "malformed_expression",
  ]);
  // The reasons the scanner's explicit binding rules give a `linked` span. Any other
  // reason beside `linked` is a combination the server never produces.
  var LINKED_REASONS = new Set([
    "name_after",
    "name_before_separator",
    "name_before_copula",
  ]);
  var CAPACITY_ORIGINS = new Set([
    "declared_text",
    "temporary_clarification",
    "not_applicable",
    "unresolved",
  ]);
  var OMITTED_CONVERSION_REASONS = new Set(["mass", "unsafe_fraction", "conversion_failed"]);
  var APPROXIMATION_WORDS = new Set(["около", "примерно", "приблизительно"]);
  var STEP_STATUSES = new Set(["complete", "incomplete", "failed"]);
  var ANALYSIS_STATUSES = new Set(["complete", "incomplete", "failed"]);
  var PROVENANCE_STATUSES = new Set(["complete", "incomplete", "failed"]);
  // The source-term units of the typed contract: the units a per-term capacity can say
  // anything about.
  var SOURCE_TERM_UNITS = new Set(["glass", "teaspoon", "tablespoon", "milliliter", "liter"]);
  var STEP_TEXT_HASH = /^[0-9a-f]{64}$/;

  // --- KH-F01-B2: the independent `measures.mass_conversions` section --------------------------
  //
  // Its own `schema_version: 1`, its own closed vocabularies. The client draws a number only
  // for a fully consistent successful item; a refusal, a contradiction or a value it cannot
  // read exactly costs that item alone, never the released B1.1 block.

  var MASS_SECTION_STATUSES = new Set(["complete", "incomplete", "failed"]);
  var MASS_CONVERSION_STATUSES = new Set([
    "reference_approximate",
    "clarification_required",
    "ambiguous",
    "unsupported",
  ]);
  var MASS_CONVERSION_REASONS = new Set([
    "converted",
    "size_class_required",
    "generic_name",
    "ingredient_ambiguous",
    "qualifier_conflict",
    "reference_variant_conflict",
    "ingredient_not_recognized",
    "unsupported_qualifier",
    "state_mismatch",
    "zero_mass",
    "reference_missing",
    "reference_not_approved",
    "rounding_gate_failed",
    "interval_too_wide",
    "below_useful_count",
  ]);
  // Every closed reason has exactly one status family; any other pair is contradictory.
  var MASS_STATUS_BY_REASON = {
    converted: "reference_approximate",
    size_class_required: "clarification_required",
    generic_name: "ambiguous",
    ingredient_ambiguous: "ambiguous",
    qualifier_conflict: "ambiguous",
    reference_variant_conflict: "ambiguous",
    unsupported_qualifier: "unsupported",
    ingredient_not_recognized: "unsupported",
    state_mismatch: "unsupported",
    zero_mass: "unsupported",
    reference_missing: "unsupported",
    reference_not_approved: "unsupported",
    rounding_gate_failed: "unsupported",
    interval_too_wide: "unsupported",
    below_useful_count: "unsupported",
  };
  var MASS_MATCH_STATUSES = new Set([
    "matched",
    "generic",
    "ambiguous",
    "qualifier_conflict",
    "unsupported_qualifier",
    "unrecognized",
  ]);
  // The match state a matcher refusal must carry, as the accepted B2-2 payload guarantees.
  var MASS_MATCH_BY_REFUSAL_REASON = {
    generic_name: "generic",
    ingredient_ambiguous: "ambiguous",
    qualifier_conflict: "qualifier_conflict",
    unsupported_qualifier: "unsupported_qualifier",
    ingredient_not_recognized: "unrecognized",
  };
  var MASS_BASES = new Set(["edible_portion", "as_purchased", "not_applicable"]);
  var MASS_REFERENCE_KINDS = new Set(["point", "measured_interval", "calibration_class"]);
  var MASS_DISPLAYS = new Set(["single", "interval"]);
  var MASS_VOLUME_UNITS = new Set(["glass", "teaspoon", "tablespoon"]);

  // An exact comparison of two normalized non-negative rationals, using only integer
  // arithmetic. Cross multiplication could exceed the safe-integer range, and floating
  // division could round a value just below a whole number up to it, so the comparison
  // is the Euclidean one: integer parts first, then the reciprocal remainders -- which
  // compare in the opposite order -- without ever multiplying two numerators together.
  function compareWholeParts(numerator, denominator) {
    return (numerator - (numerator % denominator)) / denominator;
  }

  function compareRationals(left, right) {
    var wholeLeft = compareWholeParts(left.numerator, left.denominator);
    var wholeRight = compareWholeParts(right.numerator, right.denominator);
    if (wholeLeft !== wholeRight) {
      return wholeLeft < wholeRight ? -1 : 1;
    }
    var remainderLeft = left.numerator % left.denominator;
    var remainderRight = right.numerator % right.denominator;
    if (remainderLeft === 0 || remainderRight === 0) {
      if (remainderLeft === remainderRight) {
        return 0;
      }
      return remainderLeft === 0 ? -1 : 1;
    }
    // With equal whole parts, left - right has the sign of (a mod b)*d - (c mod d)*b.
    // Comparing d/(c mod d) against b/(a mod b) has that very sign, but its cross product
    // is the same expression -- so no numerator is ever multiplied by another large one.
    // The recursive result is therefore already the sign of left - right, and is returned
    // as it is (a negation here would invert every genuine ordering such as 3/2 < 7/4).
    return compareRationals(
      { numerator: right.denominator, denominator: remainderRight },
      { numerator: left.denominator, denominator: remainderLeft }
    );
  }

  function normalizeMassAssumption(value) {
    if (!isObject(value) || value.kind !== "size_class") {
      return null;
    }
    if (typeof value.size_class !== "string" || value.size_class === "") {
      return null;
    }
    var weight = normalizeRational(value.weight_g);
    if (weight === null || weight.numerator <= 0) {
      return null;
    }
    if (!isEnum(value.mass_basis, MASS_BASES)) {
      return null;
    }
    var label = value.label_ru;
    if (typeof label !== "string" || label.trim() === "") {
      return null;
    }
    // The server's own rule: the label is Russian, so it names the assumption in words the
    // person reads, never an internal class identifier.
    var russian = false;
    for (var index = 0; index < label.length; index++) {
      var code = label.charCodeAt(index);
      if (code >= 0x0400 && code <= 0x04ff) {
        russian = true;
        break;
      }
    }
    if (!russian) {
      return null;
    }
    return { sizeClass: value.size_class, label: label };
  }

  // A mass-to-volume result. Only the practical rounded value is drawn: the engine always
  // fills it for a successful conversion, and the page never invents a rounded value of
  // its own. The exact rational is still validated, so a contradictory payload is dropped.
  function normalizeMassVolume(conversion) {
    if (!isEnum(conversion.target_unit, MASS_VOLUME_UNITS)) {
      return null;
    }
    var exact = normalizeRational(conversion.exact);
    if (exact === null || exact.numerator <= 0) {
      return null;
    }
    var rounded = normalizeRational(conversion.rounded);
    if (rounded === null || rounded.numerator <= 0) {
      return null;
    }
    var error = normalizeRational(conversion.rounded.relative_error);
    if (error === null) {
      return null;
    }
    return {
      kind: "volume",
      targetUnit: conversion.target_unit,
      numerator: rounded.numerator,
      denominator: rounded.denominator,
    };
  }

  // A mass-to-count result. The rounded bounds are what a cook reads; `display` says
  // whether they are one number or a genuine interval, and the two must agree exactly.
  function normalizeMassCount(conversion, conditional) {
    if (!isEnum(conversion.display, MASS_DISPLAYS)) {
      return null;
    }
    var piece = conversion.piece;
    if (!isObject(piece) || !isEnum(piece.reference_kind, MASS_REFERENCE_KINDS)) {
      return null;
    }
    if (
      piece.size_class !== null &&
      piece.size_class !== undefined &&
      (typeof piece.size_class !== "string" || piece.size_class === "")
    ) {
      return null;
    }
    var low = normalizeRational(conversion.count_low);
    var high = normalizeRational(conversion.count_high);
    var roundedLow = normalizeRational(conversion.rounded_low);
    var roundedHigh = normalizeRational(conversion.rounded_high);
    var width = normalizeRational(conversion.relative_width);
    if (low === null || high === null || roundedLow === null || roundedHigh === null || width === null) {
      return null;
    }
    if (
      low.numerator <= 0 ||
      high.numerator <= 0 ||
      roundedLow.numerator <= 0 ||
      roundedHigh.numerator <= 0
    ) {
      return null;
    }
    var exactOrder = compareRationals(low, high);
    var roundedOrder = compareRationals(roundedLow, roundedHigh);
    if (exactOrder > 0 || roundedOrder > 0) {
      return null;
    }
    // A zero relative width is exactly a single exact count, and the display follows the
    // rounded bounds: one number exactly when they rounded to the same value.
    if ((exactOrder === 0) !== (width.numerator === 0)) {
      return null;
    }
    var single = conversion.display === "single";
    if (single !== (roundedOrder === 0)) {
      return null;
    }
    if (piece.reference_kind === "point") {
      // A point reference is one weight: it can never present as an interval.
      if (exactOrder !== 0 || roundedOrder !== 0 || width.numerator !== 0 || !single) {
        return null;
      }
    }
    if (piece.reference_kind === "calibration_class" && !piece.size_class) {
      // A supplier size grade without its class names nothing the assumption could match.
      return null;
    }
    return {
      kind: "count",
      display: conversion.display,
      low: roundedLow,
      high: roundedHigh,
      conditional: conditional,
      assumptionLabel: null,
    };
  }

  // One B2 item as the page may draw it, or null when it is a refusal or cannot be
  // trusted. Only a successful, fully consistent item becomes a hint; a refusal is never
  // turned into a number, and nothing is repaired.
  //
  // The item must also be about the ingredient the card actually draws at that position:
  // its `name` and `source` are compared with the stored ingredient's original `name` and
  // `amount` byte for byte. A position alone is not evidence -- an item may name another
  // ingredient or another amount -- so a mismatch costs this item only, never its valid
  // siblings and never the released B1.1 block. The originals are not trimmed, normalized
  // or fuzzy-matched: the server writes the item from these very strings.
  function normalizeMassItem(item, ingredients) {
    if (!isObject(item) || !isCount(item.position) || item.position >= ingredients.length) {
      return null;
    }
    var stored = ingredients[item.position];
    if (!isObject(stored) || typeof stored.name !== "string" || typeof stored.amount !== "string") {
      return null;
    }
    if (typeof item.name !== "string" || item.name === "") {
      return null;
    }
    if (typeof item.source !== "string" || item.source === "") {
      return null;
    }
    if (item.name !== stored.name || item.source !== stored.amount) {
      return null;
    }
    // A successful conversion needs a real source mass. Zero is refused by the accepted
    // B2-2 engine (`zero_mass`), so a "converted" item claiming it is contradictory: no
    // number may be drawn from it, and the amount is never rewritten here.
    var mass = normalizeRational(item.mass_g);
    if (mass === null || mass.numerator <= 0) {
      return null;
    }
    var ingredient = item.ingredient;
    if (!isObject(ingredient) || !isEnum(ingredient.match, MASS_MATCH_STATUSES)) {
      return null;
    }
    if (!isEnum(item.status, MASS_CONVERSION_STATUSES)) {
      return null;
    }
    if (!isEnum(item.reason, MASS_CONVERSION_REASONS)) {
      return null;
    }
    if (MASS_STATUS_BY_REASON[item.reason] !== item.status) {
      return null;
    }
    if (typeof item.conditional !== "boolean") {
      return null;
    }
    var assumption = item.assumption;
    var hasAssumption = assumption !== null && assumption !== undefined;
    if (item.conditional !== hasAssumption) {
      return null;
    }
    var converted = item.reason === "converted";
    var conversion = item.conversion;
    var hasConversion = conversion !== null && conversion !== undefined;
    if (converted !== hasConversion) {
      return null;
    }
    if (!converted) {
      // A refusal is never presented. It is still checked for the contradiction the
      // accepted contract forbids, so a refusal that claims a match state it cannot have
      // is dropped like any other malformed item.
      var expectedMatch = MASS_MATCH_BY_REFUSAL_REASON[item.reason];
      if (expectedMatch !== undefined && ingredient.match !== expectedMatch) {
        return null;
      }
      return null;
    }
    // A number is only believable for an ingredient the matcher actually recognized.
    if (
      ingredient.match !== "matched" ||
      typeof ingredient.id !== "string" ||
      ingredient.id === "" ||
      typeof ingredient.display_name !== "string" ||
      ingredient.display_name === ""
    ) {
      return null;
    }
    if (!isObject(conversion)) {
      return null;
    }
    var normalizedAssumption = null;
    if (item.conditional) {
      normalizedAssumption = normalizeMassAssumption(assumption);
      if (normalizedAssumption === null) {
        return null;
      }
    }
    var normalized;
    if (conversion.kind === "volume") {
      // A conditional estimate is a count by definition, never a volume.
      if (normalizedAssumption !== null) {
        return null;
      }
      normalized = normalizeMassVolume(conversion);
    } else if (conversion.kind === "count") {
      normalized = normalizeMassCount(conversion, item.conditional);
      if (normalized !== null && normalizedAssumption !== null) {
        if (conversion.piece.size_class !== normalizedAssumption.sizeClass) {
          return null;
        }
        normalized.assumptionLabel = normalizedAssumption.label;
      }
    } else {
      return null;
    }
    if (normalized === null) {
      return null;
    }
    normalized.position = item.position;
    normalized.name = item.name;
    return normalized;
  }

  // The `mass_conversions` extension as the hints the page may draw. Anything the page
  // cannot use -- an older schema, a failed section, a malformed item -- costs only what
  // it describes; the caller's B1.1 block is never touched by this function.
  //
  // The section inherits the outer block's `favorite_version`, because the server builds it
  // for that very detail. An answer bound to another version, or one with no usable version
  // at all, is conservatively refused here (B2 only): it may describe ingredients or amounts
  // the card no longer shows, and a stale hint is worse than none. The released B1.1 fields
  // stay drawn exactly as before either way.
  function normalizeMassConversions(value, ingredients, sameVersion) {
    if (!isObject(value) || value.schema_version !== 1) {
      return [];
    }
    if (!isEnum(value.status, MASS_SECTION_STATUSES)) {
      return [];
    }
    // A failed section promises nothing: even if items are present beside it, none is
    // drawn. An incomplete one keeps exactly its valid successful items.
    if (value.status === "failed") {
      return [];
    }
    if (sameVersion !== true || !Array.isArray(value.items)) {
      return [];
    }
    var counts = Object.create(null);
    for (var raw = 0; raw < value.items.length; raw++) {
      var candidate = value.items[raw];
      if (isObject(candidate) && isCount(candidate.position) && candidate.position < ingredients.length) {
        counts[candidate.position] = (counts[candidate.position] || 0) + 1;
      }
    }
    var found = [];
    for (var index = 0; index < value.items.length; index++) {
      var normalized = normalizeMassItem(value.items[index], ingredients);
      if (normalized === null || counts[normalized.position] > 1) {
        continue;
      }
      found.push(normalized);
    }
    found.sort(function (left, right) {
      return left.position - right.position;
    });
    return found;
  }

  // One span, validated against the step it claims to annotate. Every check of 15.8.4 that
  // can be made synchronously is made here: the offsets are code points into the exact text
  // the page is about to draw, the mapped slice equals the span's own text, and every enum,
  // rational and optional field is a value the page understands. The cross-field rules of
  // 15.8.2 are checked too: a number belongs only to a span that was really linked to an
  // ingredient of this recipe and really converted, and no contradictory combination is
  // repaired. Anything less drops the span, and its step is drawn as the stored text only.
  function normalizeSpanAnnotation(item, stepText, ingredientCount) {
    if (!isObject(item)) {
      return null;
    }
    if (!Number.isSafeInteger(item.start) || !Number.isSafeInteger(item.end)) {
      return null;
    }
    if (item.start < 0 || item.end <= item.start) {
      return null;
    }
    if (typeof item.text !== "string" || item.text === "") {
      return null;
    }
    if (!isEnum(item.link, LINK_STATUSES) || !isEnum(item.link_reason, LINK_REASONS)) {
      return null;
    }
    if (!isEnum(item.capacity_origin, CAPACITY_ORIGINS)) {
      return null;
    }
    var position = item.ingredient_position === undefined ? null : item.ingredient_position;
    if (position !== null && !isCount(position)) {
      return null;
    }
    var conversion = null;
    if (item.conversion !== null && item.conversion !== undefined) {
      conversion = normalizeConversionValues(item.conversion);
      if (conversion === null) {
        return null;
      }
    }
    var omitted =
      item.conversion_omitted_reason === undefined ? null : item.conversion_omitted_reason;
    if (omitted !== null && !isEnum(omitted, OMITTED_CONVERSION_REASONS)) {
      return null;
    }
    if (item.link === "linked") {
      // A linked amount names an ingredient this recipe actually has, by a reason the
      // binding rules produce, and has either a conversion or the reason it has none.
      if (!LINKED_REASONS.has(item.link_reason)) {
        return null;
      }
      if (position === null || position >= ingredientCount) {
        return null;
      }
      if (conversion !== null && omitted !== null) {
        return null; // a conversion and an omitted reason cannot both be true
      }
      if (conversion === null && omitted === null) {
        return null;
      }
      if (
        conversion !== null &&
        item.capacity_origin === "unresolved" &&
        CONVERSION_SUCCEEDED.has(conversion.status)
      ) {
        return null; // no number may come from a capacity nobody established
      }
    } else if (conversion !== null || omitted !== null || position !== null) {
      // Every other outcome carries no conversion and names no ingredient (15.8.2).
      return null;
    }
    var approximation = item.source_approximation;
    if (!isObject(approximation) || typeof approximation.approximate !== "boolean") {
      return null;
    }
    var word = approximation.word === undefined ? null : approximation.word;
    if (approximation.approximate === true) {
      if (!isEnum(word, APPROXIMATION_WORDS)) {
        return null;
      }
    } else if (word !== null) {
      return null;
    }
    // The coordinate gate. The server's offsets are code points; the page slices UTF-16
    // code units, so both ends go through the one mapping and the slice must be the text
    // the server says it is.
    var start16 = codePointToUtf16(stepText, item.start);
    var end16 = codePointToUtf16(stepText, item.end);
    if (start16 < 0 || end16 < 0) {
      return null;
    }
    if (stepText.slice(start16, end16) !== item.text) {
      return null;
    }
    return {
      start: item.start,
      end: item.end,
      text: item.text,
      link: item.link,
      linkReason: item.link_reason,
      conversion: conversion,
      capacityOrigin: item.capacity_origin,
      sourceApproximate: approximation.approximate === true,
      sourceWord: word,
    };
  }

  // One `StepAnnotation`, or null when any of its data is not trustworthy for the step the
  // page holds. One bad span discards the whole step's hints (never the recipe), because a
  // partial set of annotations cannot be shown without guessing which one lied.
  function normalizeStepAnnotation(item, steps, ingredientCount) {
    if (!isObject(item)) {
      return null;
    }
    if (!isCount(item.step_index) || item.step_index >= steps.length) {
      return null;
    }
    if (typeof item.step_text !== "string" || item.step_text !== steps[item.step_index]) {
      return null;
    }
    if (typeof item.text_sha256 !== "string" || !STEP_TEXT_HASH.test(item.text_sha256)) {
      return null;
    }
    if (!isEnum(item.status, STEP_STATUSES)) {
      return null;
    }
    if (!Array.isArray(item.spans)) {
      return null;
    }
    if (item.status === "failed") {
      // The step's own analysis failed: its stored text is drawn, with nothing derived.
      return { stepIndex: item.step_index, text: item.step_text, spans: [] };
    }
    var spans = [];
    var previousEnd = 0;
    for (var index = 0; index < item.spans.length; index++) {
      var span = normalizeSpanAnnotation(item.spans[index], item.step_text, ingredientCount);
      if (span === null || span.start < previousEnd) {
        return null; // unsorted, overlapping or unusable: no hints for this step
      }
      previousEnd = span.end;
      spans.push(span);
    }
    return { stepIndex: item.step_index, text: item.step_text, spans: spans };
  }

  // The annotations of a block, keyed by step. A `step_index` that appears more than once is
  // a step-level integrity failure: every entry for that index is discarded, in whichever
  // order they arrived and whichever one looks valid, so a corrupt duplicate can never leave
  // a hint on a step whose annotations disagree about what they describe. The stored text of
  // that step is drawn as it is; other steps and the ingredient hints are untouched.
  function normalizeStepAnnotations(items, steps, ingredientCount) {
    var counts = Object.create(null);
    for (var raw = 0; raw < items.length; raw++) {
      var entry = items[raw];
      if (isObject(entry) && isCount(entry.step_index) && entry.step_index < steps.length) {
        counts[entry.step_index] = (counts[entry.step_index] || 0) + 1;
      }
    }
    var found = [];
    for (var index = 0; index < items.length; index++) {
      var annotation = normalizeStepAnnotation(items[index], steps, ingredientCount);
      if (annotation === null || counts[annotation.stepIndex] > 1) {
        continue;
      }
      found.push(annotation);
    }
    found.sort(function (a, b) {
      return a.stepIndex - b.stepIndex;
    });
    return found;
  }

  // The additive measure block. A block this page cannot use is not an error of the
  // recipe: normalizeFavoriteDetail ignores it and the card is drawn as it always was.
  //
  // A `schema_version: 2` block binds its step hints to `favorite_version` and the exact
  // `steps` it echoes. A malformed additive field costs only the hints it describes; the
  // released B1 ingredient block and the recipe itself are never discarded for it.
  //
  // `ingredients` is the very array the card draws (each entry its stored `name` and
  // `amount`): the B2 mass hints are checked against it, not against a count, so a hint can
  // never land on a line it does not describe.
  //
  // `favoriteVersion` is the normalized card version, which keeps the released fallback of
  // `1` for a missing or invalid top-level `version`. `originalVersion` is that same version
  // only when the API really supplied a positive safe integer, and `null` otherwise: the B2
  // gate (KH-F01-B2-3-R2) reads it instead, so the fallback can never authorize a hint.
  function normalizeMeasuresBlock(value, ingredients, steps, favoriteVersion, originalVersion) {
    if (!isObject(value) || !isObject(value.target)) {
      return null;
    }
    var ingredientCount = ingredients.length;
    var target = {};
    for (var index = 0; index < MEASURE_KEYS.length; index++) {
      var name = MEASURE_KEYS[index];
      if (!isCount(value.target[name])) {
        return null;
      }
      target[name] = value.target[name];
    }
    var sourceGlass = value.source_glass_ml;
    if (
      sourceGlass !== null &&
      sourceGlass !== undefined &&
      (!Number.isSafeInteger(sourceGlass) || sourceGlass < 50 || sourceGlass > 1000)
    ) {
      return null;
    }
    if (typeof value.source_glass_required !== "boolean") {
      return null;
    }
    if (!Array.isArray(value.ingredients)) {
      return null;
    }
    var seen = {};
    var conversions = [];
    var clarified = {};
    for (var position = 0; position < value.ingredients.length; position++) {
      var conversion = normalizeIngredientConversion(value.ingredients[position], ingredientCount);
      if (conversion === null || seen[conversion.position] === true) {
        return null;
      }
      seen[conversion.position] = true;
      // A successful conversion beside a glass term whose capacity was never established is
      // a contradiction Stage C does not produce: an undeclared glass without a clarification
      // is `clarification_required`, never a converted number. The number is refused here,
      // whatever clarification the view currently holds -- nothing is drawn from a capacity
      // nobody established. Only this conversion is lost: its line keeps the stored text, and
      // the block's other conversions stay usable (15.8.4; AC86, AC88).
      if (conversionContradictsSourceTerms(value.ingredients[position])) {
        continue;
      }
      if (conversionNeedsClarification(value.ingredients[position])) {
        clarified[conversion.position] = true;
      }
      conversions.push(conversion);
    }
    // The additive B1.1 fields. A block without `schema_version` is the released B1 block:
    // no step data, and nothing asked of it that it never promised.
    var versioned = value.schema_version === 2;
    // The steps and the B2 extension are drawn only for the version on the card: an answer
    // that names another one may have been written for text the page no longer shows.
    var sameVersion =
      Number.isSafeInteger(value.favorite_version) && value.favorite_version === favoriteVersion;
    // KH-F01-B2-3-R2: the mass section is gated on the version the API actually supplied.
    // `sameVersion` above is enough for the released step hints, but its `favoriteVersion`
    // has already been defaulted to 1 when the detail carried none. A section naming that
    // fallback must not draw a B2 number: with no trustworthy original version there is
    // nothing to prove the hint was built for this very card.
    var b2SameVersion =
      Number.isSafeInteger(originalVersion) &&
      originalVersion >= 1 &&
      Number.isSafeInteger(value.favorite_version) &&
      value.favorite_version === originalVersion;
    var annotations = [];
    var stepsGlassRequired = false;
    var analysisStatus = null;
    var provenanceStatus = null;
    if (versioned) {
      if (typeof value.steps_source_glass_required === "boolean") {
        stepsGlassRequired = value.steps_source_glass_required === true;
      }
      if (isObject(value.analysis) && isEnum(value.analysis.status, ANALYSIS_STATUSES)) {
        analysisStatus = value.analysis.status;
      }
      // The provenance audit is the ingredient side's own component status; a step failure
      // and a provenance failure are said apart (15.8.2).
      if (isObject(value.provenance) && isEnum(value.provenance.status, PROVENANCE_STATUSES)) {
        provenanceStatus = value.provenance.status;
      }
      if (sameVersion && Array.isArray(value.steps)) {
        annotations = normalizeStepAnnotations(value.steps, steps, ingredientCount);
      }
    }
    return {
      target: target,
      sourceGlassMl: sourceGlass === undefined ? null : sourceGlass,
      sourceGlassRequired: value.source_glass_required,
      ingredients: conversions,
      // The ingredient lines whose hint may rest on the temporary clarification. Nothing
      // is drawn from them after the safe «Не знаю» answer withdrew that capacity. A
      // legacy block names none because none of its terms can prove independence, so every
      // line of one is treated conservatively.
      clarifiedPositions: clarified,
      // KH-F01-B1.1: the step hints, the independent "a step glass is undeclared" flag and
      // the summary of what the server's analysis managed to do.
      steps: annotations,
      stepsGlassRequired: stepsGlassRequired,
      analysisStatus: analysisStatus,
      provenanceStatus: provenanceStatus,
      // KH-F01-B2: the ingredient-specific mass estimates, read in their own section with
      // their own schema version, bound to these ingredients and this version. Only a
      // versioned block (schema_version 2) whose favorite_version is this card's may say
      // anything about its ingredients; a legacy, versionless or stale one yields no
      // hints (a conservative B2-only failure) and cannot change any field above it.
      massConversions: normalizeMassConversions(
        value.mass_conversions,
        ingredients,
        versioned && b2SameVersion
      ),
    };
  }

  // The recipe itself: the summary and the text, for the very favorite that was asked for.
  // A structured favorite also carries its fields: the card draws those, not the text.
  function normalizeFavoriteDetail(body, id) {
    var item = normalizeFavorite(body);
    if (item === null || item.id !== id || typeof body.text !== "string" || body.text === "") {
      return null;
    }
    var notes = body.notes === undefined || body.notes === null ? "" : body.notes;
    if (typeof notes !== "string") {
      return null;
    }
    item.text = body.text;
    item.notes = notes;
    item.updatedAt = parseTimestamp(body.updated_at);
    if (item.kind === "structured") {
      var servings = body.servings === undefined || body.servings === null ? "" : body.servings;
      var ingredients = normalizeIngredients(body.ingredients);
      var steps = normalizeSteps(body.steps);
      if (typeof servings !== "string" || ingredients === null || steps === null) {
        return null;
      }
      item.servings = servings;
      item.ingredients = ingredients;
      item.steps = steps;
      // Additive and optional: an older or unusual answer simply draws no hints. The step
      // hints and the B2 mass estimates are read against the very ingredients, steps and
      // version this card is about to draw.
      //
      // KH-F01-B2-3-R2: `item.version` keeps the released fallback of 1 for a missing or
      // invalid `version`, which the step hints still use as before. The B2 gate receives
      // the original value only when the API really supplied a positive safe integer, so a
      // defaulted or coerced version can never authorize a mass hint.
      var originalVersion =
        Number.isSafeInteger(body.version) && body.version >= 1 ? body.version : null;
      item.measures = normalizeMeasuresBlock(
        body.measures,
        ingredients,
        steps,
        item.version,
        originalVersion
      );
    } else {
      item.servings = "";
      item.ingredients = [];
      item.steps = [];
      item.measures = null;
    }
    return item;
  }

  // The answer to POST /api/favorites, as what the page must do next:
  //   kept       201 (kept now) or 200 (it already was): the favorite
  //   auth       401: the session is gone
  //   rejected   the application refused it for a reason of the request (a 4xx): `text` is its words
  //   uncertain  no answer, a body that is not the application's, any 5xx: the outcome is not known.
  //              Keeping an answer is idempotent on the server, so the person may simply ask again.
  function classifyFavoriteKeep(response) {
    if (response.kind === "network") {
      return { type: "uncertain", text: unavailableText(response, null) };
    }
    if (response.status === 401) {
      return { type: "auth" };
    }
    if ((response.status === 201 || response.status === 200) && response.jsonOk) {
      var favorite = normalizeFavorite(response.json);
      if (favorite !== null) {
        return { type: "kept", favorite: favorite, created: response.status === 201 };
      }
      return { type: "uncertain", text: TEXT.unexpected };
    }
    var error = readApiError(response);
    if (error && response.status >= 400 && response.status < 500) {
      return { type: "rejected", text: error.message || TEXT.favoriteKeepFailed, correlationId: error.correlationId };
    }
    return { type: "uncertain", text: unavailableText(response, error), correlationId: error ? error.correlationId : null };
  }

  // The answer to PATCH /api/favorites/{id}, as what the editor must do next:
  //   saved     200: the recipe as it now is (with its new version)
  //   auth      401: the session is gone
  //   gone      404 favorite_not_found: it was removed somewhere else
  //   conflict  409 favorite_conflict: the recipe changed since expected_version; the draft stays
  //   rejected  another 4xx: the application refused the edit; the draft stays
  //   uncertain no answer, a body that is not the application's, any 5xx: the edit MAY have
  //             been stored (a lost acknowledgement is a 503); the draft stays, and asking
  //             again with the same expected_version either succeeds or reports a conflict.
  function classifyFavoriteEdit(response, id) {
    if (response.kind === "network") {
      return { type: "uncertain", text: unavailableText(response, null) };
    }
    if (response.status === 401) {
      return { type: "auth" };
    }
    if (response.status === 200 && response.jsonOk) {
      var item = normalizeFavoriteDetail(response.json, id);
      if (item !== null) {
        return { type: "saved", item: item };
      }
      return { type: "uncertain", text: TEXT.unexpected };
    }
    var error = readApiError(response);
    if (response.status === 404 && error && error.code === "favorite_not_found") {
      return { type: "gone" };
    }
    if (response.status === 409 && error && error.code === "favorite_conflict") {
      return { type: "conflict", text: TEXT.editConflict, correlationId: error.correlationId };
    }
    if (error && response.status >= 400 && response.status < 500) {
      return {
        type: "rejected",
        text: error.message || TEXT.editFailed,
        correlationId: error.correlationId,
      };
    }
    return {
      type: "uncertain",
      text: unavailableText(response, error),
      correlationId: error ? error.correlationId : null,
    };
  }

  // The answer to DELETE /api/favorites/{id}:
  //   removed    204, or 404 favorite_not_found: it is not in the favorites any more, which is what was asked for
  //   auth       401
  //   failed     anything else: it may still be there
  function classifyFavoriteRemove(response) {
    if (response.kind === "network") {
      return { type: "failed", text: unavailableText(response, null) };
    }
    if (response.status === 401) {
      return { type: "auth" };
    }
    if (response.status === 204) {
      return { type: "removed" };
    }
    var error = readApiError(response);
    if (response.status === 404 && error && error.code === "favorite_not_found") {
      return { type: "removed" };
    }
    return { type: "failed", text: unavailableText(response, error), correlationId: error ? error.correlationId : null };
  }

  // The answer to DELETE /api/conversations/{id}:
  //   deleted    204: it is gone
  //   gone       404 conversation_not_found: it is already gone (or was never this person's), which is
  //              the outcome that was wanted; nothing more is known or needed
  //   auth       401
  //   failed     anything else: it may still be there
  function classifyConversationDelete(response) {
    if (response.kind === "network") {
      return { type: "failed", text: unavailableText(response, null), correlationId: null };
    }
    if (response.status === 401) {
      return { type: "auth" };
    }
    if (response.status === 204) {
      return { type: "deleted" };
    }
    var error = readApiError(response);
    if (response.status === 404 && error && error.code === "conversation_not_found") {
      return { type: "gone" };
    }
    return { type: "failed", text: unavailableText(response, error), correlationId: error ? error.correlationId : null };
  }

  // --- shopping lists: what the server says about a saved checklist ------------------------------------

  var SHOPPING_STATES = { unchecked: true, at_home: true, purchased: true };
  var SHOPPING_STATUSES = { active: true, completed: true };
  // A shopping-list item is one line with no control character at all (a tab or a newline
  // included), which is stricter than a message's rule: the server refuses them too.
  var ITEM_CONTROL_CHARACTER = /[\u0000-\u001f\u007f-\u009f]/;

  function validItemText(value) {
    return (
      typeof value === "string" &&
      value !== "" &&
      !ITEM_CONTROL_CHARACTER.test(value) &&
      !hasLoneSurrogate(value)
    );
  }

  // The position is the item's order in the list, not its index: the server keeps it
  // monotonically increasing (max + 1), so a list that has lost items can hold positions
  // at or above the item cap. Only the number of items is capped.
  function normalizeShoppingItem(item) {
    if (
      !isObject(item) ||
      typeof item.id !== "string" ||
      !UUID_PATTERN.test(item.id) ||
      !isCount(item.position) ||
      !validItemText(item.name) ||
      (item.amount !== null && item.amount !== undefined && !validItemText(item.amount)) ||
      typeof item.state !== "string" ||
      SHOPPING_STATES[item.state] !== true
    ) {
      return null;
    }
    return {
      id: item.id.toLowerCase(),
      position: item.position,
      name: item.name,
      amount: item.amount === undefined ? null : item.amount,
      state: item.state,
      createdAt: parseTimestamp(item.created_at),
      updatedAt: parseTimestamp(item.updated_at),
    };
  }

  function normalizeShoppingDetail(body) {
    if (
      !isObject(body) ||
      typeof body.id !== "string" ||
      !UUID_PATTERN.test(body.id) ||
      typeof body.title !== "string" ||
      body.title === "" ||
      typeof body.status !== "string" ||
      SHOPPING_STATUSES[body.status] !== true ||
      !Array.isArray(body.items) ||
      body.items.length > MAX_SHOPPING_ITEMS ||
      (body.created !== undefined && typeof body.created !== "boolean")
    ) {
      return null;
    }
    var items = [];
    var seen = {};
    for (var i = 0; i < body.items.length; i++) {
      var item = normalizeShoppingItem(body.items[i]);
      if (item === null || seen[item.position] === true) {
        return null;
      }
      seen[item.position] = true;
      items.push(item);
    }
    items.sort(function (left, right) {
      return left.position - right.position;
    });
    return {
      id: body.id.toLowerCase(),
      title: body.title,
      status: body.status,
      createdAt: parseTimestamp(body.created_at),
      updatedAt: parseTimestamp(body.updated_at),
      items: items,
      created: body.created === true,
    };
  }

  // The count of items in each state, read from the items themselves (the server-backed data).
  function shoppingCounts(items) {
    var counts = { total: items.length, unchecked: 0, at_home: 0, purchased: 0 };
    for (var i = 0; i < items.length; i++) {
      counts[items[i].state]++;
    }
    return counts;
  }

  function normalizeShoppingSummary(item) {
    if (
      !isObject(item) ||
      typeof item.id !== "string" ||
      !UUID_PATTERN.test(item.id) ||
      typeof item.title !== "string" ||
      item.title === "" ||
      typeof item.status !== "string" ||
      SHOPPING_STATUSES[item.status] !== true ||
      !isObject(item.counts) ||
      !isCount(item.counts.total) ||
      !isCount(item.counts.unchecked) ||
      !isCount(item.counts.at_home) ||
      !isCount(item.counts.purchased) ||
      item.counts.unchecked + item.counts.at_home + item.counts.purchased !== item.counts.total
    ) {
      return null;
    }
    return {
      id: item.id.toLowerCase(),
      title: item.title,
      status: item.status,
      createdAt: parseTimestamp(item.created_at),
      updatedAt: parseTimestamp(item.updated_at),
      counts: {
        total: item.counts.total,
        unchecked: item.counts.unchecked,
        at_home: item.counts.at_home,
        purchased: item.counts.purchased,
      },
    };
  }

  function normalizeShoppingLists(body) {
    if (!isObject(body) || !Array.isArray(body.lists)) {
      return null;
    }
    var items = [];
    for (var i = 0; i < body.lists.length; i++) {
      var summary = normalizeShoppingSummary(body.lists[i]);
      if (summary === null) {
        return null;
      }
      items.push(summary);
    }
    return items;
  }

  // One stored recipe of an answer, for a draft: the server's validated fields, copied as they are.
  function normalizeRecipeRead(body, conversationId, messageId, recipeIndex) {
    if (
      !isObject(body) ||
      body.message_id !== messageId ||
      body.position !== recipeIndex ||
      typeof body.title !== "string" ||
      body.title === "" ||
      (body.servings !== null && body.servings !== undefined && typeof body.servings !== "string") ||
      !Array.isArray(body.ingredients) ||
      body.ingredients.length < 1 ||
      body.ingredients.length > MAX_EDIT_INGREDIENTS
    ) {
      return null;
    }
    var ingredients = [];
    for (var i = 0; i < body.ingredients.length; i++) {
      var ingredient = body.ingredients[i];
      if (
        !isObject(ingredient) ||
        !validItemText(ingredient.name) ||
        !validItemText(ingredient.amount)
      ) {
        return null;
      }
      ingredients.push({ name: ingredient.name, amount: ingredient.amount });
    }
    return {
      title: body.title,
      servings: typeof body.servings === "string" ? body.servings : "",
      ingredients: ingredients,
    };
  }

  // The answer to the recipe read, as what the draft screen must do next:
  //   ok         200 and the recipe of this very message and position
  //   auth       401
  //   gone       404: the answer or the recipe is not there (expired, deleted, another person's)
  //   unavailable no answer, a body that is not the application's, any 5xx
  function classifyRecipeRead(response) {
    if (response.kind === "network") {
      return { type: "unavailable", text: unavailableText(response, null), correlationId: null };
    }
    if (response.status === 401) {
      return { type: "auth" };
    }
    var error = readApiError(response);
    if (response.status === 200 && response.jsonOk && isObject(response.json)) {
      return { type: "ok", body: response.json };
    }
    if (
      response.status === 404 &&
      error &&
      (error.code === "recipe_not_found" ||
        error.code === "message_not_found" ||
        error.code === "conversation_not_found")
    ) {
      return { type: "gone", text: error.message, correlationId: error.correlationId };
    }
    return {
      type: "unavailable",
      text: unavailableText(response, error),
      correlationId: error ? error.correlationId : null,
    };
  }

  // The answer to POST /api/shopping-lists, as what the draft must do next:
  //   saved      201 (saved now) or 200 (this exact request already saved it): the list
  //   auth       401
  //   reused     409 shopping_list_key_reused: the key was used for another request. Never
  //              replaced by another key without the person asking for it.
  //   deleted    409 shopping_list_deleted: the key's list has been deleted; nothing is recreated
  //   rejected   another 4xx: the application refused the request; nothing was written
  //   uncertain  no answer, a body that is not the application's, any 5xx: the list MAY exist
  function classifyShoppingCreate(response) {
    if (response.kind === "network") {
      return { type: "uncertain", reason: response.timedOut ? "timeout" : "network", text: null };
    }
    if (response.status === 401) {
      return { type: "auth" };
    }
    if (response.status === 200 || response.status === 201) {
      if (response.jsonOk) {
        var detail = normalizeShoppingDetail(response.json);
        if (detail !== null) {
          return { type: "saved", detail: detail };
        }
      }
      return { type: "uncertain", reason: "proxy", text: TEXT.unexpected, correlationId: null };
    }
    var error = readApiError(response);
    var text = error && error.message ? error.message : null;
    var correlationId = error ? error.correlationId : null;
    if (response.status === 409 && error && error.code === "shopping_list_key_reused") {
      return { type: "reused", text: text, correlationId: correlationId };
    }
    if (response.status === 409 && error && error.code === "shopping_list_deleted") {
      return { type: "deleted", text: text, correlationId: correlationId };
    }
    if (error && response.status >= 400 && response.status < 500) {
      return { type: "rejected", text: text, correlationId: correlationId, code: error.code };
    }
    if (response.status >= 500) {
      return { type: "uncertain", reason: "server", text: text, correlationId: correlationId };
    }
    return { type: "uncertain", reason: "proxy", text: null, correlationId: correlationId };
  }

  // The answer to an item or list write (POST/PATCH/DELETE). The body is the server's own
  // row and is normalized by the caller.
  function classifyShoppingWrite(response) {
    if (response.kind === "network") {
      return { type: "uncertain", text: unavailableText(response, null), correlationId: null };
    }
    if (response.status === 401) {
      return { type: "auth" };
    }
    if (response.status === 200 || response.status === 201) {
      return response.jsonOk && isObject(response.json)
        ? { type: "ok", body: response.json }
        : { type: "uncertain", text: TEXT.unexpected, correlationId: null };
    }
    if (response.status === 204) {
      return { type: "removed" };
    }
    var error = readApiError(response);
    var common = { text: error && error.message ? error.message : null, correlationId: error ? error.correlationId : null };
    if (error && response.status === 404) {
      return {
        type: "not_found",
        code: error.code,
        text: common.text,
        correlationId: common.correlationId,
      };
    }
    if (error && response.status === 409 && error.code === "shopping_list_completed") {
      return { type: "completed", text: common.text, correlationId: common.correlationId };
    }
    if (error && response.status >= 400 && response.status < 500) {
      return {
        type: "rejected",
        code: error.code,
        text: common.text,
        correlationId: common.correlationId,
      };
    }
    return { type: "uncertain", text: unavailableText(response, error), correlationId: common.correlationId };
  }

  var MESSAGE_STATUSES = { completed: true, in_progress: true, failed: true };

  // The address of a photo that came with a message: this site's own route and nothing else. The server
  // names it (chat_api._attachment_json); anything that is not exactly that is no photo.
  var PHOTO_URL = /^\/api\/attachments\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

  // The structured recipes of one answer, as the server names them: a position and a
  // title, nothing fabricated. A malformed entry is dropped rather than allowed to spoil
  // the whole message; positions are unique and in order.
  function normalizeRecipes(value) {
    if (!Array.isArray(value)) {
      return [];
    }
    var seen = {};
    var items = [];
    for (var i = 0; i < value.length; i++) {
      var item = value[i];
      if (
        !isObject(item) ||
        !isCount(item.position) ||
        item.position < 0 ||
        item.position >= MAX_RECIPES_PER_MESSAGE ||
        typeof item.title !== "string" ||
        item.title === "" ||
        seen[item.position] === true
      ) {
        continue;
      }
      seen[item.position] = true;
      items.push({ position: item.position, title: item.title });
    }
    items.sort(function (left, right) {
      return left.position - right.position;
    });
    return items;
  }

  function normalizePhoto(list) {
    if (!Array.isArray(list) || list.length < 1) {
      return null;
    }
    var first = list[0];
    if (!isObject(first) || typeof first.url !== "string" || !PHOTO_URL.test(first.url)) {
      return null;
    }
    if (first.content_type !== "image/jpeg" && first.content_type !== "image/png" && first.content_type !== "image/webp") {
      return null;
    }
    return { url: first.url };
  }

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
      photo: normalizePhoto(item.attachments),
      recipes: item.role === "assistant" ? normalizeRecipes(item.recipes) : [],
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
    // The server's feature switch (the shell's data-structured-recipes): with it off the
    // page offers no per-recipe Save control, even for an answer that still has stored
    // recipes from before it was switched off.
    var structuredRecipes = deps.structuredRecipes === true;
    var store = createPendingStore(deps.storage || null, nowFn);

    var epoch = 0; // bumped when the session ends: answers that arrive after it are ignored
    // Bumped when a deletion is confirmed: a list read that began before it is of a list that
    // still had the conversation, and its answer must not put it back.
    var listEpoch = 0;
    // Counts the beginnings of list reads: an answer to an older read never replaces the answer
    // of a newer one (a deletion starts a fresh read, which a reload may in turn overtake).
    var listRead = 0;
    // Orders the profile's reads and saves. An answer to an older read never replaces the answer
    // of a newer one, and a read that was out before a save was accepted must not put back the
    // values that save replaced (its answer may have been made before the change, or before the
    // save was known). A save that fails changes nothing, so the reads that are out stay valid.
    var profileRevision = 0;
    // The same for the shopping lists: an older read of the list or of one list's items never
    // replaces a newer answer, and a write, a deletion or a disappearance makes every read of
    // that list that began before it stale. The detail reads are counted per list (the counts
    // are part of the state, below), so an answer about one list can never make a read of
    // another list stale.
    var shoppingListsRead = 0;
    // The writes of saved lists that have not settled, by list id. One write per list at a
    // time: a second one is refused (the controls are drawn from the same guard) instead of
    // being allowed to overlap the first, and every write -- its reconciliation included --
    // ends here, whatever its answer was.
    var shoppingMutations = {};
    // A deletion was confirmed while the page was held back (the server's answer to who is
    // signed in was still out): the list is read as soon as the same person is let through.
    var listRefreshOnUnlock = false;
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

    // What the page knows of the favorites. `items` newest first; `currentId` is the recipe that is open
    // (section "favorites") and `detail` its state; `saving` is per message id for a whole answer, or
    // per "message:position" for one structured recipe (a save that is under way or failed); `removing`
    // is the question "remove it?" and its outcome, for one recipe; `editor` is the draft being edited,
    // if any. The editor is memory only and survives a list refresh.
    function freshFavorites() {
      return {
        status: "idle",
        error: null,
        items: [],
        currentId: null,
        detail: null,
        saving: {},
        removing: null,
        editor: null,
        // The source-glass clarification for the recipe that is open, in whole millilitres,
        // or null. It is page memory only: it is sent as the detail read's query parameter,
        // is never written to the profile or the recipe, and starts again for another recipe.
        // Only a choice the server has confirmed is kept here: its block and this value are
        // updated together, so what is drawn always belongs to the same source capacity.
        sourceGlass: null,
        // The choice whose detail read is under way, or null. While it is here the card keeps
        // the confirmed choice and its block, and the page says that the new one is pending.
        sourceGlassPending: null,
        // The last choice whose read failed: {value, text, correlationId} or null. The
        // confirmed choice is untouched; the person may ask for the failed one again.
        sourceGlassError: null,
        // The custom part of the question as the person is working in it: the exact text
        // of the field and whether that part is shown. Neither is a confirmation -- a
        // typed capacity becomes `sourceGlass` only with the server's own answer for it --
        // and neither is a setting: both are page memory for the open recipe only.
        sourceGlassOtherOpen: false,
        sourceGlassDraft: "",
        // The safe answer to the source-glass question: the person does not know the
        // recipe's glass, so no capacity is assumed for it. Page memory for the open
        // recipe only -- it closes the question, clears any temporary clarification and
        // is never stored; opening the recipe again starts from its own text.
        sourceGlassUnknown: false,
        // Counts the detail reads of the open card: only the newest answer may change what
        // is drawn, so a slow answer can never overwrite a newer choice or a newer save.
        detailReadSeq: 0,
      };
    }

    // What the page knows of the shopping lists: the sidebar's summaries, the open list (its
    // items, its inline editor and its confirmations) and the draft being prepared. The draft is
    // memory only: nothing of it is stored in the browser, and it is never overwritten by a
    // refresh of the saved lists.
    function freshShopping() {
      return {
        status: "idle",
        error: null,
        lists: [],
        currentId: null,
        detail: null,
        draft: null,
        notice: null,
      };
    }

    function freshShoppingDetail(id) {
      return {
        id: id,
        status: "loading", // loading | ready | error | gone
        item: null,
        error: null,
        editor: null,
        adding: null,
        removing: null,
        deleting: null,
        actionError: null,
        statusBusy: false,
        // The write of this list that is under way, if any: { kind }. A write starts only
        // when this and shoppingMutations[id] are both clear, and ends only after its
        // outcome (and its reconciliation, when the outcome was unclear) has been applied.
        mutation: null,
      };
    }

    // What the page knows of the profile. `loaded` distinguishes "nothing has been read
    // yet" from a profile whose display name is null; `editing` says whether the pane shows
    // the form or the saved values; `saving` is one PATCH under way; `notice` is what the
    // last save said, and `fieldError` the name's own refusal.
    function freshProfile() {
      return {
        status: "idle",
        loaded: false,
        error: null,
        displayName: null,
        textSize: "normal",
        measures: null,
        loginMethods: [],
        // False on the way in and after every fresh read: the pane opens on the saved values,
        // and only «Изменить» opens the form. A refused or failed save leaves it true.
        editing: false,
        saving: false,
        notice: null,
        fieldError: null,
        // A refusal of the household measures as a group: the form is drawn with it rather
        // than with a name error, and the draft is kept.
        measureError: null,
        // What is in the form while it has not been saved: the exact text, the chosen size
        // and the three measure fields, kept here so a redraw (a failure, a retry) does not
        // lose the person's work. A successful read or save replaces it with the server's
        // values.
        draft: null,
      };
    }

    // The form's strings for the saved measures: an empty field means the system default
    // (no override). An explicit value equal to the default is still that person's choice
    // and stays a non-empty field.
    function measureDraftValues(measures) {
      var values = {};
      for (var index = 0; index < MEASURE_KEYS.length; index++) {
        var name = MEASURE_KEYS[index];
        var setting = measures ? measures[name] : null;
        values[name] = setting && setting.override !== null ? String(setting.override) : "";
      }
      return values;
    }

    function draftMeasureValues(value) {
      var values = measureDraftValues(state.profile.measures);
      if (value && typeof value === "object") {
        for (var index = 0; index < MEASURE_KEYS.length; index++) {
          var name = MEASURE_KEYS[index];
          if (typeof value[name] === "string") {
            values[name] = value[name];
          }
        }
      }
      return values;
    }

    function freshState(screen) {
      return {
        screen: screen,
        // Which part of the app is in front: the conversations, the favorites, the shopping
        // lists or the profile.
        section: "chats",
        favorites: freshFavorites(),
        shopping: freshShopping(),
        profile: freshProfile(),
        // The detail reads of the shopping lists, by list id. They belong to one page of one
        // person: a new state (another start, another sign-in) begins them from nothing, so
        // nothing of an earlier account's reads is kept or added to.
        shoppingDetailReads: {},
        // While set, the signed-in page is held back: nothing of the conversations is in the view and
        // nothing is asked for them, until /auth/me has said who is signed in. { phase: "checking" } while
        // the question is out; { phase: "unavailable", failure } when it could not be answered.
        gate: null,
        notice: null,
        failure: null,
        list: { items: [], hasMore: false, status: "idle", error: null },
        // The conversation whose deletion is being asked about or is under way:
        // { id, phase: "asking" | "deleting" | "error", error }.
        deleting: null,
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
      // The new state releases everything the page kept for the person who was here: the
      // threads, the favorites, the open list and its draft -- and the per-list read counts,
      // which must not grow from one account to the next (an answer still on its way is
      // stale by the epoch as well).
      state = freshState(screen);
      listRefreshOnUnlock = false;
      shoppingMutations = {};
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

    // No way of signing in is connected here (see classifyMe): the page says so, calmly, and has
    // nothing to retry. Nobody is signed in and nothing of anyone is shown.
    function showSetup() {
      cancelAllTimers();
      state.screen = "setup";
      state.notice = null;
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
      if (listRefreshOnUnlock) {
        listRefreshOnUnlock = false;
        track(loadList()); // (a deletion was confirmed while the page was held back)
      }
      Object.keys(state.pending).forEach(function (id) {
        var turn = state.pending[id];
        if (turn.phase === "waiting") {
          track(attempt(id));
        } else if (turn.phase === "uncertain" && state.currentId === id) {
          track(reconcile(id));
        }
      });
      if (state.favorites.status === "idle") {
        track(loadFavorites()); // (asked for while it was held back)
      }
      if (state.profile.status === "idle") {
        track(loadProfile()); // (the same for the profile)
      }
      // Returning to the page while «Покупки» is in front reads the server again. A draft is
      // never touched (and is not even a reason to read over it): it is the person's unsaved work.
      if (state.section === "shopping" && state.shopping.draft === null) {
        if (state.shopping.status !== "loading") {
          track(loadShoppingLists());
        }
        track(refreshOpenShoppingList());
      }
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
        } else if (found.type === "setup") {
          showSetup();
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
      track(loadFavorites()); // (beside the conversation: a slow or failed list does not hold it up)
      track(loadProfile()); // (and so does a slow or failed profile: the rest of the page is already usable)
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

    // --- the profile -------------------------------------------------------------------------

    // A failed read is a failure of this pane only: the conversations, the favorites and the
    // shopping lists are already loaded and keep working. A 401 is the session ending, which
    // is the same flow everywhere (handleAuthLost): nothing of the person stays in the page.
    async function loadProfile() {
      if (state.screen !== "app" || state.gate) {
        return false;
      }
      var mine = epoch;
      var revision = ++profileRevision;
      state.profile.status = "loading";
      state.profile.error = null;
      state.profile.notice = null;
      state.profile.fieldError = null;
      state.profile.measureError = null;
      // A fresh read is the authority on the saved values, so the pane it draws shows them
      // rather than a form. A save under way is the exception: its own answer decides, and
      // the form (with what was typed) stays until it does.
      if (!state.profile.saving) {
        state.profile.editing = false;
        state.profile.draft = null;
      }
      emit();
      var response = await http("GET", "/api/profile", { timeoutMs: READ_TIMEOUT_MS });
      if (mine !== epoch || revision !== profileRevision) {
        return false; // a newer read, or a save the server accepted, is the authority now
      }
      var found = classifyRead(response);
      if (found.type === "ok") {
        var profile = normalizeProfile(found.body);
        if (profile !== null) {
          applyProfile(profile);
          state.profile.status = "ready";
          state.profile.error = null;
          emit();
          return true;
        }
        found = { type: "unavailable", text: TEXT.unexpected, correlationId: null };
      }
      if (found.type === "auth") {
        handleAuthLost();
        return false;
      }
      state.profile.status = "error";
      state.profile.error = { text: found.text || TEXT.profileFailed, correlationId: found.correlationId || null };
      emit();
      return false;
    }

    function applyProfile(profile) {
      state.profile.loaded = true;
      state.profile.displayName = profile.displayName;
      state.profile.textSize = profile.textSize;
      state.profile.measures = profile.measures;
      state.profile.loginMethods = profile.loginMethods;
    }

    // The form's way in: the saved values are shown until the person asks to change them.
    function beginProfileEdit() {
      if (
        state.screen !== "app" ||
        state.gate ||
        !state.profile.loaded ||
        state.profile.editing ||
        state.profile.saving
      ) {
        return;
      }
      state.profile.editing = true;
      state.profile.draft = null; // the form starts from the saved values
      state.profile.notice = null; // a previous save's message is about values already shown
      state.profile.fieldError = null;
      state.profile.measureError = null;
      emit();
    }

    // The name, the size and the three measures as one save. Every field the form holds is
    // checked here first (the name as the server checks it; each measure as a whole number
    // in its approved range, an empty field meaning the default), so a refusal is immediate;
    // the server checks everything again and is the authority. Only the fields that changed
    // are sent, and a form that changed nothing sends no PATCH at all.
    async function saveProfile(draft) {
      if (state.screen !== "app" || state.gate || !state.profile.loaded) {
        return { ok: false, reason: "not_ready" };
      }
      if (state.profile.saving) {
        return { ok: false, reason: "busy" };
      }
      var size = draft && TEXT_SIZE_VALUES[draft.textSize] ? draft.textSize : state.profile.textSize;
      // A refusal or a failed save leaves the form exactly where it was, with what the person
      // typed (not the trimmed form), until a save succeeds or a fresh read replaces it.
      state.profile.editing = true;
      state.profile.draft = {
        displayName: draft && typeof draft.displayName === "string" ? draft.displayName : "",
        textSize: size,
        measures: draftMeasureValues(draft ? draft.measures : null),
      };
      var prepared = prepareProfileName(draft ? draft.displayName : "");
      state.profile.notice = null;
      state.profile.fieldError = prepared.ok ? null : prepared.message;
      if (!prepared.ok) {
        state.profile.measureError = null;
        emit();
        return { ok: false, reason: "invalid" };
      }
      var measures = prepareMeasures(draftMeasureValues(draft ? draft.measures : null));
      state.profile.measureError = measures.ok ? null : measures.message;
      if (!measures.ok) {
        state.profile.fieldError = null;
        emit();
        return { ok: false, reason: "invalid" };
      }
      // A form that still holds the saved values has nothing to save: no PATCH is sent, no
      // message claims a save that did not happen, and the pane returns to those values.
      var savedName = state.profile.displayName === null ? "" : state.profile.displayName;
      var changed = {};
      if (prepared.text !== savedName) {
        changed.display_name = prepared.text;
      }
      if (size !== state.profile.textSize) {
        changed.text_size = size;
      }
      var changedMeasures = {};
      for (var index = 0; index < MEASURE_KEYS.length; index++) {
        var name = MEASURE_KEYS[index];
        var setting = state.profile.measures[name];
        if (measures.values[name] !== setting.override) {
          changedMeasures[name] = measures.values[name];
        }
      }
      if (Object.keys(changedMeasures).length) {
        changed.measures = changedMeasures;
      }
      if (!Object.keys(changed).length) {
        state.profile.draft = null;
        state.profile.editing = false;
        state.profile.measureError = null;
        emit();
        return { ok: true, unchanged: true };
      }
      var mine = epoch;
      state.profile.saving = true;
      emit();
      var response = await http("PATCH", "/api/profile", {
        json: changed,
        timeoutMs: READ_TIMEOUT_MS,
      });
      if (mine !== epoch) {
        return { ok: false, reason: "stale" };
      }
      state.profile.saving = false;
      if (response.status === 401) {
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      var error = readApiError(response);
      if (response.status === 200 && response.jsonOk && isObject(response.json)) {
        var profile = normalizeProfile(response.json);
        if (profile !== null) {
          // The server's answer is what the save made true: every profile read that is still
          // out (one begun while the save was pending included) is now older than it.
          profileRevision++;
          applyProfile(profile);
          state.profile.status = "ready";
          state.profile.error = null;
          state.profile.fieldError = null;
          state.profile.measureError = null;
          state.profile.draft = null; // the server's values are what the pane shows now
          state.profile.editing = false; // the save is done: back to the values, read-only
          state.profile.notice = { kind: "ok", text: TEXT.profileSaved };
          announce(TEXT.profileSaved);
          emit();
          return { ok: true };
        }
      }
      if (response.kind === "http" && response.status >= 400 && response.status < 500 && error) {
        if (error.code === "invalid_display_name") {
          state.profile.fieldError = error.message || TEXT.profileNameBadChars;
          state.profile.measureError = null;
          state.profile.notice = null;
        } else if (error.code === "invalid_measure") {
          state.profile.fieldError = null;
          state.profile.measureError = error.message || CODE_MESSAGES.invalid_measure;
          state.profile.notice = null;
        } else {
          state.profile.fieldError = null;
          state.profile.measureError = null;
          state.profile.notice = {
            kind: "error",
            text: error.message || TEXT.profileSaveFailed,
            correlationId: error.correlationId,
          };
        }
        emit();
        return { ok: false, reason: "rejected" };
      }
      state.profile.fieldError = null;
      state.profile.measureError = null;
      state.profile.notice = {
        kind: "error",
        text: unavailableText(response, error),
        correlationId: error ? error.correlationId : null,
      };
      emit();
      return { ok: false, reason: "unavailable" };
    }

    function syncProfileDraft(draft) {
      state.profile.draft = {
        displayName: draft && typeof draft.displayName === "string" ? draft.displayName : "",
        textSize: draft && TEXT_SIZE_VALUES[draft.textSize] ? draft.textSize : state.profile.textSize,
        measures: draftMeasureValues(draft ? draft.measures : null),
      };
    }

    function clearProfileNotice() {
      if (
        state.profile.notice !== null ||
        state.profile.fieldError !== null ||
        state.profile.measureError !== null
      ) {
        state.profile.notice = null;
        state.profile.fieldError = null;
        state.profile.measureError = null;
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
      var listMine = listEpoch;
      var readMine = ++listRead;
      state.list.status = "loading";
      state.list.error = null;
      emit();
      var response = await http("GET", "/api/conversations?limit=" + PAGE_SIZE, { timeoutMs: READ_TIMEOUT_MS });
      if (mine !== epoch || listMine !== listEpoch || readMine !== listRead) {
        return false;
      }
      var found = classifyRead(response);
      if (found.type === "ok") {
        var list = normalizeConversationList(found.body);
        if (list !== null) {
          state.list.items = list.items;
          state.list.hasMore = list.hasMore;
          state.list.status = "ready";
          if (state.deleting && !list.items.some(function (item) { return item.id === state.deleting.id; })) {
            state.deleting = null; // the question was about a conversation the server no longer lists
          }
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
      if (state.deleting && state.deleting.phase !== "deleting") {
        state.deleting = null; // opening something else calls off a question about what was there
      }
      state.section = "chats";
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

    // Everything this page holds of a conversation: the unresolved turn (and its stored record),
    // the history, the list item and any question about deleting it. It does not decide what the
    // page says about it, nor what is open: the callers do (conversationGone, deleteConversation).
    function forgetConversation(id) {
      if (state.pending[id]) {
        removePending(id);
      }
      delete state.threads[id];
      state.list.items = state.list.items.filter(function (item) {
        return item.id !== id;
      });
      if (state.deleting && state.deleting.id === id) {
        state.deleting = null;
      }
    }

    function conversationGone(id) {
      var wasCurrent = state.currentId === id;
      forgetConversation(id);
      if (wasCurrent) {
        state.currentId = null;
        state.banner = { kind: "error", text: TEXT.conversationGone, correlationId: null };
      }
      emit();
    }

    // Deleting is asked first, for one conversation, and done only on the second click.
    function askDeleteConversation(id) {
      if (state.screen !== "app" || state.gate) {
        return;
      }
      if (!state.list.items.some(function (item) { return item.id === id; })) {
        return;
      }
      if (state.deleting && state.deleting.phase === "deleting") {
        return;
      }
      state.deleting = { id: id, phase: "asking", error: null };
      emit();
    }

    function cancelDeleteConversation() {
      if (state.deleting && state.deleting.phase !== "deleting") {
        state.deleting = null;
        emit();
      }
    }

    // The second click. A 404 is the outcome that was wanted (it is already gone, or it was never
    // this person's): the page drops what it held of it and, when it was the open conversation,
    // returns to a new empty draft. Nothing of the conversation is left in memory, including an
    // unresolved turn, whose key is no longer of any use. A failed request keeps everything and
    // shows why, with a retry.
    async function deleteConversation() {
      var asked = state.deleting;
      if (state.screen !== "app" || state.gate || !asked || asked.phase === "deleting") {
        return { ok: false, reason: "not_asked" };
      }
      var mine = epoch;
      var id = asked.id;
      asked.phase = "deleting";
      asked.error = null;
      emit();
      var response = await http("DELETE", "/api/conversations/" + id, { timeoutMs: READ_TIMEOUT_MS });
      if (mine !== epoch) {
        return { ok: false, reason: "stale" };
      }
      var outcome = classifyConversationDelete(response);
      if (outcome.type === "auth") {
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (outcome.type === "deleted" || outcome.type === "gone") {
        var wasCurrent = state.currentId === id;
        listEpoch++; // a list read begun before this answer has the conversation and is not waited for
        forgetConversation(id);
        if (wasCurrent) {
          state.currentId = null; // a new empty draft: the next message makes its conversation
          state.banner = { kind: "info", text: TEXT.deleted, correlationId: null };
        }
        if (state.deleting === asked) {
          state.deleting = null;
        }
        announce(TEXT.deleted);
        emit();
        // The page held the list as it was before this deletion, one conversation short (and
        // perhaps with an older one still beyond its edge): read the list again from the server,
        // which fills it back up to its limit. A read begun before the deletion is ignored
        // (listEpoch); a failed read leaves the list in its error state, with its retry, and
        // still without the conversation that is confirmed gone. While the page is held back
        // nothing may be asked for the conversations, so the read waits for letThrough.
        if (state.gate) {
          listRefreshOnUnlock = true;
        } else {
          track(loadList());
        }
        return { ok: true, wasCurrent: wasCurrent };
      }
      if (state.deleting === asked) {
        asked.phase = "error";
        asked.error = { text: outcome.text || TEXT.deleteFailed, correlationId: outcome.correlationId || null };
        emit();
      }
      return { ok: false, reason: "failed" };
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
      // A conversation that has no name yet is named by its first message, as the server will name it
      // when the list is read again. Only when the first message is known to be in memory: not when an
      // older page is still to be read.
      state.list.items.forEach(function (item) {
        if (item.id === id && !item.title && !thread.hasMore) {
          for (var i = 0; i < thread.messages.length; i++) {
            if (thread.messages[i].role === "user") {
              item.title = deriveTitle(thread.messages[i].text);
              return;
            }
          }
        }
      });
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

    // --- the favorites ---------------------------------------------------------------------------------

    // The whole-answer favorite of an answer, if it has one. A structured favorite of the
    // same answer is a different thing and is not it.
    function favoriteOfMessage(messageId) {
      var items = state.favorites.items;
      for (var i = 0; i < items.length; i++) {
        if (items[i].kind === "text" && items[i].messageId === messageId) {
          return items[i];
        }
      }
      return null;
    }

    function structuredFavoriteOf(messageId, position) {
      var items = state.favorites.items;
      for (var i = 0; i < items.length; i++) {
        if (
          items[i].kind === "structured" &&
          items[i].messageId === messageId &&
          items[i].recipeIndex === position
        ) {
          return items[i];
        }
      }
      return null;
    }

    // The newest first; one favorite per id, and one per (answer, kind, position).
    function sameFavorite(left, right) {
      return (
        left.id === right.id ||
        (left.messageId === right.messageId &&
          left.kind === right.kind &&
          left.recipeIndex === right.recipeIndex)
      );
    }

    function putFavorite(favorite) {
      var others = state.favorites.items.filter(function (item) {
        return !sameFavorite(item, favorite);
      });
      state.favorites.items = [favorite].concat(others);
    }

    // An edit changed the summary the list shows; keep its place (an edit is not news).
    function replaceFavoriteSummary(favorite) {
      var items = state.favorites.items;
      for (var i = 0; i < items.length; i++) {
        if (items[i].id === favorite.id) {
          items[i] = favorite;
          return;
        }
      }
      items.unshift(favorite);
    }

    function dropFavorite(id) {
      state.favorites.items = state.favorites.items.filter(function (item) {
        return item.id !== id;
      });
    }

    async function loadFavorites() {
      if (state.screen !== "app" || state.gate || state.favorites.status === "loading") {
        return false;
      }
      var mine = epoch;
      state.favorites.status = "loading";
      state.favorites.error = null;
      emit();
      var response = await http("GET", "/api/favorites", { timeoutMs: READ_TIMEOUT_MS });
      if (mine !== epoch) {
        return false;
      }
      var favorites = state.favorites;
      var found = classifyRead(response, "favorite_not_found");
      if (found.type === "ok") {
        var items = normalizeFavoriteList(found.body);
        if (items !== null) {
          favorites.items = items;
          favorites.status = "ready";
          // The list is the server's answer: a recipe that is no longer in it was removed
          // somewhere else. Say so for the open card, unless a draft is being edited (it
          // is kept; saving will report the truth of the version).
          if (
            favorites.editor === null &&
            favorites.currentId !== null &&
            favorites.detail !== null &&
            favorites.detail.status === "ready" &&
            !items.some(function (item) {
              return item.id === favorites.currentId;
            })
          ) {
            favorites.detail.status = "gone";
            favorites.detail.error = { text: TEXT.recipeGone, correlationId: null };
            favorites.detail.measuresPending = false;
            favorites.detail.measuresError = null;
            favorites.sourceGlassPending = null;
          }
          emit();
          return true;
        }
        found = { type: "unavailable", text: TEXT.unexpected, correlationId: null };
      }
      if (found.type === "auth") {
        handleAuthLost();
        return false;
      }
      favorites.status = "error";
      favorites.error = { text: found.text || TEXT.favoritesFailed, correlationId: found.correlationId || null };
      emit();
      return false;
    }

    // A capacity that the two offered choices do not name: the one the custom field is for.
    function isCustomGlass(value) {
      return value !== null && value !== 200 && value !== 250;
    }

    // The source-glass clarification as a detail-read query parameter, or nothing. It is
    // only a parameter: nothing is written anywhere.
    function favoriteDetailQuery() {
      var glass = state.favorites.sourceGlass;
      return glass === null ? "" : "?source_glass_ml=" + glass;
    }

    // The open card read again in the background, without putting it into "loading": a
    // recipe changed on another device shows its current fields when the person returns
    // to Favorites. A draft is never touched, and a failed refresh changes nothing (the
    // next edit will report the conflict).
    async function refreshOpenFavorite() {
      var favorites = state.favorites;
      var id = favorites.currentId;
      if (
        id === null ||
        favorites.editor !== null ||
        favorites.detail === null ||
        favorites.detail.status !== "ready" ||
        // A choice under way, or the settled read after a save, is the authority: a
        // background read must not take its place (nor be taken for a newer answer).
        favorites.sourceGlassPending !== null ||
        favorites.detail.measuresPending === true
      ) {
        return false;
      }
      var mine = epoch;
      var seq = ++favorites.detailReadSeq;
      var query = favoriteDetailQuery();
      var response = await http("GET", "/api/favorites/" + id + query, { timeoutMs: READ_TIMEOUT_MS });
      if (
        mine !== epoch ||
        favorites !== state.favorites ||
        favorites.detailReadSeq !== seq ||
        favorites.currentId !== id ||
        favorites.editor !== null ||
        favorites.detail === null ||
        favorites.detail.id !== id ||
        favoriteDetailQuery() !== query // a newer source-glass choice is the authority
      ) {
        return false;
      }
      var detail = favorites.detail;
      var found = classifyRead(response, "favorite_not_found");
      if (found.type === "ok") {
        var item = normalizeFavoriteDetail(found.body, id);
        if (
          item !== null &&
          (item.measures === null || item.measures.sourceGlassMl === favorites.sourceGlass)
        ) {
          detail.status = "ready";
          detail.item = item;
          detail.error = null;
          // This read used the confirmed choice: whatever a previous conversion read
          // said about not having it is no longer true.
          detail.measuresPending = false;
          detail.measuresError = null;
          replaceFavoriteSummary(summaryOf(item));
          emit();
          return true;
        }
        return false;
      }
      if (found.type === "auth") {
        handleAuthLost();
        return false;
      }
      if (found.type === "not_found") {
        dropFavorite(id);
        detail.status = "gone";
        detail.item = null;
        detail.error = { text: TEXT.recipeGone, correlationId: null };
        emit();
        return false;
      }
      return false; // a failed background read is not an error shown over a readable card
    }

    // The open card's conversion read again with the choice it was drawn with -- after an
    // edit, whose own answer carried no clarification, and when the person asks again
    // after such a read failed. The saved fields stay on the card throughout; only the
    // conversion section is pending, and a failure leaves a worded retry instead of a
    // conversion that belongs to another context.
    async function refreshFavoriteConversion(id, glass) {
      var favorites = state.favorites;
      var detail = favorites.detail;
      if (
        state.screen !== "app" ||
        state.gate ||
        favorites.currentId !== id ||
        detail === null ||
        detail.id !== id ||
        detail.status !== "ready" ||
        favorites.sourceGlass !== glass
      ) {
        return false;
      }
      var mine = epoch;
      var seq = ++favorites.detailReadSeq;
      detail.measuresPending = true;
      detail.measuresError = null;
      emit();
      var response = await http(
        "GET",
        "/api/favorites/" + id + (glass === null ? "" : "?source_glass_ml=" + glass),
        { timeoutMs: READ_TIMEOUT_MS }
      );
      if (
        mine !== epoch ||
        favorites !== state.favorites ||
        favorites.detailReadSeq !== seq ||
        favorites.currentId !== id ||
        favorites.sourceGlass !== glass ||
        // A draft opened meanwhile is based on the version on the card; a newer answer
        // must not move that version under it (the editor is asked for again on cancel).
        favorites.editor !== null
      ) {
        return false; // a newer choice, recipe, draft or save is the authority
      }
      detail = favorites.detail;
      if (detail === null || detail.id !== id || detail.status !== "ready") {
        return false;
      }
      var found = classifyRead(response, "favorite_not_found");
      if (found.type === "ok") {
        var item = normalizeFavoriteDetail(found.body, id);
        if (
          item !== null &&
          (item.measures === null || item.measures.sourceGlassMl === glass)
        ) {
          detail.item = item;
          detail.error = null;
          detail.measuresPending = false;
          detail.measuresError = null;
          replaceFavoriteSummary(summaryOf(item));
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
        dropFavorite(id);
        detail.status = "gone";
        detail.item = null;
        detail.error = { text: TEXT.recipeGone, correlationId: null };
        detail.measuresPending = false;
        emit();
        return false;
      }
      detail.measuresPending = false;
      detail.measuresError = {
        text: TEXT.measuresRefreshFailed,
        correlationId: found.correlationId || null,
      };
      emit();
      return false;
    }

    // "Повторить" beside a failed conversion read: the saved card is kept and only its
    // conversion is asked for again, with the choice that was confirmed for it.
    function retryFavoriteConversion() {
      var favorites = state.favorites;
      var id = favorites.currentId;
      if (id === null) {
        return false;
      }
      return refreshFavoriteConversion(id, favorites.sourceGlass);
    }

    // Which part of the app is in front. The list of the favorites (and the open card, when
    // there is one) is read whenever the person returns to it, so a recipe kept or changed
    // in another tab or on another device shows up. The saved shopping lists are read the
    // same way; an open list is read again beside them, and a draft is never touched.
    function showSection(name) {
      if (
        state.screen !== "app" ||
        state.gate ||
        (name !== "chats" && name !== "favorites" && name !== "shopping" && name !== "profile")
      ) {
        return;
      }
      state.section = name;
      emit();
      if (name === "favorites" && state.favorites.status !== "loading") {
        track(loadFavorites());
        track(refreshOpenFavorite());
      }
      if (name === "shopping") {
        track(loadShoppingLists());
        track(refreshOpenShoppingList());
      }
      if (name === "profile" && state.profile.status !== "loading") {
        track(loadProfile());
      }
    }

    // Opening a card from anywhere the person opens one is a new view of it. `keepView` is
    // for the two internal re-reads of the card already open (the retry of a failed first
    // read, and "Загрузить заново" after an edit conflict): those show the same view again,
    // so its clarification stays. No user-facing action reaches this function with it.
    async function openFavorite(rawId, keepView) {
      var id = typeof rawId === "string" ? rawId.toLowerCase() : "";
      if (state.screen !== "app" || state.gate || !UUID_PATTERN.test(id)) {
        return;
      }
      var mine = epoch;
      var favorites = state.favorites;
      if (keepView !== true) {
        // Every explicit opening is a new view of the recipe, even when it is the same
        // recipe again: a clarification belongs to one view of one card, so the confirmed
        // capacity, the custom field's draft, a pending choice, its failure and the safe
        // «Не знаю» answer all start over. The recipe's own declarations are the new
        // view's start. A redraw of the same view never comes through here.
        favorites.sourceGlass = null;
        favorites.sourceGlassOtherOpen = false;
        favorites.sourceGlassDraft = "";
        favorites.sourceGlassUnknown = false;
      }
      // Opening a recipe (the same one again, too) abandons a choice whose read is out and
      // the failure of one: only the newest read of this card may change it.
      var seq = ++favorites.detailReadSeq;
      favorites.sourceGlassPending = null;
      favorites.sourceGlassError = null;
      state.section = "favorites";
      favorites.currentId = id;
      favorites.removing = null; // a question about another recipe is over
      favorites.editor = null; // (the page asked before discarding a draft; see app.js)
      favorites.detail = {
        id: id,
        status: "loading",
        item: null,
        error: null,
        measuresPending: false,
        measuresError: null,
      };
      emit();
      var response = await http("GET", "/api/favorites/" + id + favoriteDetailQuery(), { timeoutMs: READ_TIMEOUT_MS });
      if (
        mine !== epoch ||
        favorites !== state.favorites ||
        favorites.detailReadSeq !== seq ||
        favorites.currentId !== id
      ) {
        return; // the session ended, or another recipe (or a newer read) was asked for meanwhile
      }
      var detail = favorites.detail;
      var found = classifyRead(response, "favorite_not_found");
      if (found.type === "ok") {
        var item = normalizeFavoriteDetail(found.body, id);
        if (item !== null) {
          // A block that names a clarification this read did not is not the card's: the
          // recipe stays readable, without a conversion from another context.
          if (item.measures !== null && item.measures.sourceGlassMl !== favorites.sourceGlass) {
            item.measures = null;
          }
          detail.status = "ready";
          detail.item = item;
          detail.error = null;
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
        dropFavorite(id); // (removed in another window)
        detail.status = "gone";
        detail.error = { text: TEXT.recipeGone, correlationId: null };
        emit();
        return;
      }
      detail.status = "error";
      detail.error = { text: found.text || TEXT.recipeFailed, correlationId: found.correlationId || null };
      emit();
    }

    function reloadRecipe() {
      var id = state.favorites.currentId;
      return id === null ? Promise.resolve() : openFavorite(id, true);
    }

    // The person's answer to "which glass does the recipe mean?": a whole number of
    // millilitres in 50--1000, or null to drop the clarification. The open detail is read
    // again with it. It is the current recipe's presentation only and is never stored.
    //
    // The choice is only confirmed with the block the server answers for it: until that
    // answer arrives the card keeps the previous confirmed choice and its conversion, and
    // a failure keeps them too, with a worded error and a way to ask again. Every read
    // counts (detailReadSeq), so a slow answer for an older choice is discarded.
    async function setSourceGlass(value) {
      if (state.screen !== "app" || state.gate || state.favorites.currentId === null) {
        return { ok: false, reason: "not_ready" };
      }
      // A draft fixes the version and the block it started from; a clarification read
      // could bring a different version under it, so none is started while one is open
      // (the chooser is not drawn then either).
      if (state.favorites.editor !== null) {
        return { ok: false, reason: "editing" };
      }
      var checked = null;
      if (value !== null && value !== undefined) {
        var text = trimLikeServer(typeof value === "string" ? value : String(value));
        if (!/^[0-9]+$/.test(text)) {
          return { ok: false, reason: "invalid" };
        }
        var number = Number(text);
        if (!Number.isSafeInteger(number) || number < 50 || number > 1000) {
          return { ok: false, reason: "invalid" };
        }
        checked = number;
      }
      var favorites = state.favorites;
      var id = favorites.currentId;
      var detail = favorites.detail;
      if (detail === null || detail.id !== id) {
        return { ok: false, reason: "not_ready" };
      }
      if (checked === favorites.sourceGlass && favorites.sourceGlassPending === null) {
        // The confirmed choice was asked for again: nothing to read. A confirmed custom
        // capacity is shown again in the field it belongs to.
        var shown = false;
        if (favorites.sourceGlassError !== null) {
          favorites.sourceGlassError = null;
          shown = true;
        }
        if (isCustomGlass(checked)) {
          if (!favorites.sourceGlassOtherOpen) {
            favorites.sourceGlassOtherOpen = true;
            shown = true;
          }
          if (favorites.sourceGlassDraft !== String(checked)) {
            favorites.sourceGlassDraft = String(checked);
            shown = true;
          }
        }
        if (shown) {
          emit();
        }
        return { ok: true, reason: "unchanged" };
      }
      var mine = epoch;
      var seq = ++favorites.detailReadSeq;
      favorites.sourceGlassPending = checked;
      favorites.sourceGlassError = null;
      if (isCustomGlass(checked)) {
        // The field names the capacity that is being applied (and holds it for a retry);
        // the confirmed one, and the block drawn for it, stay as they were.
        favorites.sourceGlassOtherOpen = true;
        favorites.sourceGlassDraft = String(checked);
      }
      emit(); // (the confirmed choice and its conversion stay drawn while this is out)
      var response = await http(
        "GET",
        "/api/favorites/" + id + (checked === null ? "" : "?source_glass_ml=" + checked),
        { timeoutMs: READ_TIMEOUT_MS }
      );
      if (
        mine !== epoch ||
        favorites !== state.favorites ||
        favorites.detailReadSeq !== seq ||
        favorites.currentId !== id ||
        // An editor opened while this was out: its draft owns the card's version now, and
        // an answer read before that cannot replace it (the read is already invalidated
        // by every read counter; this is the same rule said in the editor's terms).
        favorites.editor !== null
      ) {
        return { ok: false, reason: "stale" }; // a newer choice, card or draft is the authority
      }
      detail = favorites.detail;
      if (detail === null || detail.id !== id || detail.status !== "ready") {
        return { ok: false, reason: "stale" };
      }
      var found = classifyRead(response, "favorite_not_found");
      if (found.type === "auth") {
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (found.type === "not_found") {
        dropFavorite(id); // (removed in another window)
        favorites.sourceGlassPending = null;
        favorites.sourceGlass = null;
        favorites.sourceGlassError = null;
        detail.status = "gone";
        detail.item = null;
        detail.error = { text: TEXT.recipeGone, correlationId: null };
        detail.measuresPending = false;
        detail.measuresError = null;
        emit();
        return { ok: false, reason: "gone" };
      }
      if (found.type === "ok") {
        var item = normalizeFavoriteDetail(found.body, id);
        // The block must belong to the choice that was asked for: a value echoed for
        // another capacity would put a new selection beside an old conversion.
        if (
          item !== null &&
          (item.measures === null || item.measures.sourceGlassMl === checked)
        ) {
          // The confirmed choice and the block drawn for it change together. A custom
          // capacity is the one its field holds when it is reopened; a standard one
          // closes that field, whose text stays as the draft it was.
          favorites.sourceGlassPending = null;
          favorites.sourceGlass = checked;
          // A capacity the server has answered for is the view's answer again: the safe
          // «Не знаю» state belongs to the view that had no capacity.
          favorites.sourceGlassUnknown = false;
          favorites.sourceGlassError = null;
          if (isCustomGlass(checked)) {
            favorites.sourceGlassOtherOpen = true;
            favorites.sourceGlassDraft = String(checked);
          } else {
            favorites.sourceGlassOtherOpen = false;
          }
          detail.status = "ready";
          detail.item = item;
          detail.error = null;
          detail.measuresPending = false;
          detail.measuresError = null;
          emit();
          return { ok: true };
        }
        found = { type: "unavailable", text: TEXT.unexpected, correlationId: null };
      }
      // The confirmed choice and its conversion are left exactly as they were; the
      // person sees that this one failed and may ask for it again.
      favorites.sourceGlassPending = null;
      if (detail.measuresPending === true) {
        detail.measuresPending = false;
      }
      favorites.sourceGlassError = {
        value: checked,
        text: TEXT.sourceGlassFailed,
        correlationId: found.correlationId || null,
      };
      emit();
      return { ok: false, reason: "failed" };
    }

    // «Не знаю — оставить исходное количество без пересчета»: the safe answer (OD-10). It
    // closes the question for this view, drops any temporary clarification the view held,
    // and reads the recipe again as its own text declares it, so no hint is drawn from a
    // capacity nobody confirmed. Nothing is guessed, nothing is stored, and the recipe is
    // readable throughout -- a failed re-read is the same worded retry the other reads have.
    async function chooseUnknownSourceGlass() {
      if (state.screen !== "app" || state.gate || state.favorites.currentId === null) {
        return { ok: false, reason: "not_ready" };
      }
      if (state.favorites.editor !== null) {
        return { ok: false, reason: "editing" };
      }
      var favorites = state.favorites;
      var id = favorites.currentId;
      var detail = favorites.detail;
      if (detail === null || detail.id !== id || detail.status !== "ready") {
        return { ok: false, reason: "not_ready" };
      }
      if (favorites.sourceGlassUnknown === true) {
        return { ok: true, reason: "unchanged" };
      }
      var confirmed = favorites.sourceGlass;
      var block = detail.item ? detail.item.measures : null;
      // «Не знаю» is an unconditional state change of this view: every detail read that is
      // still out -- a pending capacity choice included, whether or not any capacity was
      // ever confirmed -- is invalidated here, so no late answer can put a capacity back,
      // replace the recipe or draw a hint from it. The counter is the only authority.
      favorites.detailReadSeq += 1;
      favorites.sourceGlassUnknown = true;
      // The temporary clarification of this view is cleared, whether or not the server can
      // be asked again: what the page draws never depends on a capacity that was dropped.
      favorites.sourceGlass = null;
      favorites.sourceGlassPending = null;
      favorites.sourceGlassError = null;
      favorites.sourceGlassOtherOpen = false;
      emit();
      // A block that was read with a clarification is stale as a whole: ask again for the
      // recipe as its own text declares it. With nothing confirmed and nothing echoed there
      // is nothing to clear on the server, and the local state is already the safe one.
      if (confirmed !== null || (block !== null && block.sourceGlassMl !== null)) {
        await refreshFavoriteConversion(id, null);
      }
      return { ok: true };
    }

    // Opening or closing the custom capacity of the question. It confirms nothing: the
    // confirmed choice and its block stay exactly as they are, and the field inside is a
    // draft until "Применить" has the server's answer for it. Returns the state the
    // control is now in, so the page can put the keyboard where it belongs.
    function toggleSourceGlassOther() {
      var favorites = state.favorites;
      if (
        state.screen !== "app" ||
        state.gate ||
        favorites.currentId === null ||
        favorites.editor !== null
      ) {
        return { open: favorites.sourceGlassOtherOpen === true, changed: false };
      }
      favorites.sourceGlassOtherOpen = !favorites.sourceGlassOtherOpen;
      emit();
      return { open: favorites.sourceGlassOtherOpen, changed: true };
    }

    // What is in the custom field while it has not been applied, exactly as it was typed.
    // Like the editor's draft this redraws nothing (the person may be typing) and sends
    // nothing: the field is the person's own work, not a choice.
    function syncSourceGlassDraft(value) {
      var favorites = state.favorites;
      if (favorites.currentId === null || favorites.editor !== null) {
        return false;
      }
      favorites.sourceGlassDraft =
        typeof value === "string" ? value : value === null || value === undefined ? "" : String(value);
      return true;
    }

    // Keep an answer of the assistant, or (with a position) one of its structured recipes.
    // The server copies from the answer it finds under this conversation and message, and
    // keeps one favorite per answer, and one per recipe: asking again (after an unclear
    // outcome, or from another window) is harmless.
    async function keepFavorite(conversationId, messageId, recipeIndex) {
      if (state.screen !== "app" || state.gate) {
        return { ok: false, reason: "not_ready" };
      }
      if (typeof conversationId !== "string" || !UUID_PATTERN.test(conversationId) || !isCount(messageId) || messageId < 1) {
        return { ok: false, reason: "invalid" };
      }
      var structured = recipeIndex !== undefined && recipeIndex !== null;
      if (structured && (!isCount(recipeIndex) || recipeIndex < 0 || recipeIndex >= MAX_RECIPES_PER_MESSAGE)) {
        return { ok: false, reason: "invalid" };
      }
      var favorites = state.favorites;
      var key = structured ? messageId + ":" + recipeIndex : String(messageId);
      if (structured ? structuredFavoriteOf(messageId, recipeIndex) !== null : favoriteOfMessage(messageId) !== null) {
        return { ok: true, reason: "already" };
      }
      var asked = favorites.saving[key];
      if (asked && asked.phase === "saving") {
        return { ok: false, reason: "busy" };
      }
      var mine = epoch;
      favorites.saving[key] = { phase: "saving", error: null };
      emit();
      var body = { conversation_id: conversationId, message_id: messageId };
      if (structured) {
        body.recipe_index = recipeIndex;
      }
      var response = await http("POST", "/api/favorites", {
        json: body,
        timeoutMs: READ_TIMEOUT_MS,
      });
      if (mine !== epoch) {
        return { ok: false, reason: "stale" };
      }
      favorites = state.favorites;
      var outcome = classifyFavoriteKeep(response);
      if (outcome.type === "auth") {
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (outcome.type === "kept") {
        delete favorites.saving[key];
        putFavorite(outcome.favorite);
        announce(TEXT.favoriteKeptAnnounce);
        emit();
        return { ok: true, reason: outcome.created ? "created" : "already" };
      }
      favorites.saving[key] = {
        phase: "error",
        error: {
          text: outcome.text || (structured ? TEXT.recipeSaveFailed : TEXT.favoriteKeepFailed),
          correlationId: outcome.correlationId || null,
        },
      };
      emit();
      return { ok: false, reason: outcome.type };
    }

    // --- editing a recipe: the server stores it, no model is called ---------------------------

    function draftOf(item) {
      return {
        title: item.title,
        text: item.kind === "text" ? item.text : "",
        notes: item.notes || "",
        servings: item.servings || "",
        ingredients: item.kind === "structured"
          ? item.ingredients.map(function (ingredient) {
              return { name: ingredient.name, amount: ingredient.amount };
            })
          : [],
        steps: item.kind === "structured" ? item.steps.slice() : [],
      };
    }

    // The draft as the editor holds it, every field exactly as it was written. Nothing is
    // shortened or dropped here: a field over the server's bound is reported when the person
    // asks to save (draftProblem), and the whole draft stays as it is until then. Fields the
    // caller did not name fall back to the draft it had; nothing is invented for a text
    // favorite (its structured fields stay empty).
    function draftFrom(raw, fallback) {
      raw = isObject(raw) ? raw : {};
      fallback = fallback || {};
      function text(name) {
        var value = typeof raw[name] === "string" ? raw[name] : fallback[name];
        return typeof value === "string" ? value : "";
      }
      var ingredients = Array.isArray(raw.ingredients) ? raw.ingredients : fallback.ingredients || [];
      var steps = Array.isArray(raw.steps) ? raw.steps : fallback.steps || [];
      var draft = {
        title: text("title"),
        text: text("text"),
        notes: text("notes"),
        servings: text("servings"),
        ingredients: [],
        steps: [],
      };
      for (var i = 0; i < ingredients.length; i++) {
        var ingredient = isObject(ingredients[i]) ? ingredients[i] : {};
        draft.ingredients.push({
          name: typeof ingredient.name === "string" ? ingredient.name : "",
          amount: typeof ingredient.amount === "string" ? ingredient.amount : "",
        });
      }
      for (var j = 0; j < steps.length; j++) {
        draft.steps.push(typeof steps[j] === "string" ? steps[j] : "");
      }
      return draft;
    }

    // The first thing that would make the server refuse the edit, in the page's words. Lengths
    // are counted in Unicode code points (as Python's len() does), never in JavaScript's
    // UTF-16 .length, so a field of emoji is judged by the same number as on the server.
    function draftProblem(draft, kind) {
      var title = draft.title.trim();
      if (!title) {
        return TEXT.editTitleRequired;
      }
      if (countCodePoints(title) > MAX_EDIT_TITLE) {
        return TEXT.editTitleTooLong;
      }
      if (countCodePoints(draft.notes) > MAX_EDIT_NOTES) {
        return TEXT.editNotesTooLong;
      }
      if (kind === "text") {
        if (!draft.text.trim()) {
          return TEXT.editTextRequired;
        }
        if (countCodePoints(draft.text) > MAX_EDIT_TEXT) {
          return TEXT.editTextTooLong;
        }
        return null;
      }
      if (countCodePoints(draft.servings.trim()) > MAX_EDIT_SERVINGS) {
        return TEXT.editServingsTooLong;
      }
      if (draft.ingredients.length === 0) {
        return TEXT.editIngredientsRequired;
      }
      if (draft.ingredients.length > MAX_EDIT_INGREDIENTS) {
        return TEXT.editTooManyIngredients;
      }
      for (var i = 0; i < draft.ingredients.length; i++) {
        var name = draft.ingredients[i].name.trim();
        var amount = draft.ingredients[i].amount.trim();
        if (!name || !amount) {
          return TEXT.editIngredientIncomplete;
        }
        if (countCodePoints(name) > MAX_EDIT_INGREDIENT_NAME) {
          return TEXT.editIngredientNameTooLong;
        }
        if (countCodePoints(amount) > MAX_EDIT_AMOUNT) {
          return TEXT.editIngredientAmountTooLong;
        }
      }
      if (draft.steps.length === 0) {
        return TEXT.editStepsRequired;
      }
      if (draft.steps.length > MAX_EDIT_STEPS) {
        return TEXT.editTooManySteps;
      }
      for (var j = 0; j < draft.steps.length; j++) {
        var step = draft.steps[j].trim();
        if (!step) {
          return TEXT.editStepEmpty;
        }
        if (countCodePoints(step) > MAX_EDIT_STEP) {
          return TEXT.editStepTooLong;
        }
      }
      // The protocol bounds one recipe block (the fields as JSON) at 20,000 characters;
      // the server would refuse a larger edit with a less specific message.
      var block = JSON.stringify({
        title: draft.title.trim(),
        servings: draft.servings.trim() ? draft.servings.trim() : null,
        ingredients: draft.ingredients.map(function (ingredient) {
          return { name: ingredient.name.trim(), amount: ingredient.amount.trim() };
        }),
        steps: draft.steps.map(function (step) {
          return step.trim();
        }),
      });
      if (countCodePoints(block) > 20000) {
        return TEXT.editTooLong;
      }
      return null;
    }

    // What PATCH names: the version the draft started from and every editable field of the
    // kind. A blank optional note or servings phrase is null, as the protocol's shape has
    // it; the text of a text favorite and the notes are stored as written.
    function editBody(draft, kind, version) {
      var body = {
        expected_version: version,
        title: draft.title.trim(),
        notes: draft.notes.trim() ? draft.notes : null,
      };
      if (kind === "text") {
        body.text = draft.text;
        return body;
      }
      body.servings = draft.servings.trim() ? draft.servings.trim() : null;
      body.ingredients = draft.ingredients.map(function (ingredient) {
        return { name: ingredient.name.trim(), amount: ingredient.amount.trim() };
      });
      body.steps = draft.steps.map(function (step) {
        return step.trim();
      });
      return body;
    }

    function currentDetailItem() {
      var favorites = state.favorites;
      var detail = favorites.detail;
      if (
        favorites.currentId !== null &&
        detail !== null &&
        detail.status === "ready" &&
        detail.item !== null &&
        detail.item.id === favorites.currentId
      ) {
        return detail.item;
      }
      return null;
    }

    function beginEditFavorite() {
      var item = currentDetailItem();
      if (state.screen !== "app" || state.gate || item === null) {
        return false;
      }
      var favorites = state.favorites;
      if (favorites.editor !== null) {
        return false;
      }
      // The draft's baseline is the recipe on the card at this moment: its identity, its
      // kind and the version it was read at. Every read of the card that is out -- a
      // background refresh, a conversion refresh, a source-glass choice -- is invalidated
      // here: its answer belongs to a view older than this draft, and letting it replace
      // the card would make a save name a version the draft was never based on. A choice
      // that was pending is dropped with its read (the confirmed one and its block stay).
      favorites.detailReadSeq += 1;
      favorites.sourceGlassPending = null;
      favorites.editor = {
        id: item.id,
        kind: item.kind,
        baseVersion: item.version,
        phase: "editing",
        draft: draftOf(item),
        error: null,
      };
      emit();
      return true;
    }

    // The draft lives here, in memory, not in the page's inputs: the page hands over
    // every change as it happens (a keystroke included), and a drawing made later --
    // after a session recheck, a return to the tab, an online event or any other
    // refresh -- is made from this draft, so an unsaved edit is never rebuilt from an
    // older one. It redraws nothing (the person may be typing) and sends nothing.
    function syncFavoriteDraft(rawDraft) {
      var editor = state.favorites.editor;
      if (editor === null || editor.phase === "saving") {
        return false;
      }
      editor.draft = draftFrom(rawDraft, editor.draft);
      return true;
    }

    // Every change of the form (a row added, removed or moved; the draft before a save)
    // passes through here; it redraws, which a keystroke must not.
    function updateFavoriteDraft(rawDraft) {
      if (!syncFavoriteDraft(rawDraft)) {
        return false;
      }
      emit();
      return true;
    }

    function cancelEditFavorite() {
      var favorites = state.favorites;
      if (favorites.editor === null) {
        return false;
      }
      var id = favorites.editor.id;
      favorites.editor = null;
      emit();
      // The conversion read that a save started was answered while the draft was open (and
      // therefore not applied): ask for it again now that the card is shown as it is.
      var detail = favorites.detail;
      if (
        detail !== null &&
        detail.id === id &&
        detail.status === "ready" &&
        detail.measuresPending === true
      ) {
        track(refreshFavoriteConversion(id, favorites.sourceGlass));
      }
      return true;
    }

    function summaryOf(item) {
      return {
        id: item.id,
        title: item.title,
        messageId: item.messageId,
        createdAt: item.createdAt,
        kind: item.kind,
        recipeIndex: item.recipeIndex,
        version: item.version,
      };
    }

    function finishEdit(item, outcome, draft, editor) {
      var favorites = state.favorites;
      if (favorites.editor !== editor) {
        return { ok: false, reason: "stale" };
      }
      if (outcome.type === "auth") {
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (outcome.type === "saved") {
        var updated = outcome.item;
        favorites.editor = null;
        // The saved answer is the newest word about this card: a read that was out
        // before it (a background refresh, an older choice) cannot replace it.
        favorites.detailReadSeq += 1;
        favorites.sourceGlassPending = null;
        var detail = favorites.detail && favorites.detail.id === updated.id ? favorites.detail : null;
        if (detail !== null) {
          detail.status = "ready";
          detail.item = updated;
          detail.error = null;
          detail.measuresPending = false;
          detail.measuresError = null;
        }
        replaceFavoriteSummary(summaryOf(updated));
        announce(TEXT.editSaved);
        if (detail !== null && favorites.sourceGlass !== null) {
          // The edit's answer was calculated without the person's clarification, so its
          // conversion is not the one that belongs to this card: it is not drawn. The
          // saved recipe stays readable while the detail is read again with the confirmed
          // choice; if that read fails, only a worded retry is shown, never an old hint.
          detail.item.measures = null;
          if (detail.item.kind === "structured") {
            detail.measuresPending = true;
          }
          emit();
          return refreshFavoriteConversion(updated.id, favorites.sourceGlass).then(function () {
            return { ok: true }; // the edit itself is saved, whatever the refresh did
          });
        }
        emit();
        return { ok: true };
      }
      if (outcome.type === "gone") {
        dropFavorite(item.id);
        favorites.editor = null;
        if (favorites.currentId === item.id) {
          favorites.detail = {
            id: item.id,
            status: "gone",
            item: null,
            error: { text: TEXT.recipeGone, correlationId: null },
          };
        }
        announce(TEXT.recipeGone);
        emit();
        return { ok: false, reason: "gone" };
      }
      if (outcome.type === "conflict") {
        editor.phase = "conflict";
        editor.draft = draft;
        editor.error = { text: TEXT.editConflict, correlationId: outcome.correlationId || null };
        emit();
        return { ok: false, reason: "conflict" };
      }
      editor.phase = "editing";
      editor.draft = draft;
      editor.error = {
        text: outcome.text || TEXT.editFailed,
        correlationId: outcome.correlationId || null,
      };
      emit();
      return { ok: false, reason: outcome.type };
    }

    async function saveFavoriteEdit(rawDraft) {
      var favorites = state.favorites;
      var editor = favorites.editor;
      var item = currentDetailItem();
      if (
        state.screen !== "app" ||
        state.gate ||
        editor === null ||
        item === null ||
        editor.id !== item.id ||
        editor.phase === "saving"
      ) {
        return { ok: false, reason: "not_ready" };
      }
      var draft = draftFrom(rawDraft, editor.draft);
      editor.draft = draft;
      // The save names the version this draft started from -- never one a read may have
      // brought to the card while it was open. If the recipe changed meanwhile, the server
      // refuses it as a conflict and the draft stays; nothing is silently overwritten.
      var problem = draftProblem(draft, editor.kind);
      if (problem !== null) {
        editor.phase = "editing";
        editor.error = { text: problem, correlationId: null };
        emit();
        return { ok: false, reason: "invalid" };
      }
      var mine = epoch;
      editor.phase = "saving";
      editor.error = null;
      emit();
      var response = await http("PATCH", "/api/favorites/" + editor.id, {
        json: editBody(draft, editor.kind, editor.baseVersion),
        timeoutMs: READ_TIMEOUT_MS,
      });
      if (mine !== epoch) {
        return { ok: false, reason: "stale" };
      }
      return finishEdit({ id: editor.id }, classifyFavoriteEdit(response, editor.id), draft, editor);
    }

    // "Загрузить заново": the draft is given up and the last stored version is read.
    function reloadFavoriteAfterConflict() {
      var editor = state.favorites.editor;
      if (editor === null) {
        return Promise.resolve(false);
      }
      var id = editor.id;
      state.favorites.editor = null;
      emit();
      // The same open card is read again: its clarification belongs to this view and stays.
      return openFavorite(id, true);
    }

    // "Сохранить поверх": the person has explicitly chosen to overwrite. The last stored
    // version is read first, so the PATCH names the version that is current now; if it
    // changed again in between, the server answers 409 and the choice is shown again.
    async function overwriteFavorite(rawDraft) {
      var favorites = state.favorites;
      var editor = favorites.editor;
      var item = currentDetailItem();
      if (
        state.screen !== "app" ||
        state.gate ||
        editor === null ||
        item === null ||
        editor.id !== item.id ||
        editor.phase !== "conflict"
      ) {
        return { ok: false, reason: "not_ready" };
      }
      var mine = epoch;
      // The conflict view is editable too: the fields as they are in the page are the
      // draft that will overwrite, not the copy made when the conflict was reported.
      var draft = draftFrom(rawDraft, editor.draft);
      editor.draft = draft;
      // An over-limit field is refused here as it is on a plain save: the whole draft stays
      // on the page, in the conflict view, and nothing is sent.
      var problem = draftProblem(draft, editor.kind);
      if (problem !== null) {
        editor.phase = "conflict";
        editor.error = { text: problem, correlationId: null };
        emit();
        return { ok: false, reason: "invalid" };
      }
      var response = await http("GET", "/api/favorites/" + editor.id, { timeoutMs: READ_TIMEOUT_MS });
      if (mine !== epoch || state.favorites.editor !== editor) {
        return { ok: false, reason: "stale" };
      }
      var found = classifyRead(response, "favorite_not_found");
      if (found.type === "auth") {
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (found.type === "not_found") {
        return finishEdit({ id: editor.id }, { type: "gone" }, draft, editor);
      }
      var latest = found.type === "ok" ? normalizeFavoriteDetail(found.body, editor.id) : null;
      if (latest === null) {
        editor.phase = "conflict";
        editor.error = {
          text: found.type === "ok" ? TEXT.unexpected : found.text || TEXT.editFailed,
          correlationId: found.correlationId || null,
        };
        emit();
        return { ok: false, reason: found.type === "ok" ? "unexpected" : found.type };
      }
      editor.draft = draft;
      editor.phase = "saving";
      editor.error = null;
      emit();
      var response2 = await http("PATCH", "/api/favorites/" + editor.id, {
        json: editBody(draft, latest.kind, latest.version),
        timeoutMs: READ_TIMEOUT_MS,
      });
      if (mine !== epoch) {
        return { ok: false, reason: "stale" };
      }
      return finishEdit(latest, classifyFavoriteEdit(response2, editor.id), draft, editor);
    }

    function hasUnsavedChanges() {
      return state.favorites.editor !== null;
    }

    // Removing is asked first, for the recipe that is open, and done only on the second click.
    function askRemoveFavorite(id) {
      var favorites = state.favorites;
      if (state.screen !== "app" || state.gate || favorites.currentId !== id || !favorites.detail || favorites.detail.status !== "ready") {
        return;
      }
      if (favorites.removing && favorites.removing.phase === "removing") {
        return;
      }
      favorites.removing = { id: id, phase: "asking", error: null };
      emit();
    }

    function cancelRemoveFavorite() {
      var removing = state.favorites.removing;
      if (removing && removing.phase !== "removing") {
        state.favorites.removing = null;
        emit();
      }
    }

    async function removeFavorite() {
      var asked = state.favorites.removing;
      if (state.screen !== "app" || state.gate || !asked || asked.phase === "removing") {
        return { ok: false, reason: "not_asked" };
      }
      var mine = epoch;
      var id = asked.id;
      asked.phase = "removing";
      asked.error = null;
      emit();
      var response = await http("DELETE", "/api/favorites/" + id, { timeoutMs: READ_TIMEOUT_MS });
      if (mine !== epoch) {
        return { ok: false, reason: "stale" };
      }
      var favorites = state.favorites;
      var outcome = classifyFavoriteRemove(response);
      if (outcome.type === "auth") {
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (outcome.type === "removed") {
        dropFavorite(id);
        if (favorites.currentId === id) {
          favorites.currentId = null;
          favorites.detail = null;
        }
        if (favorites.editor !== null && favorites.editor.id === id) {
          favorites.editor = null;
        }
        if (favorites.removing === asked) {
          favorites.removing = null;
        }
        announce(TEXT.removed);
        emit();
        return { ok: true };
      }
      if (favorites.removing === asked) {
        asked.phase = "error";
        asked.error = { text: outcome.text || TEXT.removeFailed, correlationId: outcome.correlationId || null };
        emit();
      }
      return { ok: false, reason: "failed" };
    }

    // --- the shopping lists ----------------------------------------------------------------------------

    function sortShoppingSummaries(items) {
      var active = [];
      var completed = [];
      for (var i = 0; i < items.length; i++) {
        (items[i].status === "completed" ? completed : active).push(items[i]);
      }
      return active.concat(completed);
    }

    function shoppingSummaryOf(detail) {
      return {
        id: detail.id,
        title: detail.title,
        status: detail.status,
        createdAt: detail.createdAt,
        updatedAt: detail.updatedAt,
        counts: shoppingCounts(detail.items),
      };
    }

    // Every read of one list numbers itself in that list's own count. A write, a deletion or a
    // disappearance moves the count on, so an answer to an older read of that list is known to
    // be stale; other lists have their own count and are never invalidated by it.
    function shoppingDetailReadOf(id) {
      return state.shoppingDetailReads[id] || 0;
    }

    function beginShoppingDetailRead(id) {
      state.shoppingDetailReads[id] = shoppingDetailReadOf(id) + 1;
      return state.shoppingDetailReads[id];
    }

    function invalidateShoppingDetailReads(id) {
      if (typeof id === "string" && id !== "") {
        state.shoppingDetailReads[id] = shoppingDetailReadOf(id) + 1;
      }
    }

    function rememberShoppingSummary(detail) {
      var summary = shoppingSummaryOf(detail);
      var others = state.shopping.lists.filter(function (item) {
        return item.id !== summary.id;
      });
      state.shopping.lists = sortShoppingSummaries([summary].concat(others));
      if (state.shopping.status === "loading" || state.shopping.status === "error") {
        state.shopping.status = "ready"; // (a read in flight is now stale and will be discarded)
        state.shopping.error = null;
      }
      shoppingListsRead++; // a list read begun before this change may not put the old set back
    }

    function dropShoppingSummary(id) {
      state.shopping.lists = state.shopping.lists.filter(function (item) {
        return item.id !== id;
      });
      if (state.shopping.status === "loading" || state.shopping.status === "error") {
        state.shopping.status = "ready";
        state.shopping.error = null;
      }
      shoppingListsRead++;
    }

    function shoppingItemById(detail, itemId) {
      if (!detail || !detail.item) {
        return null;
      }
      for (var i = 0; i < detail.item.items.length; i++) {
        if (detail.item.items[i].id === itemId) {
          return detail.item.items[i];
        }
      }
      return null;
    }

    function currentShoppingDetail() {
      var shopping = state.shopping;
      var detail = shopping.detail;
      if (
        shopping.currentId !== null &&
        detail !== null &&
        detail.status === "ready" &&
        detail.item !== null &&
        detail.item.id === shopping.currentId
      ) {
        return detail;
      }
      return null;
    }

    function replaceShoppingItem(detail, item) {
      var items = detail.item.items;
      for (var i = 0; i < items.length; i++) {
        if (items[i].id === item.id) {
          items[i] = item;
          return;
        }
      }
      items.push(item);
      items.sort(function (left, right) {
        return left.position - right.position;
      });
    }

    // One write per list at a time. A second one is refused ("busy") instead of being
    // allowed to overlap the first -- the controls are drawn from this same guard -- and a
    // list whose write is unsettled cannot be opened again meanwhile. A write starts by
    // invalidating the reads of that list that began before it; its own answer is never
    // judged by a read's generation, so a read or a navigation can no longer swallow it.
    function shoppingMutationBusy(detail) {
      return detail !== null && (detail.mutation !== null || shoppingMutations[detail.id] === true);
    }

    function beginShoppingMutation(detail, kind) {
      if (detail === null || shoppingMutationBusy(detail)) {
        return false;
      }
      shoppingMutations[detail.id] = true;
      detail.mutation = { kind: kind };
      invalidateShoppingDetailReads(detail.id);
      return true;
    }

    function endShoppingMutation(detail) {
      if (detail !== null) {
        delete shoppingMutations[detail.id];
        detail.mutation = null;
      }
    }

    // The open state of the list a write was issued for, when it is still what the page
    // shows. A write's answer always belongs to its own list: if the person opened another
    // list meanwhile, the answer is applied to the list's own (detached) state and to its
    // summary by id, never to the other list's.
    function shoppingLiveDetail(id) {
      var detail = state.shopping.detail;
      if (detail !== null && detail.item !== null && detail.item.id === id && detail.status === "ready") {
        return detail;
      }
      return null;
    }

    function shoppingOutcomeDetail(detail) {
      var live = shoppingLiveDetail(detail.id);
      return live !== null ? live : detail;
    }

    function applyShoppingItem(detail, server) {
      var target = shoppingOutcomeDetail(detail);
      replaceShoppingItem(target, server);
      target.item.updatedAt = server.updatedAt;
      rememberShoppingSummary(target.item);
    }

    function removeShoppingItem(detail, itemId) {
      var target = shoppingOutcomeDetail(detail);
      target.item.items = target.item.items.filter(function (entry) {
        return entry.id !== itemId;
      });
      rememberShoppingSummary(target.item);
    }

    function applyShoppingDetail(detail, found) {
      detail.status = "ready";
      detail.item = found;
      detail.error = null;
      if (detail.removing && !shoppingItemById(detail, detail.removing.id)) {
        detail.removing = null;
      }
      if (detail.editor && !shoppingItemById(detail, detail.editor.id)) {
        detail.editor = null;
      }
    }

    function shoppingDetailGone(id) {
      dropShoppingSummary(id);
      if (state.shopping.currentId === id) {
        state.shopping.currentId = null;
        state.shopping.detail = null;
      }
      state.shopping.notice = { kind: "error", text: TEXT.shoppingListGone };
      invalidateShoppingDetailReads(id);
      emit();
    }

    function shoppingDraftBusy() {
      var draft = state.shopping.draft;
      return Boolean(draft && (draft.phase === "saving" || draft.phase === "uncertain"));
    }

    // The phases in which the draft still holds work that would be lost with the page: it can
    // still be saved (ready, rejected, reused), its save is out (saving), or its save ended
    // without an answer and is retried with the same key (uncertain). A terminal draft (its
    // list was deleted elsewhere, or its recipe could not be read) holds nothing a save could
    // still make.
    function shoppingDraftUnsaved(phase) {
      return (
        phase === "ready" ||
        phase === "rejected" ||
        phase === "reused" ||
        phase === "saving" ||
        phase === "uncertain"
      );
    }

    // A draft is the person's unsaved choice of what is already at home. Only a marked
    // ingredient is a modification: an untouched draft may be left without a question, unless
    // its save is out or its answer was lost. A save that is out, or whose answer was lost, is
    // unsaved work even with nothing marked: leaving then would take the key and the frozen
    // payload that are the only way to learn whether the list was made. A confirmed save (the
    // draft is gone) and an explicit discard (the draft is gone) end it.
    function shoppingDraftDirty() {
      var draft = state.shopping.draft;
      if (draft === null || !shoppingDraftUnsaved(draft.phase)) {
        return false;
      }
      if (shoppingDraftBusy()) {
        return true; // a save is out, or its answer was lost: that work is not only its marks
      }
      for (var i = 0; i < draft.atHome.length; i++) {
        if (draft.atHome[i] === true) {
          return true;
        }
      }
      return false;
    }

    // The saved lists, read whenever the person returns to «Покупки».
    async function loadShoppingLists() {
      var shopping = state.shopping;
      if (state.screen !== "app" || state.gate || shopping.status === "loading") {
        return false;
      }
      var mine = epoch;
      var read = ++shoppingListsRead;
      shopping.status = "loading";
      shopping.error = null;
      emit();
      var response = await http("GET", "/api/shopping-lists", { timeoutMs: READ_TIMEOUT_MS });
      if (mine !== epoch || read !== shoppingListsRead) {
        return false;
      }
      var found = classifyRead(response, "shopping_list_not_found");
      if (found.type === "ok") {
        var items = normalizeShoppingLists(found.body);
        if (items !== null) {
          shopping.lists = sortShoppingSummaries(items);
          shopping.status = "ready";
          shopping.error = null;
          var open = shopping.currentId;
          if (
            open !== null &&
            shopping.detail !== null &&
            shopping.detail.status === "ready" &&
            shopping.detail.mutation === null &&
            !items.some(function (item) {
              return item.id === open;
            })
          ) {
            shoppingDetailGone(open); // removed in another tab or on another device
            return true;
          }
          emit();
          return true;
        }
        found = { type: "unavailable", text: TEXT.unexpected, correlationId: null };
      }
      if (found.type === "auth") {
        handleAuthLost();
        return false;
      }
      shopping.status = "error";
      shopping.error = { text: found.text || TEXT.shoppingFailed, correlationId: found.correlationId || null };
      emit();
      return false;
    }

    async function openShoppingList(rawId) {
      var id = typeof rawId === "string" ? rawId.toLowerCase() : "";
      if (state.screen !== "app" || state.gate || !UUID_PATTERN.test(id)) {
        return { ok: false, reason: "invalid" };
      }
      if (shoppingDraftBusy()) {
        return { ok: false, reason: "busy" };
      }
      if (shoppingMutations[id] === true) {
        return { ok: false, reason: "busy" }; // a write of this list is not settled yet
      }
      var mine = epoch;
      var shopping = state.shopping;
      state.section = "shopping";
      shopping.draft = null; // the person chose a saved list instead of the draft: no list was made
      shopping.notice = null;
      shopping.currentId = id;
      shopping.detail = freshShoppingDetail(id);
      var detail = shopping.detail;
      var read = beginShoppingDetailRead(id);
      emit();
      var response = await http("GET", "/api/shopping-lists/" + id, { timeoutMs: READ_TIMEOUT_MS });
      if (mine !== epoch || shopping.detail !== detail || shoppingDetailReadOf(id) !== read) {
        return { ok: false, reason: "stale" };
      }
      var found = classifyRead(response, "shopping_list_not_found");
      if (found.type === "ok") {
        var item = normalizeShoppingDetail(found.body);
        if (item !== null && item.id === id) {
          applyShoppingDetail(detail, item);
          rememberShoppingSummary(item);
          emit();
          return { ok: true };
        }
        found = { type: "unavailable", text: TEXT.unexpected, correlationId: null };
      }
      if (found.type === "auth") {
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (found.type === "not_found") {
        shoppingDetailGone(id);
        return { ok: false, reason: "gone" };
      }
      detail.status = "error";
      detail.error = { text: found.text || TEXT.shoppingListFailed, correlationId: found.correlationId || null };
      emit();
      return { ok: false, reason: "failed" };
    }

    function closeShoppingList() {
      invalidateShoppingDetailReads(state.shopping.currentId);
      state.shopping.currentId = null;
      state.shopping.detail = null;
      state.shopping.notice = null;
      emit();
      return true;
    }

    function reloadShoppingList() {
      var detail = state.shopping.detail;
      if (!detail) {
        return Promise.resolve(false);
      }
      return openShoppingList(detail.id);
    }

    // The open list read again in the background, without putting it into "loading": a list
    // changed on another device shows its current items when the person returns to it. A draft,
    // an inline edit, a question or a write that is under way is never overwritten.
    async function refreshOpenShoppingList() {
      var shopping = state.shopping;
      var detail = shopping.detail;
      var id = shopping.currentId;
      if (
        id === null ||
        detail === null ||
        detail.status !== "ready" ||
        shopping.draft !== null ||
        detail.editor !== null ||
        detail.adding !== null ||
        detail.removing !== null ||
        detail.deleting !== null ||
        detail.statusBusy ||
        detail.mutation !== null
      ) {
        return false;
      }
      var mine = epoch;
      var read = beginShoppingDetailRead(id);
      var response = await http("GET", "/api/shopping-lists/" + id, { timeoutMs: READ_TIMEOUT_MS });
      if (mine !== epoch || state.shopping.detail !== detail || shoppingDetailReadOf(id) !== read) {
        return false;
      }
      var found = classifyRead(response, "shopping_list_not_found");
      if (found.type === "ok") {
        var item = normalizeShoppingDetail(found.body);
        if (item !== null && item.id === id) {
          applyShoppingDetail(detail, item);
          rememberShoppingSummary(item);
          emit();
          return true;
        }
        return false;
      }
      if (found.type === "auth") {
        handleAuthLost();
        return false;
      }
      if (found.type === "not_found") {
        shoppingDetailGone(id);
        return false;
      }
      return false; // a failed background read is not an error shown over a readable list
    }

    // The server's current state of the open list, read after an unclear write. It never puts
    // the list into "loading" and never touches a draft.
    async function reconcileShoppingDetail(detail) {
      if (!detail || state.shopping.detail !== detail || detail.status !== "ready") {
        return false;
      }
      var id = detail.id;
      var mine = epoch;
      var read = beginShoppingDetailRead(id);
      var response = await http("GET", "/api/shopping-lists/" + id, { timeoutMs: READ_TIMEOUT_MS });
      if (mine !== epoch || state.shopping.detail !== detail || shoppingDetailReadOf(id) !== read) {
        return false;
      }
      var found = classifyRead(response, "shopping_list_not_found");
      if (found.type === "ok") {
        var item = normalizeShoppingDetail(found.body);
        if (item !== null && item.id === id) {
          applyShoppingDetail(detail, item);
          rememberShoppingSummary(item);
          emit();
          return true;
        }
        return false;
      }
      if (found.type === "auth") {
        handleAuthLost();
        return false;
      }
      if (found.type === "not_found") {
        shoppingDetailGone(id);
        return false;
      }
      return false;
    }

    // --- the draft of a recipe -------------------------------------------------------------------

    function draftIngredients(recipe) {
      return recipe.ingredients.map(function (ingredient) {
        return { name: ingredient.name, amount: ingredient.amount };
      });
    }

    function beginDraft(source, originSection, title, servings, ingredients) {
      var draft = {
        phase: "ready",
        originSection: originSection,
        source: source,
        title: title,
        servings: servings || "",
        ingredients: ingredients,
        atHome: ingredients.map(function () {
          return false;
        }),
        key: null,
        payload: null,
        error: null,
        code: null,
      };
      state.section = "shopping";
      state.shopping.draft = draft;
      invalidateShoppingDetailReads(state.shopping.currentId);
      state.shopping.currentId = null;
      state.shopping.detail = null;
      state.shopping.notice = null;
      emit();
      return draft;
    }

    async function readDraftRecipe(draft) {
      var source = draft.source;
      var mine = epoch;
      draft.phase = "loading";
      draft.error = null;
      emit();
      var response = await http(
        "GET",
        "/api/conversations/" + source.conversationId + "/messages/" + source.messageId + "/recipes/" + source.recipeIndex,
        { timeoutMs: READ_TIMEOUT_MS }
      );
      if (mine !== epoch || state.shopping.draft !== draft) {
        return { ok: false, reason: "stale" };
      }
      var found = classifyRecipeRead(response);
      if (found.type === "ok") {
        var recipe = normalizeRecipeRead(found.body, source.conversationId, source.messageId, source.recipeIndex);
        if (recipe !== null) {
          draft.title = recipe.title;
          draft.servings = recipe.servings;
          draft.ingredients = draftIngredients(recipe);
          draft.atHome = draft.ingredients.map(function () {
            return false;
          });
          draft.phase = "ready";
          draft.error = null;
          emit();
          return { ok: true };
        }
        found = { type: "unavailable", text: TEXT.unexpected, correlationId: null };
      }
      if (found.type === "auth") {
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (found.type === "gone") {
        draft.phase = "load_gone";
        draft.error = { text: found.text || TEXT.shoppingDraftFailed, correlationId: found.correlationId || null };
        emit();
        return { ok: false, reason: "gone" };
      }
      draft.phase = "load_error";
      draft.error = { text: found.text || TEXT.shoppingDraftFailed, correlationId: found.correlationId || null };
      emit();
      return { ok: false, reason: "failed" };
    }

    // «Что купить?» under a structured recipe of an answer: the recipe is read from the stored
    // rows it was rendered from, never re-parsed from the prose.
    async function beginShoppingDraftFromRecipe(conversationId, messageId, recipeIndex) {
      if (state.screen !== "app" || state.gate) {
        return { ok: false, reason: "not_ready" };
      }
      if (
        typeof conversationId !== "string" ||
        !UUID_PATTERN.test(conversationId) ||
        !isCount(messageId) ||
        messageId < 1 ||
        !isCount(recipeIndex) ||
        recipeIndex < 0 ||
        recipeIndex >= MAX_RECIPES_PER_MESSAGE
      ) {
        return { ok: false, reason: "invalid" };
      }
      if (shoppingDraftBusy()) {
        return { ok: false, reason: "busy" };
      }
      var origin = state.section === "favorites" ? "favorites" : "chats";
      var draft = beginDraft(
        {
          kind: "message",
          conversationId: conversationId.toLowerCase(),
          messageId: messageId,
          recipeIndex: recipeIndex,
        },
        origin,
        "",
        "",
        []
      );
      return readDraftRecipe(draft);
    }

    // «Что купить?» inside a structured recipe opened from Favorites: the detail that is on the
    // screen is the source, and its version is sent with the creation.
    function beginShoppingDraftFromFavorite() {
      var item = currentDetailItem();
      if (state.screen !== "app" || state.gate || item === null || item.kind !== "structured") {
        return { ok: false, reason: "not_ready" };
      }
      if (state.favorites.editor !== null) {
        return { ok: false, reason: "editing" };
      }
      if (shoppingDraftBusy()) {
        return { ok: false, reason: "busy" };
      }
      beginDraft(
        { kind: "favorite", favoriteId: item.id, expectedVersion: item.version },
        "favorites",
        item.title,
        item.servings,
        draftIngredients(item)
      );
      return { ok: true };
    }

    function reloadShoppingDraft() {
      var draft = state.shopping.draft;
      if (draft === null || draft.source.kind !== "message" || draft.phase !== "load_error") {
        return Promise.resolve({ ok: false, reason: "not_ready" });
      }
      return readDraftRecipe(draft);
    }

    function toggleShoppingDraftAtHome(position, atHome) {
      var draft = state.shopping.draft;
      if (
        draft === null ||
        (draft.phase !== "ready" && draft.phase !== "rejected" && draft.phase !== "reused") ||
        !isCount(position) ||
        position < 0 ||
        position >= draft.ingredients.length
      ) {
        return false;
      }
      draft.atHome[position] = atHome === true;
      emit();
      return true;
    }

    function cancelShoppingDraft() {
      var draft = state.shopping.draft;
      if (draft === null) {
        return { ok: false, reason: "none" };
      }
      if (draft.phase === "saving") {
        return { ok: false, reason: "busy" };
      }
      var origin = draft.originSection;
      state.shopping.draft = null;
      state.section = origin;
      emit();
      return { ok: true, origin: origin };
    }

    function creationPayload(draft) {
      var states = [];
      for (var position = 0; position < draft.ingredients.length; position++) {
        if (draft.atHome[position] === true) {
          states.push({ position: position, state: "at_home" });
        }
      }
      if (draft.source.kind === "message") {
        return {
          source: "message",
          conversation_id: draft.source.conversationId,
          message_id: draft.source.messageId,
          recipe_index: draft.source.recipeIndex,
          item_states: states,
        };
      }
      return {
        source: "favorite",
        favorite_id: draft.source.favoriteId,
        expected_version: draft.source.expectedVersion,
        item_states: states,
      };
    }

    function uncertainDraftText(draft, reason) {
      if (reason === "timeout") {
        return TEXT.shoppingTimeout;
      }
      if (reason === "server") {
        return TEXT.shoppingUncertainServer;
      }
      return TEXT.shoppingUncertain;
    }

    async function submitShoppingDraft(draft, key, payload) {
      var mine = epoch;
      draft.phase = "saving";
      draft.error = null;
      draft.code = null;
      emit();
      var response = await http("POST", "/api/shopping-lists", {
        headers: { "Idempotency-Key": key },
        json: payload,
        timeoutMs: READ_TIMEOUT_MS,
      });
      if (mine !== epoch || state.shopping.draft !== draft) {
        return { ok: false, reason: "stale" };
      }
      var outcome = classifyShoppingCreate(response);
      if (outcome.type === "auth") {
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (outcome.type === "saved") {
        var detail = outcome.detail;
        state.shopping.draft = null;
        state.shopping.notice = null;
        state.shopping.currentId = detail.id;
        state.shopping.detail = freshShoppingDetail(detail.id);
        state.shopping.detail.status = "ready";
        state.shopping.detail.item = detail;
        invalidateShoppingDetailReads(detail.id);
        rememberShoppingSummary(detail);
        announce(TEXT.shoppingSaved);
        emit();
        return { ok: true, id: detail.id, created: detail.created };
      }
      if (outcome.type === "reused") {
        draft.phase = "reused";
        draft.code = "shopping_list_key_reused";
        draft.error = { text: TEXT.shoppingKeyReused, correlationId: outcome.correlationId || null };
        emit();
        return { ok: false, reason: "reused" };
      }
      if (outcome.type === "deleted") {
        draft.phase = "deleted";
        draft.code = "shopping_list_deleted";
        draft.error = { text: TEXT.shoppingDeletedElsewhere, correlationId: outcome.correlationId || null };
        emit();
        return { ok: false, reason: "deleted" };
      }
      if (outcome.type === "rejected") {
        draft.phase = "rejected";
        draft.code = outcome.code || null;
        draft.error = {
          text:
            outcome.code === "favorite_conflict"
              ? TEXT.shoppingFavoriteConflict
              : outcome.text || TEXT.unexpected,
          correlationId: outcome.correlationId || null,
        };
        emit();
        return { ok: false, reason: "rejected" };
      }
      draft.phase = "uncertain";
      draft.error = {
        text: uncertainDraftText(draft, outcome.reason),
        correlationId: outcome.correlationId || null,
      };
      emit();
      return { ok: false, reason: "uncertain" };
    }

    function saveShoppingDraft() {
      var draft = state.shopping.draft;
      if (state.screen !== "app" || state.gate) {
        return Promise.resolve({ ok: false, reason: "not_ready" });
      }
      if (draft === null || draft.phase !== "ready") {
        return Promise.resolve({ ok: false, reason: "not_ready" });
      }
      var key;
      try {
        key = newIdempotencyKey(cryptoApi);
      } catch (ignored) {
        draft.error = { text: TEXT.noCrypto, correlationId: null };
        emit();
        return Promise.resolve({ ok: false, reason: "no_crypto" });
      }
      draft.key = key;
      draft.payload = creationPayload(draft);
      return submitShoppingDraft(draft, key, draft.payload);
    }

    // The same key and the same frozen payload again: the only way to learn what became of a
    // save whose answer was not received. A new list is never made twice by it.
    function retryShoppingDraft() {
      var draft = state.shopping.draft;
      if (draft === null || draft.phase !== "uncertain" || !draft.key || !draft.payload) {
        return Promise.resolve({ ok: false, reason: "not_ready" });
      }
      return submitShoppingDraft(draft, draft.key, draft.payload);
    }

    // After a refused key (or a full account), a NEW save made by the person's own click: a new
    // key and the payload as the draft is now. Nothing here happens by itself.
    function saveShoppingDraftWithNewKey() {
      var draft = state.shopping.draft;
      if (draft === null) {
        return Promise.resolve({ ok: false, reason: "not_ready" });
      }
      var allowed =
        draft.phase === "reused" || (draft.phase === "rejected" && draft.code === "shopping_lists_full");
      if (!allowed) {
        return Promise.resolve({ ok: false, reason: "not_ready" });
      }
      var key;
      try {
        key = newIdempotencyKey(cryptoApi);
      } catch (ignored) {
        draft.error = { text: TEXT.noCrypto, correlationId: null };
        emit();
        return Promise.resolve({ ok: false, reason: "no_crypto" });
      }
      draft.key = key;
      draft.payload = creationPayload(draft);
      return submitShoppingDraft(draft, key, draft.payload);
    }

    // --- the items of a saved list -----------------------------------------------------------------
    // Every write is one conditional request after the server's list lock; the page applies it
    // optimistically, and a failure either takes the change back or reconciles with what the
    // server actually has.

    function checkedItemText(rawName, rawAmount) {
      var name = trimLikeServer(typeof rawName === "string" ? rawName : "");
      var amount = trimLikeServer(typeof rawAmount === "string" ? rawAmount : "");
      if (!name) {
        return { ok: false, text: TEXT.itemRequired };
      }
      if (countCodePoints(name) > MAX_SHOPPING_NAME) {
        return { ok: false, text: TEXT.itemNameTooLong };
      }
      if (!validItemText(name)) {
        return { ok: false, text: TEXT.itemBadCharacters };
      }
      if (amount) {
        if (countCodePoints(amount) > MAX_SHOPPING_AMOUNT) {
          return { ok: false, text: TEXT.itemAmountTooLong };
        }
        if (!validItemText(amount)) {
          return { ok: false, text: TEXT.itemBadCharacters };
        }
      }
      return { ok: true, name: name, amount: amount ? amount : null };
    }

    async function setShoppingItemState(itemId, itemState) {
      var detail = currentShoppingDetail();
      if (detail === null || detail.item.status !== "active") {
        return { ok: false, reason: "not_ready" };
      }
      if (!SHOPPING_STATES[itemState]) {
        return { ok: false, reason: "invalid" };
      }
      var item = shoppingItemById(detail, itemId);
      if (item === null) {
        return { ok: false, reason: "invalid" };
      }
      if (item.pending !== undefined && item.pending !== null) {
        return { ok: false, reason: "busy" };
      }
      if (item.state === itemState) {
        return { ok: true, reason: "same" };
      }
      if (!beginShoppingMutation(detail, "state")) {
        return { ok: false, reason: "busy" };
      }
      var previous = item.state;
      var mine = epoch;
      detail.actionError = null;
      item.pending = itemState;
      item.state = itemState;
      emit();
      var response = await http(
        "PATCH",
        "/api/shopping-lists/" + detail.id + "/items/" + itemId,
        { json: { state: itemState }, timeoutMs: READ_TIMEOUT_MS }
      );
      if (mine !== epoch) {
        endShoppingMutation(detail);
        return { ok: false, reason: "stale" };
      }
      var outcome = classifyShoppingWrite(response);
      if (outcome.type === "auth") {
        endShoppingMutation(detail);
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (outcome.type === "ok") {
        var server = normalizeShoppingItem(outcome.body);
        if (server !== null && server.id === itemId) {
          applyShoppingItem(detail, server);
          announce(TEXT.itemUpdated);
          endShoppingMutation(detail);
          emit();
          return { ok: true };
        }
        outcome = { type: "uncertain", text: TEXT.unexpected, correlationId: null };
      }
      item.state = previous;
      item.pending = null;
      if (outcome.type === "not_found" && outcome.code === "shopping_list_not_found") {
        endShoppingMutation(detail);
        shoppingDetailGone(detail.id);
        return { ok: false, reason: "gone" };
      }
      detail.actionError = {
        text:
          outcome.type === "completed"
            ? LogicMessage("shopping_list_completed")
            : outcome.text || TEXT.itemStateFailed,
        correlationId: outcome.correlationId || null,
      };
      emit();
      if (outcome.type === "completed" || outcome.type === "uncertain" || outcome.type === "not_found") {
        await reconcileShoppingDetail(detail);
      }
      endShoppingMutation(detail);
      emit();
      return { ok: false, reason: outcome.type };
    }

    function LogicMessage(code) {
      return messageForCode(code) || TEXT.itemStateFailed;
    }

    function startShoppingItemEdit(itemId) {
      var detail = currentShoppingDetail();
      if (
        detail === null ||
        detail.item.status !== "active" ||
        shoppingMutationBusy(detail) ||
        detail.editor !== null ||
        detail.adding !== null
      ) {
        return false;
      }
      var item = shoppingItemById(detail, itemId);
      if (item === null) {
        return false;
      }
      detail.removing = null;
      detail.editor = {
        id: item.id,
        phase: "editing",
        draft: { name: item.name, amount: item.amount || "" },
        error: null,
      };
      detail.actionError = null;
      emit();
      return true;
    }

    function syncShoppingItemEdit(raw) {
      var detail = state.shopping.detail;
      var editor = detail ? detail.editor : null;
      if (editor === null || editor.phase === "saving") {
        return false;
      }
      raw = isObject(raw) ? raw : {};
      editor.draft = {
        name: typeof raw.name === "string" ? raw.name : editor.draft.name,
        amount: typeof raw.amount === "string" ? raw.amount : editor.draft.amount,
      };
      return true;
    }

    function cancelShoppingItemEdit() {
      var detail = state.shopping.detail;
      if (!detail || detail.editor === null || detail.editor.phase === "saving") {
        return false;
      }
      detail.editor = null;
      emit();
      return true;
    }

    async function saveShoppingItemEdit(raw) {
      var detail = currentShoppingDetail();
      var editor = detail ? detail.editor : null;
      if (detail === null || editor === null || editor.phase === "saving" || detail.item.status !== "active") {
        return { ok: false, reason: "not_ready" };
      }
      syncShoppingItemEdit(raw);
      var checked = checkedItemText(editor.draft.name, editor.draft.amount);
      if (!checked.ok) {
        editor.error = { text: checked.text, correlationId: null };
        emit();
        return { ok: false, reason: "invalid" };
      }
      if (!beginShoppingMutation(detail, "edit")) {
        return { ok: false, reason: "busy" };
      }
      var mine = epoch;
      editor.phase = "saving";
      editor.error = null;
      emit();
      var response = await http(
        "PATCH",
        "/api/shopping-lists/" + detail.id + "/items/" + editor.id,
        { json: { name: checked.name, amount: checked.amount }, timeoutMs: READ_TIMEOUT_MS }
      );
      if (mine !== epoch) {
        endShoppingMutation(detail);
        return { ok: false, reason: "stale" };
      }
      var outcome = classifyShoppingWrite(response);
      if (outcome.type === "auth") {
        endShoppingMutation(detail);
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (outcome.type === "ok") {
        var server = normalizeShoppingItem(outcome.body);
        if (server !== null && server.id === editor.id) {
          applyShoppingItem(detail, server);
          detail.editor = null;
          announce(TEXT.itemSaved);
          endShoppingMutation(detail);
          emit();
          return { ok: true, id: server.id };
        }
        outcome = { type: "uncertain", text: TEXT.unexpected, correlationId: null };
      }
      if (outcome.type === "not_found" && outcome.code === "shopping_list_not_found") {
        detail.editor = null;
        endShoppingMutation(detail);
        shoppingDetailGone(detail.id);
        return { ok: false, reason: "gone" };
      }
      editor.phase = "editing";
      editor.error = {
        text: outcome.type === "completed" ? LogicMessage("shopping_list_completed") : outcome.text || TEXT.itemEditFailed,
        correlationId: outcome.correlationId || null,
      };
      emit();
      if (outcome.type === "completed" || outcome.type === "uncertain" || outcome.type === "not_found") {
        await reconcileShoppingDetail(detail);
      }
      endShoppingMutation(detail);
      emit();
      return { ok: false, reason: outcome.type };
    }

    function beginShoppingItemAdd() {
      var detail = currentShoppingDetail();
      if (
        detail === null ||
        detail.item.status !== "active" ||
        shoppingMutationBusy(detail) ||
        detail.adding !== null ||
        detail.editor !== null ||
        detail.removing !== null ||
        detail.deleting !== null
      ) {
        return false;
      }
      detail.removing = null;
      detail.adding = { phase: "editing", name: "", amount: "", error: null };
      detail.actionError = null;
      emit();
      return true;
    }

    function syncShoppingItemAdd(raw) {
      var detail = state.shopping.detail;
      var adding = detail ? detail.adding : null;
      if (adding === null || adding.phase === "saving") {
        return false;
      }
      raw = isObject(raw) ? raw : {};
      adding.name = typeof raw.name === "string" ? raw.name : adding.name;
      adding.amount = typeof raw.amount === "string" ? raw.amount : adding.amount;
      return true;
    }

    function cancelShoppingItemAdd() {
      var detail = state.shopping.detail;
      if (!detail || detail.adding === null || detail.adding.phase === "saving") {
        return false;
      }
      detail.adding = null;
      emit();
      return true;
    }

    // The item the server now holds that was not there before this add and matches what was
    // typed: only an authoritative read that names it that way closes the form. A read that
    // does not name it proves nothing (the first request may still be on its way), so the
    // form stays and only the person's own click sends another POST.
    function shoppingAddedItem(detail, beforeIds, checked) {
      var items = detail.item === null ? [] : detail.item.items;
      for (var i = 0; i < items.length; i++) {
        var entry = items[i];
        if (beforeIds.indexOf(entry.id) !== -1) {
          continue;
        }
        if (entry.name === checked.name && entry.amount === checked.amount) {
          return entry;
        }
      }
      return null;
    }

    async function saveShoppingItemAdd(raw) {
      var detail = currentShoppingDetail();
      if (
        detail === null ||
        detail.item.status !== "active" ||
        detail.adding === null ||
        detail.adding.phase === "saving"
      ) {
        return { ok: false, reason: "not_ready" };
      }
      syncShoppingItemAdd(raw);
      var adding = detail.adding;
      var checked = checkedItemText(adding.name, adding.amount);
      if (!checked.ok) {
        adding.error = { text: checked.text, correlationId: null };
        emit();
        return { ok: false, reason: "invalid" };
      }
      if (!beginShoppingMutation(detail, "add")) {
        return { ok: false, reason: "busy" };
      }
      var beforeIds = detail.item.items.map(function (entry) {
        return entry.id;
      });
      var mine = epoch;
      adding.phase = "saving";
      adding.error = null;
      emit();
      var response = await http(
        "POST",
        "/api/shopping-lists/" + detail.id + "/items",
        { json: { name: checked.name, amount: checked.amount }, timeoutMs: READ_TIMEOUT_MS }
      );
      if (mine !== epoch) {
        endShoppingMutation(detail);
        return { ok: false, reason: "stale" };
      }
      var outcome = classifyShoppingWrite(response);
      if (outcome.type === "auth") {
        endShoppingMutation(detail);
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (outcome.type === "ok") {
        var server = normalizeShoppingItem(outcome.body);
        if (server !== null) {
          applyShoppingItem(detail, server);
          detail.adding = null;
          announce(TEXT.itemAdded);
          endShoppingMutation(detail);
          emit();
          return { ok: true, id: server.id };
        }
        outcome = { type: "uncertain", text: TEXT.unexpected, correlationId: null };
      }
      if (outcome.type === "not_found" && outcome.code === "shopping_list_not_found") {
        detail.adding = null;
        endShoppingMutation(detail);
        shoppingDetailGone(detail.id);
        return { ok: false, reason: "gone" };
      }
      if (outcome.type === "uncertain") {
        // The product may or may not have been added. The form keeps what was typed; the
        // list is read again, and only if that answer names the new item does the form close.
        // Nothing is sent again by itself: a second POST is the person's explicit choice.
        adding.phase = "uncertain";
        adding.error = { text: TEXT.addItemUncertain, correlationId: outcome.correlationId || null };
        emit();
        await reconcileShoppingDetail(detail);
        var added = shoppingAddedItem(detail, beforeIds, checked);
        if (added !== null) {
          detail.adding = null;
          announce(TEXT.itemAdded);
          endShoppingMutation(detail);
          emit();
          return { ok: true, id: added.id, reconciled: true };
        }
        endShoppingMutation(detail);
        emit();
        return { ok: false, reason: "uncertain" };
      }
      adding.phase = "editing";
      adding.error = {
        text: outcome.type === "completed" ? LogicMessage("shopping_list_completed") : outcome.text || TEXT.addItemFailed,
        correlationId: outcome.correlationId || null,
      };
      emit();
      if (outcome.type === "completed" || outcome.type === "not_found") {
        await reconcileShoppingDetail(detail);
      }
      endShoppingMutation(detail);
      emit();
      return { ok: false, reason: outcome.type };
    }

    function askDeleteShoppingItem(itemId) {
      var detail = currentShoppingDetail();
      if (detail === null || detail.item.status !== "active" || shoppingMutationBusy(detail)) {
        return false;
      }
      if (detail.removing !== null && detail.removing.phase === "deleting") {
        return false;
      }
      if (shoppingItemById(detail, itemId) === null) {
        return false;
      }
      detail.editor = null;
      detail.removing = { id: itemId, phase: "asking", error: null };
      detail.actionError = null;
      emit();
      return true;
    }

    function cancelDeleteShoppingItem() {
      var detail = state.shopping.detail;
      if (!detail || detail.removing === null || detail.removing.phase === "deleting") {
        return false;
      }
      detail.removing = null;
      emit();
      return true;
    }

    async function deleteShoppingItem() {
      var detail = currentShoppingDetail();
      var removing = detail ? detail.removing : null;
      if (detail === null || removing === null || removing.phase === "deleting") {
        return { ok: false, reason: "not_asked" };
      }
      var item = shoppingItemById(detail, removing.id);
      if (item === null) {
        detail.removing = null;
        emit();
        return { ok: false, reason: "invalid" };
      }
      if (!beginShoppingMutation(detail, "remove")) {
        return { ok: false, reason: "busy" };
      }
      var mine = epoch;
      removing.phase = "deleting";
      removing.error = null;
      emit();
      var response = await http(
        "DELETE",
        "/api/shopping-lists/" + detail.id + "/items/" + removing.id,
        { timeoutMs: READ_TIMEOUT_MS }
      );
      if (mine !== epoch) {
        endShoppingMutation(detail);
        return { ok: false, reason: "stale" };
      }
      var outcome = classifyShoppingWrite(response);
      if (outcome.type === "auth") {
        endShoppingMutation(detail);
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (outcome.type === "removed" || outcome.type === "not_found") {
        if (outcome.type === "not_found" && outcome.code === "shopping_list_not_found") {
          endShoppingMutation(detail);
          shoppingDetailGone(detail.id);
          return { ok: false, reason: "gone" };
        }
        removeShoppingItem(detail, removing.id);
        detail.removing = null;
        announce(TEXT.itemRemoved);
        endShoppingMutation(detail);
        emit();
        return { ok: true };
      }
      removing.phase = "error";
      removing.error = {
        text: outcome.type === "completed" ? LogicMessage("shopping_list_completed") : outcome.text || TEXT.itemRemoveFailed,
        correlationId: outcome.correlationId || null,
      };
      emit();
      if (outcome.type === "completed" || outcome.type === "uncertain") {
        await reconcileShoppingDetail(detail);
      }
      endShoppingMutation(detail);
      emit();
      return { ok: false, reason: outcome.type };
    }

    async function setShoppingListStatus(status) {
      var detail = currentShoppingDetail();
      if (
        detail === null ||
        detail.statusBusy ||
        shoppingMutationBusy(detail) ||
        detail.editor !== null ||
        // An unresolved addition (no write in flight: its POST is settled as "uncertain")
        // must not hold a completed list hostage: it keeps its own form, and the list can
        // be returned to work. Only an addition that is actually being sent blocks this.
        (detail.adding !== null && detail.adding.phase === "saving") ||
        detail.removing !== null ||
        detail.deleting !== null
      ) {
        return { ok: false, reason: "not_ready" };
      }
      if (!SHOPPING_STATUSES[status] || detail.item.status === status) {
        return { ok: false, reason: "invalid" };
      }
      if (!beginShoppingMutation(detail, "status")) {
        return { ok: false, reason: "busy" };
      }
      var mine = epoch;
      var target = detail;
      detail.statusBusy = true;
      detail.actionError = null;
      emit();
      var response = await http(
        "PATCH",
        "/api/shopping-lists/" + detail.id,
        { json: { status: status }, timeoutMs: READ_TIMEOUT_MS }
      );
      if (mine !== epoch) {
        endShoppingMutation(detail);
        return { ok: false, reason: "stale" };
      }
      detail.statusBusy = false;
      var outcome = classifyShoppingWrite(response);
      if (outcome.type === "auth") {
        endShoppingMutation(detail);
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (outcome.type === "ok") {
        var item = normalizeShoppingDetail(outcome.body);
        if (item !== null && item.id === detail.id) {
          target = shoppingOutcomeDetail(detail);
          applyShoppingDetail(target, item);
          rememberShoppingSummary(target.item);
          announce(status === "completed" ? TEXT.listCompletedNow : TEXT.listReopened);
          endShoppingMutation(detail);
          emit();
          return { ok: true };
        }
        outcome = { type: "uncertain", text: TEXT.unexpected, correlationId: null };
      }
      if (outcome.type === "not_found") {
        endShoppingMutation(detail);
        shoppingDetailGone(detail.id);
        return { ok: false, reason: "gone" };
      }
      target = shoppingOutcomeDetail(detail);
      target.actionError = {
        text:
          status === "completed"
            ? TEXT.completeListFailed
            : TEXT.reopenListFailed,
        correlationId: outcome.correlationId || null,
      };
      emit();
      if (outcome.type === "uncertain") {
        await reconcileShoppingDetail(detail);
      }
      endShoppingMutation(detail);
      emit();
      return { ok: false, reason: outcome.type };
    }

    function askDeleteShoppingList() {
      var detail = currentShoppingDetail();
      if (
        detail === null ||
        shoppingMutationBusy(detail) ||
        detail.editor !== null ||
        detail.adding !== null ||
        detail.removing !== null
      ) {
        return false;
      }
      if (detail.deleting !== null && detail.deleting.phase === "deleting") {
        return false;
      }
      detail.deleting = { phase: "asking", error: null };
      detail.actionError = null;
      emit();
      return true;
    }

    function cancelDeleteShoppingList() {
      var detail = state.shopping.detail;
      if (!detail || detail.deleting === null || detail.deleting.phase === "deleting") {
        return false;
      }
      detail.deleting = null;
      emit();
      return true;
    }

    async function deleteShoppingList() {
      var detail = currentShoppingDetail();
      var deleting = detail ? detail.deleting : null;
      if (detail === null || deleting === null || deleting.phase === "deleting") {
        return { ok: false, reason: "not_asked" };
      }
      if (!beginShoppingMutation(detail, "delete")) {
        return { ok: false, reason: "busy" };
      }
      var mine = epoch;
      var id = detail.id;
      deleting.phase = "deleting";
      deleting.error = null;
      emit();
      var response = await http("DELETE", "/api/shopping-lists/" + id, {
        timeoutMs: READ_TIMEOUT_MS,
      });
      if (mine !== epoch) {
        endShoppingMutation(detail);
        return { ok: false, reason: "stale" };
      }
      var outcome = classifyShoppingWrite(response);
      if (outcome.type === "auth") {
        endShoppingMutation(detail);
        handleAuthLost();
        return { ok: false, reason: "auth" };
      }
      if (outcome.type === "removed" || outcome.type === "not_found") {
        // The confirmation is the server's, whatever the page has shown since: the summary
        // goes by the list's own id, and a list whose detail has been replaced meanwhile is
        // still removed (only a detail of another list is left alone).
        dropShoppingSummary(id);
        if (state.shopping.currentId === id) {
          state.shopping.currentId = null;
          state.shopping.detail = null;
          state.shopping.notice = null;
        }
        // A read of the deleted list that began before this answer cannot put it back. Only
        // that list is moved on: a read of another list that is under way -- the list the
        // person has navigated to meanwhile, say -- is not made stale by this deletion.
        invalidateShoppingDetailReads(id);
        announce(TEXT.listDeleted);
        endShoppingMutation(detail);
        emit();
        return { ok: true };
      }
      deleting.phase = "error";
      deleting.error = { text: outcome.text || TEXT.deleteListFailed, correlationId: outcome.correlationId || null };
      emit();
      if (outcome.type === "uncertain") {
        await reconcileShoppingDetail(detail);
      }
      endShoppingMutation(detail);
      emit();
      return { ok: false, reason: outcome.type };
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

    // Whether an answer is kept: {state: "none" | "saving" | "saved" | "error", id (of the favorite), error}.
    // (When the list of the favorites has not been read, an answer shows as "none": keeping it again is harmless.)
    function favoriteMark(messageId) {
      var kept = favoriteOfMessage(messageId);
      if (kept !== null) {
        return { state: "saved", id: kept.id, error: null };
      }
      var saving = state.favorites.saving[String(messageId)];
      if (saving) {
        return saving.phase === "saving"
          ? { state: "saving", id: null, error: null }
          : { state: "error", id: null, error: saving.error };
      }
      return { state: "none", id: null, error: null };
    }

    // The same, for one structured recipe of an answer: each has its own save, its own
    // state and its own favorite, and one being under way or failed does not touch another.
    function recipeMark(messageId, position) {
      var kept = structuredFavoriteOf(messageId, position);
      if (kept !== null) {
        return { state: "saved", id: kept.id, error: null };
      }
      var saving = state.favorites.saving[messageId + ":" + position];
      if (saving) {
        return saving.phase === "saving"
          ? { state: "saving", id: null, error: null }
          : { state: "error", id: null, error: saving.error };
      }
      return { state: "none", id: null, error: null };
    }

    // The favorites as the page draws them: the list (for the side), and the recipe that is open.
    function favoritesView(now) {
      var favorites = state.favorites;
      var items = favorites.items.map(function (item) {
        return {
          id: item.id,
          title: item.title,
          meta: item.createdAt === null ? "" : fill(TEXT.savedOn, { when: formatMoment(item.createdAt, now) }),
          current: item.id === favorites.currentId,
        };
      });
      var current = null;
      var detail = favorites.detail;
      if (favorites.currentId !== null && detail && detail.id === favorites.currentId) {
        var listed = items.filter(function (item) {
          return item.id === detail.id;
        })[0];
        var removing = favorites.removing && favorites.removing.id === detail.id ? favorites.removing : null;
        var editor = favorites.editor && favorites.editor.id === detail.id ? favorites.editor : null;
        current = {
          id: detail.id,
          status: detail.status,
          title: detail.item ? detail.item.title : listed ? listed.title : "",
          text: detail.item ? detail.item.text : "",
          meta: detail.item && detail.item.createdAt !== null ? fill(TEXT.savedOn, { when: formatMoment(detail.item.createdAt, now) }) : "",
          error: detail.error,
          removing: removing ? { phase: removing.phase, error: removing.error } : null,
          kind: detail.item ? detail.item.kind : "text",
          version: detail.item ? detail.item.version : null,
          notes: detail.item ? detail.item.notes : "",
          servings: detail.item ? detail.item.servings : "",
          ingredients: detail.item ? detail.item.ingredients : [],
          steps: detail.item ? detail.item.steps : [],
          measures: detail.item ? detail.item.measures : null,
          measuresPending: detail.measuresPending === true,
          measuresError: detail.measuresError || null,
          sourceGlass: favorites.sourceGlass,
          sourceGlassUnknown: favorites.sourceGlassUnknown === true,
          sourceGlassPending: favorites.sourceGlassPending,
          sourceGlassError: favorites.sourceGlassError,
          // The custom capacity is drawn as what it is: an open field with the person's
          // own draft text, never as a confirmed choice of its own.
          sourceGlassOtherOpen: favorites.sourceGlassOtherOpen === true,
          sourceGlassDraft: favorites.sourceGlassDraft,
          updatedAt: detail.item ? detail.item.updatedAt : null,
          editor: editor
            ? {
                phase: editor.phase,
                // The version the draft was opened on: a save names this one, whatever the
                // card may have shown later.
                baseVersion: editor.baseVersion,
                error: editor.error,
                draft: {
                  title: editor.draft.title,
                  text: editor.draft.text,
                  notes: editor.draft.notes,
                  servings: editor.draft.servings,
                  ingredients: editor.draft.ingredients.map(function (ingredient) {
                    return { name: ingredient.name, amount: ingredient.amount };
                  }),
                  steps: editor.draft.steps.slice(),
                },
              }
            : null,
        };
      }
      return { status: favorites.status, error: favorites.error, items: items, current: current };
    }

    // --- the shopping lists as the page draws them ---------------------------------------------------

    var SHOPPING_GROUPS = ["unchecked", "at_home", "purchased"];

    function shoppingItemView(item) {
      return {
        id: item.id,
        position: item.position,
        name: item.name,
        amount: item.amount,
        state: item.state,
        pending: item.pending || null,
      };
    }

    function shoppingDraftView(draft, now) {
      var atHome = 0;
      var ingredients = [];
      for (var i = 0; i < draft.ingredients.length; i++) {
        if (draft.atHome[i] === true) {
          atHome++;
        }
        ingredients.push({
          position: i,
          name: draft.ingredients[i].name,
          amount: draft.ingredients[i].amount,
          atHome: draft.atHome[i] === true,
        });
      }
      var editable = draft.phase === "ready" || draft.phase === "rejected" || draft.phase === "reused";
      var canSaveNew =
        draft.phase === "reused" ||
        (draft.phase === "rejected" && draft.code === "shopping_lists_full");
      return {
        phase: draft.phase,
        title: draft.title,
        servings: draft.servings,
        ingredients: ingredients,
        total: draft.ingredients.length,
        atHome: atHome,
        remaining: draft.ingredients.length - atHome,
        instruction: TEXT.shoppingDraftInstruction,
        editable: editable,
        canSave: draft.phase === "ready",
        saving: draft.phase === "saving",
        canRetry: draft.phase === "uncertain" || draft.phase === "load_error",
        retryLabel: draft.phase === "load_error" ? TEXT.retry : TEXT.shoppingRetrySave,
        canSaveNew: canSaveNew,
        saveNewLabel: draft.phase === "reused" ? TEXT.shoppingSaveNew : TEXT.shoppingSaveAgain,
        canCancel: draft.phase !== "saving",
        terminal: draft.phase === "deleted" || draft.phase === "load_gone",
        error: draft.error,
      };
    }

    function shoppingDetailView(now) {
      var shopping = state.shopping;
      var detail = shopping.detail;
      if (shopping.currentId === null || detail === null || detail.id !== shopping.currentId) {
        return null;
      }
      var item = detail.item;
      var listed = shopping.lists.filter(function (entry) {
        return entry.id === detail.id;
      })[0];
      var view = {
        id: detail.id,
        status: detail.status,
        error: detail.error,
        title: item ? item.title : listed ? listed.title : "",
        listStatus: item ? item.status : null,
        meta:
          item && item.updatedAt !== null
            ? fill(TEXT.listUpdated, { when: formatMoment(item.updatedAt, now) })
            : "",
        counts: null,
        progress: "",
        groups: [],
        itemCount: 0,
        readOnly: item ? item.status === "completed" : false,
        statusBusy: detail.statusBusy,
        // A write of this list is unsettled, reconciliation included: every conflicting
        // control is drawn from this same guard.
        busy: detail.mutation !== null,
        actionError: detail.actionError,
        editor: null,
        adding: null,
        removing: null,
        deleting: detail.deleting ? { phase: detail.deleting.phase, error: detail.deleting.error } : null,
      };
      if (item) {
        var counts = shoppingCounts(item.items);
        view.counts = counts;
        view.itemCount = counts.total;
        view.progress = fill(TEXT.progress, { purchased: counts.purchased, total: counts.total });
        SHOPPING_GROUPS.forEach(function (stateName) {
          var members = item.items.filter(function (entry) {
            return entry.state === stateName;
          });
          if (members.length) {
            view.groups.push({
              state: stateName,
              title: stateName === "unchecked" ? TEXT.toBuy : stateName === "at_home" ? TEXT.atHome : TEXT.bought,
              count: members.length,
              items: members.map(shoppingItemView),
            });
          }
        });
      }
      if (detail.editor) {
        view.editor = {
          id: detail.editor.id,
          phase: detail.editor.phase,
          error: detail.editor.error,
          draft: {
            name: detail.editor.draft.name,
            amount: detail.editor.draft.amount,
          },
        };
      }
      if (detail.adding) {
        view.adding = {
          phase: detail.adding.phase,
          error: detail.adding.error,
          draft: { name: detail.adding.name, amount: detail.adding.amount },
        };
      }
      if (detail.removing) {
        view.removing = {
          id: detail.removing.id,
          phase: detail.removing.phase,
          error: detail.removing.error,
        };
      }
      return view;
    }

    function shoppingView(now) {
      var shopping = state.shopping;
      var lists = shopping.lists.map(function (item) {
        var meta = [
          fill(TEXT.progress, { purchased: item.counts.purchased, total: item.counts.total }),
          item.status === "completed" ? TEXT.listCompleted : TEXT.listActive,
        ];
        if (item.updatedAt !== null) {
          meta.push(formatMoment(item.updatedAt, now));
        }
        return {
          id: item.id,
          title: item.title,
          meta: meta.join(" · "),
          current: item.id === shopping.currentId,
        };
      });
      var current = shoppingDetailView(now);
      var draft = shopping.draft === null ? null : shoppingDraftView(shopping.draft, now);
      var title = draft !== null ? TEXT.shoppingDraftTitle : current !== null && current.title ? current.title : TEXT.shoppingTitle;
      return {
        status: shopping.status,
        error: shopping.error,
        notice: shopping.notice,
        title: title,
        lists: lists,
        current: current,
        draft: draft,
        // The lists whose write is unsettled: their sidebar entries are not opened again
        // until the write (and its reconciliation) has settled.
        blocked: Object.keys(shoppingMutations),
        empty: shopping.status === "ready" && lists.length === 0,
      };
    }

    function profileView() {
      var profile = state.profile;
      return {
        status: profile.status,
        loaded: profile.loaded,
        error: profile.error,
        displayName: profile.displayName,
        textSize: profile.textSize,
        measures: profile.measures,
        loginMethods: profile.loginMethods.map(function (method) {
          return { provider: method.provider, address: method.address, status: method.status };
        }),
        editing: profile.editing,
        saving: profile.saving,
        notice: profile.notice,
        fieldError: profile.fieldError,
        measureError: profile.measureError,
        draft: profile.draft
          ? {
              displayName: profile.draft.displayName,
              textSize: profile.draft.textSize,
              measures: profile.draft.measures,
            }
          : null,
      };
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
          photo: message.photo || null,
          time: formatMoment(message.createdAt, now),
          iso: isoTime(message.createdAt),
          status: message.status,
          note: "",
          correlationId: null,
          canRetry: false,
        };
        if (message.role === "assistant") {
          var recipes = structuredRecipes ? message.recipes : [];
          item.recipes = recipes.map(function (recipe) {
            var mark = recipeMark(message.id, recipe.position);
            return {
              position: recipe.position,
              title: recipe.title,
              state: mark.state,
              favoriteId: mark.id,
              error: mark.error,
            };
          });
          // With the feature on, only an answer that carries validated recipes has Save
          // controls -- one per recipe -- and an ordinary answer has none at all: the
          // legacy whole-answer button is not a fallback for it. With the feature off
          // nothing changes: every answer keeps the legacy whole-answer control.
          item.favorite = structuredRecipes ? null : favoriteMark(message.id);
        }
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
        section: "chats",
        favorites: { status: "idle", error: null, items: [], current: null },
        shopping: {
          status: "idle",
          error: null,
          notice: null,
          title: TEXT.shoppingTitle,
          lists: [],
          current: null,
          draft: null,
          blocked: [],
          empty: false,
        },
        // Nothing of the profile is shown while the page does not know whose it is (and the
        // large text size comes off with it: drawScreens only applies the attribute to the app).
        profile: {
          status: "idle",
          loaded: false,
          error: null,
          displayName: null,
          textSize: "normal",
          loginMethods: [],
          saving: false,
          notice: null,
          fieldError: null,
          draft: null,
        },
        currentId: null,
        busy: false,
        maxLength: maxLength,
        chatTitle: "",
        list: { status: "idle", error: null, hasMore: false, draftCurrent: false, deleting: null, items: [] },
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
        section: state.section,
        favorites: favoritesView(now),
        shopping: shoppingView(now),
        profile: profileView(),
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
        deleting: state.deleting,
        items: state.list.items.map(function (item) {
          var turn = state.pending[item.id];
          var meta = item.updatedAt === null ? "" : formatMoment(item.updatedAt, now);
          if (turn) {
            meta = turn.phase === "sending" || turn.phase === "waiting" ? "готовится ответ…" : "ждет решения";
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
      askDeleteConversation: askDeleteConversation,
      cancelDeleteConversation: cancelDeleteConversation,
      deleteConversation: function () {
        return track(deleteConversation());
      },
      showSection: showSection,
      openFavorite: function (id) {
        return track(openFavorite(id));
      },
      reloadFavorites: function () {
        track(refreshOpenFavorite());
        return track(loadFavorites());
      },
      reloadRecipe: function () {
        return track(reloadRecipe());
      },
      setSourceGlass: function (value) {
        return track(setSourceGlass(value));
      },
      chooseUnknownSourceGlass: function () {
        return track(chooseUnknownSourceGlass());
      },
      toggleSourceGlassOther: toggleSourceGlassOther,
      syncSourceGlassDraft: syncSourceGlassDraft,
      retryFavoriteConversion: function () {
        return track(retryFavoriteConversion());
      },
      keepFavorite: function (conversationId, messageId, recipeIndex) {
        return track(keepFavorite(conversationId, messageId, recipeIndex));
      },
      beginEditFavorite: beginEditFavorite,
      syncFavoriteDraft: syncFavoriteDraft,
      updateFavoriteDraft: updateFavoriteDraft,
      cancelEditFavorite: cancelEditFavorite,
      saveFavoriteEdit: function (draft) {
        return track(saveFavoriteEdit(draft));
      },
      reloadFavoriteAfterConflict: function () {
        return track(reloadFavoriteAfterConflict());
      },
      overwriteFavorite: function (draft) {
        return track(overwriteFavorite(draft));
      },
      hasUnsavedChanges: hasUnsavedChanges,
      askRemoveFavorite: askRemoveFavorite,
      cancelRemoveFavorite: cancelRemoveFavorite,
      removeFavorite: function () {
        return track(removeFavorite());
      },
      // The profile.
      reloadProfile: function () {
        return track(loadProfile());
      },
      beginProfileEdit: beginProfileEdit,
      saveProfile: function (draft) {
        return track(saveProfile(draft));
      },
      syncProfileDraft: syncProfileDraft,
      clearProfileNotice: clearProfileNotice,
      // The shopping lists.
      reloadShoppingLists: function () {
        return track(loadShoppingLists());
      },
      beginShoppingDraftFromRecipe: function (conversationId, messageId, recipeIndex) {
        return track(beginShoppingDraftFromRecipe(conversationId, messageId, recipeIndex));
      },
      beginShoppingDraftFromFavorite: beginShoppingDraftFromFavorite,
      reloadShoppingDraft: function () {
        return track(reloadShoppingDraft());
      },
      toggleShoppingDraftAtHome: toggleShoppingDraftAtHome,
      shoppingDraftDirty: shoppingDraftDirty,
      saveShoppingDraft: function () {
        return track(saveShoppingDraft());
      },
      retryShoppingDraft: function () {
        return track(retryShoppingDraft());
      },
      saveShoppingDraftWithNewKey: function () {
        return track(saveShoppingDraftWithNewKey());
      },
      cancelShoppingDraft: cancelShoppingDraft,
      openShoppingList: function (id) {
        return track(openShoppingList(id));
      },
      closeShoppingList: closeShoppingList,
      reloadShoppingList: function () {
        return track(reloadShoppingList());
      },
      setShoppingItemState: function (itemId, state) {
        return track(setShoppingItemState(itemId, state));
      },
      startShoppingItemEdit: startShoppingItemEdit,
      syncShoppingItemEdit: syncShoppingItemEdit,
      cancelShoppingItemEdit: cancelShoppingItemEdit,
      saveShoppingItemEdit: function (draft) {
        return track(saveShoppingItemEdit(draft));
      },
      beginShoppingItemAdd: beginShoppingItemAdd,
      syncShoppingItemAdd: syncShoppingItemAdd,
      cancelShoppingItemAdd: cancelShoppingItemAdd,
      saveShoppingItemAdd: function (draft) {
        return track(saveShoppingItemAdd(draft));
      },
      askDeleteShoppingItem: askDeleteShoppingItem,
      cancelDeleteShoppingItem: cancelDeleteShoppingItem,
      deleteShoppingItem: function () {
        return track(deleteShoppingItem());
      },
      setShoppingListStatus: function (status) {
        return track(setShoppingListStatus(status));
      },
      askDeleteShoppingList: askDeleteShoppingList,
      cancelDeleteShoppingList: cancelDeleteShoppingList,
      deleteShoppingList: function () {
        return track(deleteShoppingList());
      },
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

  // --- Telegram: connecting an account -------------------------------------------------------------------------
  //
  // A small controller of its own, beside the conversations and independent of them: it reads whether the bot
  // exists here and whether this person has linked an account (GET /api/telegram), asks for a pairing code
  // (POST /api/telegram/link) and unlinks (DELETE /api/telegram/link). The code is shown once, as a link to the
  // bot and as the command to send by hand, and is kept only in memory until it is used or has expired; while it
  // is live the status is read every few seconds, so the panel changes by itself once the person has pressed Start
  // in Telegram. Nothing is stored in the browser. Who is signed in is not its business: the page starts it when it
  // shows the conversations and stops it when it stops showing them, and a late answer to a stopped panel is ignored
  // (epoch).

  var TELEGRAM_TEXT = {
    loading: "Проверяем Telegram…",
    unlinked:
      "Подключите Telegram, чтобы писать помощнику с телефона. Разговоры будут общими с сайтом: " +
      "начатое здесь можно продолжить там и наоборот.",
    // Always under the panel: what connecting does with what the person writes, and what happens to it.
    privacy:
      "После подключения все, что вы пишете помощнику на сайте, вместе с его ответами дублируется в этот " +
      "Telegram-чат (отдельно это не отключается: только отключением Telegram). Текст сообщений и фотографии " +
      "передаются в OpenAI для подготовки ответа. История разговоров хранится 365 дней и затем удаляется; " +
      "рецепты, сохраненные в избранное, остаются, пока вы сами их не уберете.",
    unavailable:
      "Бот Telegram сейчас не отвечает: подключить его и получать сообщения в Telegram пока нельзя. " +
      "Сайт работает как обычно; попробуйте позже.",
    linked: "Telegram подключен",
    linkedAs: "Подключен: {name}",
    pairing:
      "Нажмите «Открыть Telegram» и затем «Запустить» (Start). Если ссылка не открылась, " +
      "отправьте боту {bot} сообщение: /start {code}. Ссылка действует 10 минут.",
    pairedNow: "Готово: Telegram подключен.",
    expired: "Ссылка устарела. Нажмите «Подключить Telegram», чтобы получить новую.",
    unlinkedNow: "Telegram отключен.",
    askUnlink: "Отключить Telegram? Помощник перестанет писать вам туда, а бот перестанет отвечать.",
    failed: "Не удалось получить состояние Telegram.",
    connect: "Подключить Telegram",
    open: "Открыть Telegram",
    disconnect: "Отключить",
    confirmDisconnect: "Да, отключить",
    cancel: "Отмена",
  };

  var TELEGRAM_LINK = /^https:\/\/t\.me\/[A-Za-z][A-Za-z0-9_]{3,31}\?start=[A-Za-z0-9_-]{16,64}$/;
  var TELEGRAM_CODE = /^[A-Za-z0-9_-]{16,64}$/;
  var TELEGRAM_POLL_MS = 3000;
  var TELEGRAM_TIMEOUT_MS = 15000;

  function createTelegramLink(deps) {
    var nowFn = deps.now || Date.now;
    var scheduleFn = deps.setTimeout || setTimeout;
    var cancelFn = deps.clearTimeout || clearTimeout;
    var AbortCtor = deps.AbortController !== undefined
      ? deps.AbortController
      : typeof AbortController !== "undefined"
        ? AbortController
        : null;
    var changed = deps.onChange || function () {};
    var epoch = 0;
    var pollTimer = null;
    var state = blank();

    function blank() {
      return {
        phase: "off", // off | loading | unlinked | pairing | linked | error
        available: true, // false: the server says the bot is not reading Telegram just now
        bot: null,
        name: null,
        link: null,
        code: null,
        deadline: null,
        notice: null,
        busy: false,
        confirming: false,
      };
    }

    function emit() {
      changed();
    }

    function stopPolling() {
      if (pollTimer !== null) {
        cancelFn(pollTimer);
        pollTimer = null;
      }
    }

    function parse(text) {
      try {
        return { ok: true, value: JSON.parse(text) };
      } catch (ignored) {
        return { ok: false, value: null };
      }
    }

    // One call: {kind: "ok"|"error"|"network", status, body}.
    async function call(method, path, json) {
      var headers = { Accept: "application/json" };
      var init = { method: method, headers: headers, credentials: "same-origin", cache: "no-store", redirect: "error" };
      if (json !== undefined) {
        headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(json);
      }
      var timer = null;
      if (AbortCtor) {
        var aborter = new AbortCtor();
        init.signal = aborter.signal;
        timer = scheduleFn(function () {
          aborter.abort();
        }, TELEGRAM_TIMEOUT_MS);
      }
      var response;
      var text;
      try {
        response = await deps.fetch(path, init);
        text = await response.text();
      } catch (ignored) {
        if (timer !== null) {
          cancelFn(timer);
        }
        return { kind: "network", status: 0, body: null };
      }
      if (timer !== null) {
        cancelFn(timer);
      }
      var parsed = parse(text);
      var body = parsed.ok && parsed.value !== null && typeof parsed.value === "object" ? parsed.value : null;
      return { kind: response.status >= 200 && response.status < 300 ? "ok" : "error", status: response.status, body: body };
    }

    function readStatus(reply) {
      // The shape of GET /api/telegram, or null.
      var body = reply.body;
      if (reply.kind !== "ok" || body === null || typeof body.enabled !== "boolean" || typeof body.linked !== "boolean") {
        return null;
      }
      var name = null;
      if (body.linked && body.account && typeof body.account === "object" && typeof body.account.name === "string") {
        name = body.account.name;
      }
      var bot = typeof body.bot_username === "string" ? body.bot_username : null;
      return { enabled: body.enabled, available: body.available !== false, linked: body.linked, name: name, bot: bot };
    }

    function applyStatus(status, quiet) {
      if (!status.enabled) {
        stopPolling();
        state = blank();
        return;
      }
      state.bot = status.bot;
      state.available = status.available;
      if (status.linked) {
        var wasPairing = state.phase === "pairing";
        stopPolling();
        state.phase = "linked";
        state.name = status.name;
        state.link = null;
        state.code = null;
        state.deadline = null;
        state.confirming = false;
        if (wasPairing) {
          state.notice = TELEGRAM_TEXT.pairedNow;
        } else if (!quiet) {
          state.notice = null;
        }
      } else if (state.phase !== "pairing") {
        state.phase = "unlinked";
        state.name = null;
        state.confirming = false;
        if (!quiet) {
          state.notice = null;
        }
      }
    }

    function failure(reply, fallback) {
      if (reply.kind === "network") {
        return TEXT.network;
      }
      var code = reply.body && typeof reply.body.detail === "string" ? reply.body.detail : null;
      if (code !== null && Object.prototype.hasOwnProperty.call(CODE_MESSAGES, code)) {
        return CODE_MESSAGES[code];
      }
      return fallback;
    }

    // Reads the status. Called when the conversations are shown, when the page comes back, and by the poll.
    async function refresh(quiet) {
      var mine = epoch;
      if (state.phase === "off" && !quiet) {
        state.phase = "loading";
        emit();
      }
      var reply = await call("GET", "/api/telegram");
      if (mine !== epoch) {
        return;
      }
      var status = readStatus(reply);
      if (status === null) {
        if (reply.status === 401) {
          stopPolling();
          state = blank(); // the conversations deal with the end of a session; this panel simply goes
        } else if (state.phase === "pairing") {
          // (a poll that failed: the next one tries again; the code is still good)
        } else {
          stopPolling();
          state.phase = "error";
          state.notice = failure(reply, TELEGRAM_TEXT.failed);
          state.busy = false;
        }
        emit();
        return;
      }
      applyStatus(status, quiet);
      emit();
    }

    function schedulePoll() {
      stopPolling();
      var mine = epoch;
      pollTimer = scheduleFn(async function () {
        pollTimer = null;
        if (mine !== epoch || state.phase !== "pairing") {
          return;
        }
        if (state.deadline !== null && nowFn() >= state.deadline) {
          state.phase = "unlinked";
          state.link = null;
          state.code = null;
          state.deadline = null;
          state.notice = TELEGRAM_TEXT.expired;
          emit();
          return;
        }
        await refresh(true);
        if (mine === epoch && state.phase === "pairing") {
          schedulePoll();
        }
      }, TELEGRAM_POLL_MS);
    }

    async function connect() {
      if (state.busy || (state.phase !== "unlinked" && state.phase !== "error")) {
        return;
      }
      if (state.phase === "error") {
        // ("Повторить" after a failure to read the status: read it again, ask for no code yet.)
        await refresh(false);
        return;
      }
      var mine = epoch;
      state.busy = true;
      state.notice = null;
      emit();
      var reply = await call("POST", "/api/telegram/link", {});
      if (mine !== epoch) {
        return;
      }
      state.busy = false;
      var body = reply.body;
      if (
        reply.kind === "ok" &&
        body !== null &&
        typeof body.code === "string" &&
        TELEGRAM_CODE.test(body.code) &&
        typeof body.link === "string" &&
        TELEGRAM_LINK.test(body.link) &&
        typeof body.expires_in_seconds === "number" &&
        body.expires_in_seconds > 0
      ) {
        state.phase = "pairing";
        state.code = body.code;
        state.link = body.link;
        state.deadline = nowFn() + body.expires_in_seconds * 1000;
        state.notice = null;
        schedulePoll();
        emit();
        return;
      }
      if (reply.body !== null && reply.body.detail === "telegram_already_linked") {
        await refresh(false);
        return;
      }
      if (reply.status === 401) {
        state = blank();
        emit();
        return;
      }
      state.notice = failure(reply, TEXT.unexpected);
      emit();
    }

    function askDisconnect() {
      if (state.phase === "linked" && !state.busy) {
        state.confirming = true;
        emit();
      }
    }

    function cancelDisconnect() {
      if (state.confirming) {
        state.confirming = false;
        emit();
      }
    }

    async function disconnect() {
      if (state.phase !== "linked" || state.busy || !state.confirming) {
        return;
      }
      var mine = epoch;
      state.busy = true;
      emit();
      var reply = await call("DELETE", "/api/telegram/link");
      if (mine !== epoch) {
        return;
      }
      state.busy = false;
      state.confirming = false;
      if (reply.kind === "ok") {
        state.phase = "unlinked";
        state.name = null;
        state.notice = TELEGRAM_TEXT.unlinkedNow;
      } else if (reply.status === 401) {
        state = blank();
      } else {
        state.notice = failure(reply, TEXT.unexpected);
      }
      emit();
    }

    // The page shows the conversations: ask what there is to show.
    function start() {
      epoch++;
      stopPolling();
      state = blank();
      return refresh(false);
    }

    // The page stops showing the conversations (signed out, another person, the session ended): nothing of
    // this person's stays in memory, and answers still on their way change nothing.
    function stop() {
      epoch++;
      stopPolling();
      state = blank();
      emit();
    }

    // The tab is shown again: the person may have pressed Start in Telegram meanwhile.
    function recheck() {
      if (state.phase === "off" || state.busy) {
        return Promise.resolve();
      }
      return refresh(state.phase !== "error");
    }

    function getView() {
      var linked = state.phase === "linked";
      var pairing = state.phase === "pairing";
      var text = "";
      if (state.phase === "loading") {
        text = TELEGRAM_TEXT.loading;
      } else if (state.phase === "unlinked") {
        text = TELEGRAM_TEXT.unlinked;
      } else if (pairing) {
        text = fill(TELEGRAM_TEXT.pairing, { bot: "@" + (state.bot || ""), code: state.code });
      } else if (linked && state.confirming) {
        text = TELEGRAM_TEXT.askUnlink;
      } else if (linked) {
        text = state.name ? fill(TELEGRAM_TEXT.linkedAs, { name: state.name }) : TELEGRAM_TEXT.linked;
      }
      return {
        visible: state.phase !== "off",
        phase: state.phase,
        text: text,
        notice: state.available ? state.notice : TELEGRAM_TEXT.unavailable,
        privacy: state.phase === "off" || state.phase === "loading" ? "" : TELEGRAM_TEXT.privacy,
        link: pairing ? state.link : null,
        busy: state.busy,
        canConnect: (state.phase === "unlinked" || state.phase === "error") && !state.busy && state.available,
        canAskDisconnect: linked && !state.confirming && !state.busy,
        canConfirmDisconnect: linked && state.confirming && !state.busy,
        canCancel: linked && state.confirming && !state.busy,
        retry: state.phase === "error",
      };
    }

    return {
      start: start,
      stop: stop,
      recheck: recheck,
      connect: connect,
      askDisconnect: askDisconnect,
      cancelDisconnect: cancelDisconnect,
      disconnect: disconnect,
      getView: getView,
    };
  }

  return {
    MAX_MESSAGE_LENGTH: MAX_MESSAGE_LENGTH,
    MAX_PROFILE_NAME: MAX_PROFILE_NAME,
    MEASURE_LIMITS: MEASURE_LIMITS,
    MEASURE_KEYS: MEASURE_KEYS,
    formatFraction: formatFraction,
    formatMeasure: formatMeasure,
    formatMassMeasure: formatMassMeasure,
    measureGrammar: measureGrammar,
    prepareMeasure: prepareMeasure,
    prepareMeasures: prepareMeasures,
    PAGE_SIZE: PAGE_SIZE,
    STORAGE_KEY: STORAGE_KEY,
    TEXT: TEXT,
    CODE_MESSAGES: CODE_MESSAGES,
    countCodePoints: countCodePoints,
    codePointToUtf16: codePointToUtf16,
    trimLikeServer: trimLikeServer,
    hasUnstorableCharacters: hasUnstorableCharacters,
    prepareMessage: prepareMessage,
    prepareProfileName: prepareProfileName,
    isValidIdempotencyKey: isValidIdempotencyKey,
    newIdempotencyKey: newIdempotencyKey,
    parseTimestamp: parseTimestamp,
    formatMoment: formatMoment,
    conversationLabel: conversationLabel,
    deriveTitle: deriveTitle,
    TITLE_LENGTH: TITLE_LENGTH,
    normalizeFavorite: normalizeFavorite,
    normalizeFavoriteList: normalizeFavoriteList,
    classifyFavoriteKeep: classifyFavoriteKeep,
    classifyFavoriteRemove: classifyFavoriteRemove,
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
    createTelegramLink: createTelegramLink,
    TELEGRAM_TEXT: TELEGRAM_TEXT,
    PHOTO_URL: PHOTO_URL,
    normalizePhoto: normalizePhoto,
  };
});
